"""#1050: the patient picker lists names and personnummer from eps.

Operator 2026-11-22: "why is the patient list in the invite lacking patient
names and personnummer?" Because the picker listed cdr1 only, which holds no
demographics by design, and only patients WITH observations — so a patient
newly registered in eps could not be opened, and therefore not invited.

Every test goes through the real routes with AUTH_MODE=sso (request loader,
care-delivery gate, audit decorator). The ONLY thing patched is the outbound
HTTP boundary (`requests.get` / `requests.post`): one fake answers as sso
(`/api/auth/me/service`), as eps (`/clinics/<unit>/patients`, refusing a unit
the bearer is not affiliated to exactly as eps's #1050 gate does, plus the
`/state`, `/clinics`, `/blocks` the opened-patient views read) and as cdr1
(`/api/v1/clinical/patients`). No client of ours is patched.

Personnummer are SYNTHETIC: built here with a deliberately WRONG check digit.
They are never printed.
"""
from __future__ import annotations

import json
import logging
import re
import uuid

import pytest
import requests
import sqlalchemy

from app import create_app
from app.models import DashboardAudit, db

SSO = "https://sso.test"
EPS = "https://eps.test"
CDR = "https://cdr1.test"
TOKEN = "tok-picker"
UNIT_A = "cccccccc-1050-4000-8000-0000000000a2"
UNIT_B = "cccccccc-1050-4000-8000-0000000000b2"
NAMES = {UNIT_A: "Vårdcentralen Norr", UNIT_B: "Vårdcentralen Söder"}
P_BOTH = "aaaaaaaa-1050-4000-8000-0000000000e1"     # eps (A) + cdr1
P_EPS = "aaaaaaaa-1050-4000-8000-0000000000e2"      # eps (A) only, no data yet
P_CDR = "aaaaaaaa-1050-4000-8000-0000000000e3"      # cdr1 only
P_B = "aaaaaaaa-1050-4000-8000-0000000000e4"        # eps (B) only
PNR_SYSTEM = "urn:oid:1.2.752.129.2.1.3.1"


def _synthetic_pnr(yyyymmdd: str, serial3: str) -> str:
    """YYYYMMDD-NNNC with C one off the Luhn digit: never a real number."""
    nine = yyyymmdd[2:] + serial3
    total = 0
    for i, ch in enumerate(nine):
        d = int(ch) * (2 if i % 2 == 0 else 1)
        total += d - 9 if d > 9 else d
    correct = (10 - total % 10) % 10
    return f"{yyyymmdd}-{serial3}{(correct + 1) % 10}"


PNR_BOTH = _synthetic_pnr("19710304", "123")
PNR_EPS = _synthetic_pnr("19850612", "456")
PNR_B = _synthetic_pnr("19900101", "789")
ALL_PNR = (PNR_BOTH, PNR_EPS, PNR_B)


def _blob(units=(UNIT_A,), **kw):
    b = {"user_guid": "dddddddd-1050-4000-8000-0000000000f1",
         "email": "ssk@example.test", "user_type": "professional",
         "is_su_admin": False, "session_phases": [],
         "affiliations": [{"affiliation_guid": f"a{i}", "role": "nurse",
                           "care_unit_guid": u, "care_unit_name": NAMES.get(u, u)}
                          for i, u in enumerate(units)]}
    b.update(kw)
    return b


class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body) if body is not None else "<html></html>"
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def _eps_patient(guid, given, family, pnr_value):
    return {"guid": guid, "resource_id": guid, "identifier_system": PNR_SYSTEM,
            "identifier_value": pnr_value, "given_name": given,
            "family_name": family, "birth_date": None, "is_active": True}


