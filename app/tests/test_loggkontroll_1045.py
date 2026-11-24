"""#1045 loggkontroll: the open patient's access log, merged across services.

Through the real routes with AUTH_MODE=sso (request loader, front-door gate,
the page's own review gate). The ONLY thing patched is the outbound HTTP
boundary (`requests.get`): the invite tests' fake answers as sso.pdhc and
eps (`/clinics`, `/state`, `/blocks`), and is extended here to answer as
cdr1's, eps's and request's access-log endpoints, each requiring the
professional's own bearer (request: as X-API-Key) and refusing a service key.
No client of ours is patched.

Fixed period 2026-11-02 (Monday) .. 2026-11-08 (Sunday). Synthetic data only.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone

import pytest
import requests

from app.models import DashboardAudit, db
from app.tests.test_invite_1050 import (  # noqa: F401 — fixtures and fakes
    EPS, P1, P2, TOKEN, UNIT_A, UNIT_B, Fake, _blob, _open, _Resp, app)

CDR = "https://cdr.test"
REQ = "https://request.test"
PERIOD = "fran=2026-11-02&till=2026-11-08"
SU = _blob(units=(), is_su_admin=True)
COLLEAGUE = "eeeeeeee-1045-4000-8000-00000000000e"


def _item(at, action, **kw):
    base = {"id": f"{action}-{at}", "at": at, "user_guid": COLLEAGUE,
            "user_label": None, "actor_type": "user", "action": action,
            "outcome": "served", "route": None, "resource_type": None,
            "session_id": None, "actor_units": None, "basis": None, "detail": {}}
    base.update(kw)
    return base


class LogFake(Fake):
    def __init__(self):
        super().__init__()
        self.logs = {
            "cdr": [_item("2026-11-02T09:00:00+00:00", "read Observation",
                          actor_units=[UNIT_A]),            # Mon 10:00 local: normal
                    _item("2026-11-08T01:00:00+00:00", "read Condition",
                          actor_units=[UNIT_B])],           # Sun 02:00, other unit
            "eps": [_item("2026-11-03T12:00:00+00:00", "euips_summary_read",
                          user_label="kollega@example.test")],
            "req": [_item("2026-11-04T08:30:00+00:00", "care_plan.read.denied",
                          outcome="denied", detail={"reason": "patient_org_mismatch"}),
                    _item("2026-11-05T09:00:00+00:00", "=HYPERLINK(\"x\")",
                          basis="su_admin")],
        }
        self.log_answer = {}        # "cdr"/"eps"/"req" -> _Resp or Exception
        self.log_calls = []         # (which, headers, params)

    def get(self, url, headers=None, params=None, timeout=None, **kw):
        headers = headers or {}
        which = None
        if re.fullmatch(rf"{CDR}/api/v1/access-log/patients/[^/]+", url):
            which, ok = "cdr", headers.get("Authorization") == f"Bearer {TOKEN}"
        elif re.fullmatch(rf"{EPS}/api/v1/audit/patients/[^/]+", url):
            which, ok = "eps", headers.get("Authorization") == f"Bearer {TOKEN}"
        elif re.fullmatch(rf"{REQ}/api/v1/patients/[^/]+/access-log", url):
            which, ok = "req", headers.get("X-API-Key") == TOKEN
        if which is None:
            return super().get(url, headers=headers, params=params, timeout=timeout, **kw)
        self.log_calls.append((which, dict(headers), dict(params or {})))
        if "X-Service-Key" in headers:
            return _Resp(403, {"error": "service key refused"})
        if not ok:
            return _Resp(401, {"error": "not_authenticated"})
        ans = self.log_answer.get(which)
        if isinstance(ans, Exception):
            raise ans
        if ans is not None:
            return ans
        return _Resp(200, {"service": which, "items": self.logs[which], "truncated": False})


@pytest.fixture
def up(monkeypatch):
    fake = LogFake()
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(requests, "post", fake.post)
    return fake


@pytest.fixture
def c(app, up):
    app.config.update(CDR1_BASE_URL=CDR, REQUEST_BASE_URL=REQ, LOGGKONTROLL_ROLES="")
    with app.app_context():
        db.session.add(DashboardAudit(
            user_guid=COLLEAGUE, user_org_guids=[UNIT_A], route="GET /patient/<guid>/charts",
            patient_guid=P1, response_status=200, event_type="admin_override",
            admin_justification="akut", timestamp=datetime(2026, 11, 6, 10, 0, tzinfo=timezone.utc)))
        db.session.commit()
    client = app.test_client()
    with client.session_transaction() as s:
        s["sso_token"] = TOKEN
    return client


def as_su(up):
    up.blob = dict(SU)


def L(guid=P1, q=PERIOD):
    return f"/loggkontroll/{guid}?{q}"


def _audit(app, event):
    with app.app_context():
        return [(r.patient_guid, r.response_status, r.payload_snapshot or {})
                for r in DashboardAudit.query.filter_by(event_type=event).all()]


def _rows(html):
    return re.findall(r'<tr class="[^"]*" data-service="([^"]+)" data-unusual="(\d)"', html)


# --------------------------------------------------------------------------
# the merged list
# --------------------------------------------------------------------------

def test_su_sees_every_service_merged_in_time_order(c, up):
    as_su(up)
    _open(c)
    r = c.get(L())
    html = r.get_data(as_text=True)
    assert r.status_code == 200, html[:500]
    services = [s for s, _ in _rows(html)]
    assert services == ["cdr.pdhc", "eps.pdhc", "request.pdhc", "request.pdhc",
                        "dashboard.pdhc", "cdr.pdhc"]
    assert html.count('data-ok="1"') == 4 and 'data-ok="0"' not in html
    assert "kollega@example.test" in html
    assert "Skäl: akut" in html


def test_each_service_is_asked_with_the_persons_bearer_and_the_period(c, up):
    as_su(up)
    _open(c)
    c.get(L())
    assert sorted(w for w, _, _ in up.log_calls) == ["cdr", "eps", "req"]
    for which, headers, params in up.log_calls:
        assert "X-Service-Key" not in headers and "X-Source-Service" not in headers
        # Stockholm days -> UTC instants, end exclusive (CET = UTC+1)
        assert params["from"] == "2026-11-01T23:00:00Z"
        assert params["to"] == "2026-11-08T23:00:00Z"


def test_the_flags_of_the_systematic_check(c, up):
    as_su(up)
    _open(c)
    html = c.get(L()).get_data(as_text=True)
    rows = re.findall(r'<tr class="[^"]*" data-service="[^"]+" data-unusual="\d">(.*?)</tr>',
                      html, re.S)
    flags = [set(re.findall(r'data-flag="(\w+)"', row)) for row in rows]
    assert flags[0] == set()                                   # Mon 10:00, own unit
    assert flags[2] == {"nekad"}                               # request refusal
    assert flags[3] == {"su_admin"}
    assert flags[4] == {"nodoppning"}                          # dashboard override
    assert flags[5] == {"utanfor_enhet", "utanfor_arbetstid"}  # Sun 02:00, unit B


def test_only_unusual_and_a_reproducible_sample(c, up):
    as_su(up)
    _open(c)
    html = c.get(L(q=PERIOD + "&visa=avvikande")).get_data(as_text=True)
    assert [u for _, u in _rows(html)] == ["1"] * 4
    one = c.get(L(q=PERIOD + "&visa=stickprov&antal=3&urval=4242")).get_data(as_text=True)
    two = c.get(L(q=PERIOD + "&visa=stickprov&antal=3&urval=4242")).get_data(as_text=True)
    assert len(_rows(one)) == 3 and _rows(one) == _rows(two)
    assert "Urvalsnummer 4242" in one
    audit = _audit(c.application, "loggkontroll")
    assert audit[-1][2]["urval"] == 4242 and audit[-1][2]["visa"] == "stickprov"


# --------------------------------------------------------------------------
# never silent
# --------------------------------------------------------------------------

@pytest.mark.parametrize("answer,expect", [
    (requests.ConnectionError("down"), "tjänsten svarade inte"),
    (_Resp(403, {"error": "forbidden"}), "ger dig inte tillgång"),
    (_Resp(200, {"no": "items"}), "fel form"),
    (_Resp(500, None, "<html>"), "svarade med fel (500)"),
])
def test_a_failing_service_is_named_never_an_empty_list(c, up, answer, expect):
    as_su(up)
    up.log_answer["cdr"] = answer
    _open(c)
    r = c.get(L())
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert 'data-service="cdr.pdhc" data-ok="0"' in html
    assert "Kunde inte hämtas från Journaldatabasen (cdr1)" in html and expect in html
    assert [s for s, _ in _rows(html)].count("cdr.pdhc") == 0
    assert "eps.pdhc" in [s for s, _ in _rows(html)]
    assert _audit(c.application, "loggkontroll")[-1][2]["failed"] == ["cdr.pdhc"]


def test_an_unconfigured_service_is_named(c, up):
    as_su(up)
    c.application.config["REQUEST_BASE_URL"] = ""
    _open(c)
    html = c.get(L()).get_data(as_text=True)
    assert "Kunde inte hämtas från Beställningar (request)" in html
    assert "REQUEST_BASE_URL saknas" in html


# --------------------------------------------------------------------------
# who may review
# --------------------------------------------------------------------------

def test_a_professional_is_refused_while_no_review_role_is_named(c, up):
    _open(c)                                       # nurse at UNIT_A, P1 listed at A
    r = c.get(L())
    assert r.status_code == 403
    assert "systemadministratör (SU)" in r.get_data(as_text=True)
    assert up.log_calls == []
    assert _audit(c.application, "loggkontroll")[-1][2]["outcome"] == "su_only"


def test_a_named_role_at_the_patients_unit_may_review(c, up):
    c.application.config["LOGGKONTROLL_ROLES"] = "nurse, loggranskare"
    _open(c)
    assert c.get(L()).status_code == 200
    assert len(up.log_calls) == 3


def test_a_named_role_at_another_unit_may_not(c, up):
    c.application.config["LOGGKONTROLL_ROLES"] = "nurse"
    up.blob = _blob(units=(UNIT_B,))
    _open(c)
    r = c.get(L())
    assert r.status_code == 403 and "inte listad där" in r.get_data(as_text=True)
    assert up.log_calls == []


def test_another_role_at_the_unit_may_not(c, up):
    c.application.config["LOGGKONTROLL_ROLES"] = "loggranskare"
    _open(c)
    assert c.get(L()).status_code == 403


def test_a_patient_not_opened_is_refused_before_anyone_is_asked(c, up):
    as_su(up)
    _open(c, P1)
    r = c.get(L(P2))
    assert r.status_code == 403 and "patient du har öppen" in r.get_data(as_text=True)
    assert up.log_calls == []
    assert c.get("/loggkontroll/not-a-guid").status_code == 404


def test_a_patient_bearer_is_refused_at_the_front_door(c, up):
    up.blob = {"user_guid": "p", "user_type": "patient", "is_su_admin": False}
    r = c.get(L())
    assert r.status_code in (302, 403)
    # refused by the front door, before the page runs: not even eps is asked
    assert "Loggkontrollen kan inte visas" not in r.get_data(as_text=True)
    assert up.eps_calls() == [] and up.log_calls == []


def test_the_link_is_offered_to_su_and_named_roles_only(c, up):
    html = _open(c).get_data(as_text=True)
    assert 'id="loggkontroll-action"' not in html
    c.application.config["LOGGKONTROLL_ROLES"] = "nurse"
    assert 'id="loggkontroll-action"' in _open(c).get_data(as_text=True)
    c.application.config["LOGGKONTROLL_ROLES"] = ""
    as_su(up)
    assert f'href="/loggkontroll/{P1}"' in _open(c).get_data(as_text=True)


# --------------------------------------------------------------------------
# input, export, audit
# --------------------------------------------------------------------------

def test_bad_dates_are_named(c, up):
    as_su(up)
    _open(c)
    r = c.get(L(q="fran=2026-11-08&till=2026-11-02"))
    assert r.status_code == 400 and "efter till-datumet" in r.get_data(as_text=True)
    assert c.get(L(q="fran=igår")).status_code == 400
    assert c.get(L(q="fran=2025-01-01&till=2026-11-08")).status_code == 400
    assert up.log_calls == []


def test_csv_export_carries_failures_and_neutralises_formulas(c, up):
    as_su(up)
    up.log_answer["eps"] = requests.ConnectionError("down")
    _open(c)
    r = c.get(f"/loggkontroll/{P1}/csv?{PERIOD}")
    assert r.status_code == 200 and r.headers["Content-Type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(r.get_data(as_text=True).lstrip("﻿")),
                           delimiter=";"))
    assert rows[0][:3] == ["tid (svensk tid)", "tjänst", "användare"]
    assert rows[1][1] == "eps.pdhc" and rows[1][5] == "KUNDE INTE HÄMTAS"
    assert len(rows) == 1 + 1 + 5
    assert any(row[5] == "'=HYPERLINK(\"x\")" for row in rows)
    assert _audit(c.application, "loggkontroll_export")[-1][0] == P1


def test_every_view_is_audited_with_the_patient_and_period(c, up):
    as_su(up)
    _open(c)
    c.get(L())
    (patient, status, payload), = _audit(c.application, "loggkontroll")
    assert patient == P1 and status == 200
    assert payload["fran"] == "2026-11-02" and payload["till"] == "2026-11-08"
    assert payload["outcome"] == "shown" and payload["n_rows"] == 6
