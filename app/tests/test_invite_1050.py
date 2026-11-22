"""#1050 — "Bjud in patient": a patient joins the patient app ONLY by an
invitation from their care unit, made here.

Every test goes through the real routes with AUTH_MODE=sso, so the request
loader, the gate and the CSRF check all run. The ONLY thing patched is the
outbound HTTP boundary (`requests.get` / `requests.post`): one fake answers as
sso.pdhc (`/api/auth/me/service`, `/api/patient-links/invites`) and as eps
(`/api/v1/patients/register-at-unit`), each refusing what the real service
refuses (a service key, a unit the bearer is not affiliated to). No client of
ours is patched, so a wrong URL, header or body is a failing test.

Synthetic data only: the personnummer below have a WRONG check digit.
"""
from __future__ import annotations

import json
import re
import uuid

import pytest
import requests
import sqlalchemy

from app import create_app
from app.models import DashboardAudit, db

SSO = "https://sso.test"
EPS = "https://eps.test"
TOKEN = "tok-operator"
UNIT_A = "cccccccc-1050-4000-8000-00000000000a"
UNIT_B = "cccccccc-1050-4000-8000-00000000000b"
PNR = "19790517-3219"          # synthetic (wrong check digit)
P_NEW = "aaaaaaaa-1050-4000-8000-000000000001"
INVITE_URL = "https://sso.test/patient-invite/ONE-TIME-TOKEN-xyz"


def _blob(units=(UNIT_A,), **kw):
    names = {UNIT_A: "Vårdcentralen Norr", UNIT_B: "Vårdcentralen Söder"}
    b = {"user_guid": "dddddddd-1050-4000-8000-000000000001",
         "email": "ssk@example.test", "user_type": "professional",
         "is_su_admin": False, "session_phases": [],
         "affiliations": [{"affiliation_guid": f"a{i}", "role": "nurse",
                           "care_unit_guid": u, "care_unit_name": names.get(u, u)}
                          for i, u in enumerate(units)]}
    b.update(kw)
    return b