class Fake:
    def __init__(self):
        self.blob = _blob()
        self.calls = []          # (method, url, headers)
        self.rosters = {
            UNIT_A: [_eps_patient(P_BOTH, "Synt", "Alfasson", PNR_BOTH),
                     _eps_patient(P_EPS, "Ny", "Registrerad", PNR_EPS)],
            UNIT_B: [_eps_patient(P_B, "Synt", "Betasson", PNR_B)],
        }
        self.cdr = [{"patient_guid": P_BOTH, "name": "", "observation_count": 7,
                     "last_observed_at": "2026-11-20T08:00:00+00:00"},
                    {"patient_guid": P_CDR, "name": "Cdr Namn", "observation_count": 3,
                     "last_observed_at": "2026-11-19T08:00:00+00:00"}]
        self.eps_down = None     # None | Exception | int status
        self.eps_down_units = set()

    def eps_roster_calls(self):
        return [c for c in self.calls if "/api/v1/clinics/" in c[1]]

    def cdr_calls(self):
        return [c for c in self.calls if c[1].startswith(CDR)]

    def get(self, url, headers=None, params=None, timeout=None, **kw):
        headers = dict(headers or {})
        self.calls.append(("GET", url, headers))
        if url == f"{SSO}/api/auth/me/service":
            if headers.get("Authorization") != f"Bearer {TOKEN}":
                return _Resp(401, {"error": "invalid"})
            return _Resp(200, self.blob)
        if url == f"{CDR}/api/v1/clinical/patients":
            return _Resp(200, {"patients": self.cdr})
        m = re.fullmatch(rf"{EPS}/api/v1/clinics/([^/]+)/patients", url)
        if m:
            unit = m.group(1)
            if headers.get("Authorization") != f"Bearer {TOKEN}":
                return _Resp(401, {"error": "Missing Authorization header"})
            if isinstance(self.eps_down, Exception) or unit in self.eps_down_units:
                raise requests.ConnectionError("eps down")
            if isinstance(self.eps_down, int):
                return _Resp(self.eps_down, None)
            mine = {a["care_unit_guid"] for a in self.blob.get("affiliations") or []}
            if not self.blob.get("is_su_admin") and unit not in mine:
                return _Resp(403, {"error": "not_affiliated"})
            if unit not in self.rosters:
                return _Resp(404, {"error": "Clinic not found"})
            return _Resp(200, self.rosters[unit])
        m = re.fullmatch(rf"{EPS}/api/v1/patients/([^/]+)/(state|clinics|blocks)", url)
        if m:
            guid, what = m.groups()
            if what == "blocks":
                return _Resp(200, {"blocks": []})
            listed = [u for u, rows in self.rosters.items()
                      if any(p["guid"] == guid for p in rows)]
            if not listed:
                return _Resp(404, {"error": "patient not found"})
            if what == "clinics":
                return _Resp(200, [{"guid": str(uuid.uuid4()), "care_unit_guid": u,
                                    "name": NAMES[u]} for u in listed],
                             headers={"X-PDHC-Patient-Guid": guid})
            p = next(p for rows in self.rosters.values() for p in rows if p["guid"] == guid)
            return _Resp(200, {"patient_guid": guid, "count": 3, "facts": [
                {"fact_type": "name.given", "value": {"value": [p["given_name"]]}},
                {"fact_type": "name.family", "value": {"value": p["family_name"]}},
                {"fact_type": "telecom", "value": {"system": "phone",
                                                   "value": "070-000 00 09", "use": "mobile"}}]})
        return _Resp(404, None)

    def post(self, url, headers=None, json=None, timeout=None, **kw):
        self.calls.append(("POST", url, dict(headers or {})))
        return _Resp(404, None)


@pytest.fixture
def up(monkeypatch):
    fake = Fake()
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(requests, "post", fake.post)
    return fake


def _make_app(**over):
    cfg = {
        "TESTING": True, "SECRET_KEY": "test",
        "DATABASE_URL": "sqlite:///:memory:",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {"connect_args": {"check_same_thread": False},
                                      "poolclass": sqlalchemy.pool.StaticPool},
        "AUTH_MODE": "sso", "SSO_BASE_URL": SSO, "SSO_CLIENT_ID": "cid",
        "SSO_CLIENT_SECRET": "secret",
        "SSO_CALLBACK_URL": "https://dashboard.test/auth/callback",
        "IPS_BASE_URL": EPS, "CDR1_BASE_URL": CDR,
        "DASHBOARD_PDHC_SERVICE_KEY": "dashboard-service-key",
    }
    cfg.update(over)
    app = create_app(cfg)
    with app.app_context():
        db.create_all()
    return app


@pytest.fixture
def app():
    return _make_app()


@pytest.fixture
def c(app, up):
    client = app.test_client()
    with client.session_transaction() as s:
        s["sso_token"] = TOKEN
    return client


def _rows(html):
    """guid -> the <tr> html of that picker row."""
    out = {}
    for m in re.finditer(r'<tr class="prow" data-guid="([^"]+)"(.*?)</tr>', html, re.S):
        out[m.group(1)] = m.group(0)
    return out


def _audit_dump(app):
    with app.app_context():
        return json.dumps([{c.name: str(getattr(r, c.name)) for c in r.__table__.columns}
                           for r in DashboardAudit.query.all()], ensure_ascii=False)


# --------------------------------------------------------------------------
# the union: names + personnummer from eps, counts from cdr1
# --------------------------------------------------------------------------

def test_union_and_dedupe_names_and_personnummer_from_eps(c, up):
    r = c.get("/select")
    assert r.status_code == 200, r.get_data(as_text=True)[:400]
    rows = _rows(r.get_data(as_text=True))
    assert set(rows) == {P_BOTH, P_EPS, P_CDR}            # P_BOTH once
    both = rows[P_BOTH]
    assert "Synt Alfasson" in both and PNR_BOTH in both
    assert ">7<" in both and "2026-11-20" in both
    assert "Vårdcentralen Norr" in both
    eps_only = rows[P_EPS]
    assert "Ny Registrerad" in eps_only and PNR_EPS in eps_only
    assert "ingen data än" in eps_only
    cdr_only = rows[P_CDR]
    assert "ej i patientregistret" in cdr_only and P_CDR[:8] in cdr_only
    # cdr1's own name is not shown: cdr1 holds no demographics by design.
    assert "Cdr Namn" not in cdr_only
    # data first, newest first; the no-data patient last
    html = r.get_data(as_text=True)
    assert html.index(P_BOTH) < html.index(P_CDR) < html.index(P_EPS)


def test_personnummer_is_displayed_yyyymmdd_dash_xxxx(c, up):
    up.rosters[UNIT_A][0]["identifier_value"] = PNR_BOTH.replace("-", "")
    rows = _rows(c.get("/select").get_data(as_text=True))
    assert f'class="pk-pnr">{PNR_BOTH}<' in rows[P_BOTH]


def test_a_non_personnummer_identifier_is_not_shown_as_one(c, up):
    up.rosters[UNIT_A][0]["identifier_system"] = "urn:oid:9.9.9"
    rows = _rows(c.get("/select").get_data(as_text=True))
    assert PNR_BOTH not in rows[P_BOTH]
    assert 'class="pk-pnr">—<' in rows[P_BOTH]


def test_eps_is_read_with_the_professionals_bearer_and_no_service_key(c, up):
    c.get("/select")
    calls = up.eps_roster_calls()
    assert calls
    for _, _, h in calls:
        assert h["Authorization"] == f"Bearer {TOKEN}"
        assert "X-Service-Key" not in h and "X-Source-Service" not in h


# --------------------------------------------------------------------------
# the eps-only patient can be opened, and then invited
# --------------------------------------------------------------------------

def test_an_eps_only_patient_is_openable_and_then_invitable(c, up):
    html = c.get("/select").get_data(as_text=True)
    assert f'href="/patient/{P_EPS}/charts"' in _rows(html)[P_EPS]
    r = c.get(f"/patient/{P_EPS}/charts")
    assert r.status_code == 200
    assert f'href="/bjud-in-patient/{P_EPS}"' in r.get_data(as_text=True)
    inv = c.get(f"/bjud-in-patient/{P_EPS}")
    assert inv.status_code == 200, inv.get_data(as_text=True)[:600]
    assert 'id="iv-patient">Ny Registrerad<' in inv.get_data(as_text=True)