class _Resp:
    def __init__(self, status, body=None, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class Fake:
    def __init__(self):
        self.blob = _blob()
        self.eps_calls, self.sso_calls = [], []
        self.eps_answer = None      # _Resp or Exception: overrides the logic
        self.sso_answer = None
        self.patients = {}          # pnr -> guid

    def get(self, url, headers=None, params=None, timeout=None, **kw):
        headers = headers or {}
        if url == f"{SSO}/api/auth/me/service":
            if headers.get("Authorization") != f"Bearer {TOKEN}":
                return _Resp(401, {"error": "invalid"})
            return _Resp(200, self.blob)
        return _Resp(404, None, "<html>404</html>")

    def _person(self, headers):
        if "X-Service-Key" in headers or "X-Source-Service" in headers:
            return "service"
        return "ok" if headers.get("Authorization") == f"Bearer {TOKEN}" else None

    def post(self, url, headers=None, json=None, timeout=None, **kw):
        headers = headers or {}
        units = {a["care_unit_guid"] for a in self.blob.get("affiliations") or []}
        if url == f"{EPS}/api/v1/patients/register-at-unit":
            self.eps_calls.append({"headers": dict(headers), "json": dict(json)})
            who = self._person(headers)
            if who == "service":
                return _Resp(403, {"resourceType": "OperationOutcome"})
            if who is None:
                return _Resp(401, {"resourceType": "OperationOutcome"})
            if isinstance(self.eps_answer, Exception):
                raise self.eps_answer
            if self.eps_answer is not None:
                return self.eps_answer
            if not self.blob.get("is_su_admin") and json["care_unit_guid"] not in units:
                return _Resp(403, {"error": "not_affiliated"})
            if json["personnummer"] in self.patients:
                return _Resp(200, {"patient_guid": self.patients[json["personnummer"]],
                                   "created": False, "assignment": "existing"})
            self.patients[json["personnummer"]] = P_NEW
            return _Resp(201, {"patient_guid": P_NEW, "created": True, "assignment": "added"})
        if url == f"{SSO}/api/patient-links/invites":
            self.sso_calls.append({"headers": dict(headers), "json": dict(json)})
            who = self._person(headers)
            if who != "ok":
                return _Resp(401, {"error": "unauthorized"})
            if isinstance(self.sso_answer, Exception):
                raise self.sso_answer
            if self.sso_answer is not None:
                return self.sso_answer
            if not self.blob.get("is_su_admin") and json["care_unit_guid"] not in units:
                return _Resp(403, {"error": "not_affiliated"})
            return _Resp(201, {"invite_guid": str(uuid.uuid4()),
                               "ips_patient_guid": json["ips_patient_guid"],
                               "care_unit_guid": json["care_unit_guid"],
                               "given_name": json.get("given_name"),
                               "family_name": json.get("family_name"),
                               "expires_at": "2026-11-29T12:00:00+00:00",
                               "invite_url": INVITE_URL})
        return _Resp(404, None, "<html>404</html>")


@pytest.fixture
def up(monkeypatch):
    fake = Fake()
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(requests, "post", fake.post)
    return fake


@pytest.fixture
def app():
    app = create_app({
        "TESTING": True, "SECRET_KEY": "test",
        "DATABASE_URL": "sqlite:///:memory:",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {"connect_args": {"check_same_thread": False},
                                      "poolclass": sqlalchemy.pool.StaticPool},
        "AUTH_MODE": "sso", "SSO_BASE_URL": SSO, "SSO_CLIENT_ID": "cid",
        "SSO_CLIENT_SECRET": "secret",
        "SSO_CALLBACK_URL": "https://dashboard.test/auth/callback",
        "IPS_BASE_URL": EPS,
        # Set on purpose: the invite calls must NOT send it.
        "DASHBOARD_PDHC_SERVICE_KEY": "dashboard-service-key",
    })
    with app.app_context():
        db.create_all()
    return app


@pytest.fixture
def c(app, up):
    client = app.test_client()
    with client.session_transaction() as s:
        s["sso_token"] = TOKEN
    return client


def _csrf(client):
    html = client.get("/bjud-in-patient").get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, html[:500]
    return m.group(1)


def _form(**over):
    f = {"given_name": "Synt", "family_name": "Inbjudensson",
         "personnummer": PNR, "mobile": "070-000 00 01", "care_unit_guid": UNIT_A}
    f.update(over)
    return {k: v for k, v in f.items() if v is not None}


def _send(client, **over):
    data = _form(**over)
    data.setdefault("csrf_token", _csrf(client))
    data.setdefault("confirmed", "ja")
    return client.post("/bjud-in-patient/skicka", data=data)


def _audit(app):
    with app.app_context():
        return [{"event": r.event_type, "patient": r.patient_guid,
                 "status": r.response_status, "payload": r.payload_snapshot or {}}
                for r in DashboardAudit.query.order_by(DashboardAudit.timestamp).all()]


# --------------------------------------------------------------------------
# the unit choice
# --------------------------------------------------------------------------

def test_one_affiliation_is_preselected_and_linked_from_the_nav(c, up):
    r = c.get("/bjud-in-patient")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert 'id="invite-nav"' in html
    assert "Vårdenhet:</strong> Vårdcentralen Norr" in html
    assert f'type="hidden" name="care_unit_guid" value="{UNIT_A}"' in html
    assert 'type="radio"' not in html
    for label in ("Förnamn", "Efternamn", "Personnummer (syntetiskt i piloten)", "Mobil (valfri)"):
        assert label in html


def test_several_affiliations_force_a_choice_with_no_default(c, up):
    up.blob = _blob(units=(UNIT_A, UNIT_B))
    html = c.get("/bjud-in-patient").get_data(as_text=True)
    radios = re.findall(r'<input type="radio"[^>]*>', html)
    assert len(radios) == 2
    assert all("checked" not in x and "required" in x for x in radios)
    # the server refuses a POST without a choice, and calls nobody
    data = _form(care_unit_guid=None)
    data["csrf_token"] = _csrf(c)
    r = c.post("/bjud-in-patient/bekrafta", data=data)
    assert r.status_code == 400 and "Välj vårdenhet." in r.get_data(as_text=True)
    data["confirmed"] = "ja"
    r = c.post("/bjud-in-patient/skicka", data=data)
    assert r.status_code == 400 and "Välj vårdenhet." in r.get_data(as_text=True)
    assert up.eps_calls == [] and up.sso_calls == []
    # with a choice: B, and B is what eps and sso are asked for
    r = _send(c, care_unit_guid=UNIT_B)
    assert r.status_code == 200
    assert up.eps_calls[0]["json"]["care_unit_guid"] == UNIT_B
    assert up.sso_calls[0]["json"]["care_unit_guid"] == UNIT_B


def test_no_affiliation_and_not_su_is_refused_with_a_message(c, up):
    up.blob = _blob(units=())
    r = c.get("/bjud-in-patient")
    html = r.get_data(as_text=True)
    assert r.status_code == 403
    assert "Du har ingen affiliering till någon vårdenhet" in html
    assert 'id="iv-form"' not in html
    with c.session_transaction() as sess:       # the page offers no form
        sess["_csrf_token"] = "known-token"
    r = c.post("/bjud-in-patient/skicka", data=dict(_form(), confirmed="ja",
                                                    csrf_token="known-token"))
    assert r.status_code == 403 and up.eps_calls == []
    assert "Du har ingen affiliering" in r.get_data(as_text=True)


def test_a_membership_is_not_an_affiliation(c, up):
    up.blob = _blob(units=(), organization_ids=[UNIT_A])
    assert c.get("/bjud-in-patient").status_code == 403


def test_a_unit_outside_the_affiliations_is_refused_before_any_call(c, up):
    r = _send(c, care_unit_guid=UNIT_B)
    assert r.status_code == 400
    assert "inte affilierad till den valda vårdenheten" in r.get_data(as_text=True)
    assert up.eps_calls == []


def test_su_without_affiliation_may_name_a_unit(c, up):
    up.blob = _blob(units=(), is_su_admin=True)
    html = c.get("/bjud-in-patient").get_data(as_text=True)
    assert 'name="care_unit_guid_su"' in html
    r = _send(c, care_unit_guid=None, care_unit_guid_su=UNIT_B)
    assert r.status_code == 200, r.get_data(as_text=True)[:400]
    assert up.eps_calls[0]["json"]["care_unit_guid"] == UNIT_B


def test_a_patient_is_refused_by_the_gate(c, up):
    up.blob = _blob(user_type="patient")
    assert c.get("/bjud-in-patient").status_code == 403
    assert 'id="invite-nav"' not in c.get("/bjud-in-patient").get_data(as_text=True)


# --------------------------------------------------------------------------
# the two steps
# --------------------------------------------------------------------------

def test_csrf_and_confirmation_are_required(c, up):
    r = c.post("/bjud-in-patient/bekrafta", data=_form())
    assert r.status_code == 403
    r = c.post("/bjud-in-patient/skicka", data=dict(_form(), confirmed="ja"))
    assert r.status_code == 403
    r = c.post("/bjud-in-patient/skicka", data=dict(_form(), csrf_token=_csrf(c)))
    assert r.status_code == 400
    assert up.eps_calls == [] and up.sso_calls == []


def test_confirm_shows_the_data_and_calls_nothing(c, up):
    data = dict(_form(), csrf_token=_csrf(c))
    r = c.post("/bjud-in-patient/bekrafta", data=data)
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "Synt Inbjudensson" in html and "Vårdcentralen Norr" in html
    assert "+46700000001" in html                 # normalised
    assert 'action="/bjud-in-patient/skicka"' in html
    assert up.eps_calls == [] and up.sso_calls == []


def test_send_calls_eps_then_sso_with_the_bearer_only_and_shows_the_link_once(app, c, up):
    r = _send(c)
    html = r.get_data(as_text=True)
    assert r.status_code == 200, html[:500]
    assert r.headers["Cache-Control"] == "no-store"
    assert html.count(INVITE_URL) == 1
    assert 'id="iv-copy"' in html and "Sms skickas inte ännu" in html
    (e,), (s,) = up.eps_calls, up.sso_calls
    for call in (e, s):
        assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
        assert "X-Service-Key" not in call["headers"]
    assert e["json"] == {"given_name": "Synt", "family_name": "Inbjudensson",
                         "personnummer": PNR, "care_unit_guid": UNIT_A,
                         "mobile": "+46700000001"}
    assert s["json"] == {"ips_patient_guid": P_NEW, "care_unit_guid": UNIT_A,
                         "given_name": "Synt", "family_name": "Inbjudensson"}
    rows = [x for x in _audit(app) if x["event"] == "patient_invite_send"]
    assert len(rows) == 1 and rows[0]["payload"]["outcome"] == "invited"
    assert str(rows[0]["patient"]) == P_NEW
    blob = json.dumps(_audit(app), default=str)
    for secret in (PNR, PNR.replace("-", ""), INVITE_URL, "ONE-TIME-TOKEN", "Inbjudensson"):
        assert secret not in blob


def test_mobile_is_optional_and_validated(c, up):
    assert _send(c, mobile="").status_code == 200
    assert "mobile" not in up.eps_calls[-1]["json"]
    r = _send(c, mobile="12345")
    assert r.status_code == 400 and "svenskt mobilnummer" in r.get_data(as_text=True)


@pytest.mark.parametrize("answer,status,text", [
    (_Resp(422, {"error": "real_personnummer"}), 422, "Piloten tar bara emot syntetiska"),
    (_Resp(422, {"error": "invalid_personnummer"}), 422, "inte ett giltigt personnummer"),
    (_Resp(409, {"error": "name_mismatch"}), 409, "under ett annat namn"),
    (_Resp(403, {"error": "not_affiliated"}), 403, "inte affilierad"),
    (_Resp(422, {"error": "unknown_unit"}), 422, "aktiv enhet"),
    (_Resp(500, None, "<html>boom</html>"), 503, "svarade inte"),
    (_Resp(404, None, "<html>404</html>"), 503, "svarade inte"),
    (requests.ConnectionError("refused"), 503, "svarade inte"),
])
def test_an_eps_refusal_is_named_and_sso_is_not_asked(c, up, answer, status, text):
    up.eps_answer = answer
    r = _send(c)
    assert r.status_code == status
    assert text in r.get_data(as_text=True)
    assert up.sso_calls == []
    assert INVITE_URL not in r.get_data(as_text=True)


def test_patient_already_linked_says_the_patient_has_an_account(c, up):
    up.sso_answer = _Resp(409, {"error": "patient_already_linked"})
    r = _send(c)
    html = r.get_data(as_text=True)
    assert r.status_code == 409
    assert "Patienten har redan ett konto" in html and 'id="iv-retry"' not in html


def test_sso_failing_after_eps_says_registered_and_retry_works(c, up):
    up.sso_answer = requests.ConnectionError("down")
    r = _send(c)
    html = r.get_data(as_text=True)
    assert r.status_code == 502
    assert "Patienten är registrerad, men inbjudan skapades inte" in html
    assert 'id="iv-retry"' in html and INVITE_URL not in html
    # the retry form carries the confirmed values and a fresh CSRF token
    data = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', html))
    assert data["personnummer"] == PNR and data["confirmed"] == "ja"
    up.sso_answer = None
    r = c.post("/bjud-in-patient/skicka", data=data)
    assert r.status_code == 200 and INVITE_URL in r.get_data(as_text=True)
    # eps found the same patient: nothing was registered twice
    assert len(up.patients) == 1 and len(up.eps_calls) == 2


def test_a_professional_without_unit_can_sign_in_only_to_reach_the_invite_page(app, up):
    """Otherwise the clear message could never be shown: the callback refused
    anyone without a care unit or the analysis phase."""
    up.blob = _blob(units=())
    for nxt, expected in (("http://localhost/bjud-in-patient", "/bjud-in-patient"),
                          ("http://localhost/nurse", "/auth/login")):
        client = app.test_client()
        with client.session_transaction() as s:
            s["sso_state"], s["sso_next"] = "st", nxt
        r = client.get(f"/auth/callback?token={TOKEN}&state=st")
        assert r.status_code == 302 and r.headers["Location"].endswith(expected), (nxt, r.headers)
    # a patient is not let in that way
    up.blob = _blob(units=(), user_type="patient")
    client = app.test_client()
    with client.session_transaction() as s:
        s["sso_state"], s["sso_next"] = "st", "http://localhost/bjud-in-patient"
    r = client.get(f"/auth/callback?token={TOKEN}&state=st")
    assert r.headers["Location"].endswith("/auth/login")