# --------------------------------------------------------------------------
# Rule 24: which units are asked
# --------------------------------------------------------------------------

def test_only_affiliated_units_are_queried(c, up):
    html = c.get("/select").get_data(as_text=True)
    urls = [u for _, u, _ in up.eps_roster_calls()]
    assert urls == [f"{EPS}/api/v1/clinics/{UNIT_A}/patients"]
    assert P_B not in html and PNR_B not in html
    (cdr,) = up.cdr_calls()
    assert cdr[2]["X-Org-Guids"] == UNIT_A and "X-Is-Admin" not in cdr[2]


def test_two_affiliations_query_both_units(c, up):
    up.blob = _blob(units=(UNIT_A, UNIT_B))
    rows = _rows(c.get("/select").get_data(as_text=True))
    assert {u for _, u, _ in up.eps_roster_calls()} == {
        f"{EPS}/api/v1/clinics/{UNIT_A}/patients", f"{EPS}/api/v1/clinics/{UNIT_B}/patients"}
    assert "Synt Betasson" in rows[P_B] and "Vårdcentralen Söder" in rows[P_B]


def test_legacy_organization_ids_are_not_read(c, up):
    up.blob = _blob(units=(UNIT_A,), organization_ids=[UNIT_B])
    c.get("/select")
    assert [u for _, u, _ in up.eps_roster_calls()] == [f"{EPS}/api/v1/clinics/{UNIT_A}/patients"]


def test_no_affiliation_and_not_su_lists_nobody(c, up):
    up.blob = _blob(units=())
    r = c.get("/select")
    assert r.status_code == 403
    assert up.eps_roster_calls() == [] and up.cdr_calls() == []


def test_eps_refusing_a_unit_is_said_not_hidden(c, up, monkeypatch):
    up.blob = _blob(units=(UNIT_A, UNIT_B))
    # eps disagrees about B (e.g. the affiliation was just ended in sso)
    orig = up.get

    def get(url, **kw):
        if url.endswith(f"/clinics/{UNIT_B}/patients"):
            up.calls.append(("GET", url, dict(kw.get("headers") or {})))
            return _Resp(403, {"error": "not_affiliated"})
        return orig(url, **kw)
    monkeypatch.setattr(requests, "get", get)
    html = c.get("/select").get_data(as_text=True)
    assert 'id="eps-refused"' in html
    assert P_B not in html


# --------------------------------------------------------------------------
# degrade honestly
# --------------------------------------------------------------------------

@pytest.mark.parametrize("down", [requests.ConnectionError("x"), 500, 502])
def test_eps_down_shows_cdr1_list_with_a_banner(c, up, down):
    up.eps_down = down
    r = c.get("/select")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Patientregistret (eps) svarar inte – namn saknas" in html
    rows = _rows(html)
    assert set(rows) == {P_BOTH, P_CDR}
    assert all(p not in html for p in ALL_PNR)
    # eps did not answer: absence from it is NOT claimed
    assert "ej i patientregistret" not in html
    assert all("namn okänt" in r for r in rows.values())


def test_eps_down_for_one_of_two_units_is_a_partial_banner(c, up):
    up.blob = _blob(units=(UNIT_A, UNIT_B))
    up.eps_down_units = {UNIT_B}
    html = c.get("/select").get_data(as_text=True)
    assert 'id="eps-partial"' in html and "1 av 2" in html
    assert "Synt Alfasson" in html
    assert "ej i patientregistret" not in _rows(html)[P_CDR]


def test_eps_not_configured_is_a_banner_not_a_silent_list(up):
    app = _make_app(IPS_BASE_URL="")
    cl = app.test_client()
    with cl.session_transaction() as s:
        s["sso_token"] = TOKEN
    html = cl.get("/select").get_data(as_text=True)
    assert "Patientregistret (eps) svarar inte – namn saknas" in html
    assert set(_rows(html)) == {P_BOTH, P_CDR}


def test_a_healthy_eps_shows_no_banner(c, up):
    html = c.get("/select").get_data(as_text=True)
    assert 'id="eps-down"' not in html and 'id="eps-partial"' not in html


# --------------------------------------------------------------------------
# filter
# --------------------------------------------------------------------------

def test_filter_data_attributes_carry_name_personnummer_and_guid(c, up):
    rows = _rows(c.get("/select").get_data(as_text=True))
    hay = re.search(r'data-search="([^"]*)"', rows[P_BOTH]).group(1)
    assert "synt alfasson" in hay
    assert PNR_BOTH in hay and PNR_BOTH.replace("-", "") in hay
    assert P_BOTH in hay
    assert 'data-in-eps="nej"' in rows[P_CDR]
    assert P_CDR in re.search(r'data-search="([^"]*)"', rows[P_CDR]).group(1)


# --------------------------------------------------------------------------
# the personnummer stays on the page
# --------------------------------------------------------------------------

def test_no_personnummer_in_logs_audit_or_urls_and_no_store(app, c, up, caplog):
    caplog.set_level(logging.DEBUG)
    r = c.get("/select")
    assert r.status_code == 200
    assert "no-store" in r.headers["Cache-Control"]
    assert PNR_BOTH in r.get_data(as_text=True)         # it IS shown
    dump = _audit_dump(app)
    for p in ALL_PNR:
        for form in (p, p.replace("-", "")):
            assert form not in caplog.text
            assert form not in dump
            assert all(form not in u for _, u, _ in up.calls)
    assert "Alfasson" not in dump
    with app.app_context():
        row = DashboardAudit.query.filter_by(route="GET /select").one()
        assert row.n_rows_returned == 3
        assert row.payload_snapshot["eps_roster"] == "ok"
        assert row.payload_snapshot["eps_patients"] == 2


# --------------------------------------------------------------------------
# SU
# --------------------------------------------------------------------------

def test_su_attests_first_and_nothing_is_read_before(c, up):
    up.blob = _blob(units=(), is_su_admin=True)
    html = c.get("/select").get_data(as_text=True)
    assert "attestation required" in html
    assert up.eps_roster_calls() == [] and up.cdr_calls() == []


def test_su_without_units_gets_cdr1_break_glass_list_and_no_eps_fan_out(c, up):
    up.blob = _blob(units=(), is_su_admin=True)
    with c.session_transaction() as s:
        s["admin_read_reason"] = "test attest"
    html = c.get("/select").get_data(as_text=True)
    assert up.eps_roster_calls() == []
    (cdr,) = up.cdr_calls()
    assert cdr[2]["X-Is-Admin"] == "1"
    assert set(_rows(html)) == {P_BOTH, P_CDR}
    assert 'id="su-note"' in html
    assert "ej i patientregistret" not in html and "namn okänt" in html


def test_su_with_own_units_gets_their_rosters(c, up):
    up.blob = _blob(units=(UNIT_B,), is_su_admin=True)
    with c.session_transaction() as s:
        s["admin_read_reason"] = "test attest"
    rows = _rows(c.get("/select").get_data(as_text=True))
    assert [u for _, u, _ in up.eps_roster_calls()] == [f"{EPS}/api/v1/clinics/{UNIT_B}/patients"]
    assert set(rows) == {P_BOTH, P_CDR, P_B}


def test_the_eps_fan_out_is_bounded(c, up):
    from app.services.eps_roster import MAX_UNITS
    many = [f"cccccccc-1050-4000-8000-{i:012d}" for i in range(MAX_UNITS + 3)]
    up.blob = _blob(units=many)
    html = c.get("/select").get_data(as_text=True)
    assert len(up.eps_roster_calls()) == MAX_UNITS
    assert 'id="eps-truncated"' in html
