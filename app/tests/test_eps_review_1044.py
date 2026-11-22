"""#1044 — the queue "EPS att granska".

Every test goes through the real routes with AUTH_MODE=sso, so the request
loader, the care-delivery gate and the CSRF check all run. The ONLY thing
patched is the outbound HTTP boundary (`requests.get` / `requests.post`): one
fake answers as sso.pdhc's `/api/auth/me/service` and as cdr1's review API,
following `plans/patient_app_contract.md` s4.2. No client of ours is patched,
so a wrong URL, header or body is a failing test.

Synthetic data only. The controls run for each guard are recorded in
progress.md (2026-11-22).
"""
from __future__ import annotations

import json
import re

import pytest
import requests
import sqlalchemy

from app import create_app
from app.models import DashboardAudit, db

SSO = "https://sso.test"
CDR = "https://cdr1.test"
TOKEN = "tok-operator"

P1 = "aaaaaaaa-0000-4000-8000-000000000001"
P2 = "aaaaaaaa-0000-4000-8000-000000000002"
R1 = "bbbbbbbb-0000-4000-8000-000000000001"
R2 = "bbbbbbbb-0000-4000-8000-000000000002"
R3 = "bbbbbbbb-0000-4000-8000-000000000003"
R4 = "bbbbbbbb-0000-4000-8000-000000000004"
UNIT = "cccccccc-0000-4000-8000-000000000001"

ANSWER_1 = "Penicillin ger utslag"
ANSWER_2 = "Metformin 500 mg morgon och kväll"
ANSWER_3 = "Blindtarm opererad 1998"
REASON = "Stämmer inte med journalen, ring mottagningen"
NOTE = "Bekräftat vid telefonsamtal"


def _nurse(**kw):
    blob = {
        "user_guid": "dddddddd-0000-4000-8000-000000000001",
        "email": "ssk@example.test",
        "user_type": "professional",
        "is_su_admin": False,
        "session_phases": [],
        "affiliations": [{"affiliation_guid": "a1", "role": "nurse",
                          "care_unit_guid": UNIT, "role_guid": "r1"}],
    }
    blob.update(kw)
    return blob


def _items():
    return [
        {"guid": R1, "patient_guid": P1, "section": "allergy",
         "link_id": "allergy.substance", "label": "Vad är du allergisk mot?",
         "value": ANSWER_1, "unit": None, "recorded_at": "2026-11-20T09:00:00+00:00",
         "submitted_at": "2026-11-20T09:05:00+00:00", "review_status": "pending"},
        {"guid": R2, "patient_guid": P2, "section": "medication",
         "link_id": "medication.current", "label": "Vilka läkemedel tar du?",
         "value": ANSWER_2, "unit": None, "recorded_at": "2026-11-21T07:00:00+00:00",
         "submitted_at": "2026-11-21T07:01:00+00:00", "review_status": "pending"},
        {"guid": R3, "patient_guid": P1, "section": "history",
         "link_id": "history.surgery", "label": "Tidigare operationer",
         "value": ANSWER_3, "unit": None, "recorded_at": "2026-11-20T09:00:00+00:00",
         "submitted_at": "2026-11-20T09:05:00+00:00", "review_status": "pending"},
        {"guid": R4, "patient_guid": P2, "section": "vaccination",
         "link_id": "vaccination.doses", "label": "Antal doser",
         "value": 3, "unit": "st", "recorded_at": "2026-11-21T07:00:00+00:00",
         "submitted_at": "2026-11-21T07:01:00+00:00", "review_status": "pending"},
    ]


class _Resp:
    def __init__(self, status, body=None, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


_HTML_404 = _Resp(404, None, "<!doctype html><title>404 Not Found</title>")


class FakeUpstreams:
    """sso.pdhc's token validation + cdr1's review API (contract s4.2)."""

    def __init__(self, blob=None, items=None):
        self.blob = blob if blob is not None else _nurse()
        self.items = items if items is not None else _items()
        self.decided: dict[str, str] = {}
        self.list_response = None      # a _Resp, or an Exception to raise
        self.decision_response = None  # likewise, for the POSTs
        self.cdr_gets: list[dict] = []
        self.cdr_posts: list[dict] = []

    # -- the boundary ------------------------------------------------------
    def get(self, url, headers=None, params=None, timeout=None, **kw):
        headers = headers or {}
        if url == f"{SSO}/api/auth/me/service":
            if headers.get("Authorization") != f"Bearer {TOKEN}" or self.blob is None:
                return _Resp(401, {"error": "invalid"})
            return _Resp(200, self.blob)
        if url == f"{CDR}/api/v1/clinical/patients":
            return _Resp(200, {"patients": [
                {"patient_guid": P1, "name": "Testsson, Anna"}]})
        if url == f"{CDR}/api/v1/reviews":
            self.cdr_gets.append({"headers": dict(headers), "params": list(params or [])})
            refusal = self._credential_refusal(headers)
            if refusal:
                return refusal
            if isinstance(self.list_response, Exception):
                raise self.list_response
            if self.list_response is not None:
                return self.list_response
            pending = [i for i in self.items if i["guid"] not in self.decided]
            return _Resp(200, {"items": pending, "total": len(pending),
                               "limit": 200, "truncated": False})
        return _HTML_404

    def post(self, url, headers=None, json=None, timeout=None, **kw):
        headers = headers or {}
        m = re.fullmatch(re.escape(CDR) + r"/api/v1/reviews/([^/]+)/(approve|reject)", url)
        if not m:
            return _HTML_404
        guid, verb = m.groups()
        self.cdr_posts.append({"guid": guid, "verb": verb, "json": json,
                               "headers": dict(headers)})
        refusal = self._credential_refusal(headers)
        if refusal:
            return refusal
        if isinstance(self.decision_response, Exception):
            raise self.decision_response
        if self.decision_response is not None:
            return self.decision_response
        if guid not in {i["guid"] for i in self.items}:
            # as built: one flat 403 for "no such item" and "not your unit"
            return _Resp(403, {"error": "forbidden",
                               "message": "no such review item in your care units"})
        if guid in self.decided:
            return _Resp(409, {"error": "already_decided"})
        if verb == "reject":
            if not (isinstance(json, dict) and str(json.get("reason") or "").strip()):
                return _Resp(422, {"error": "reason_required", "message": "a rejection needs a reason"})
            self.decided[guid] = "rejected"
            return _Resp(200, {"review_status": "rejected"})
        self.decided[guid] = "endorsed"
        return _Resp(201, {"endorsement_guid": "eeeeeeee-0000-4000-8000-000000000001",
                           "amended": isinstance(json, dict) and "value" in json})

    @staticmethod
    def _credential_refusal(headers):
        """cdr1 takes a service key BEFORE a bearer, and dashboard's key is
        read-only there. A review call carrying one is not the operator's."""
        if "X-Service-Key" in headers or "X-Source-Service" in headers:
            return _Resp(403, {"error": "service_key_not_accepted"})
        if headers.get("Authorization") != f"Bearer {TOKEN}":
            return _Resp(401, {"error": "unauthenticated"})
        return None


@pytest.fixture
def up(monkeypatch):
    fake = FakeUpstreams()
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(requests, "post", fake.post)
    return fake


@pytest.fixture
def app():
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "test",
        "DATABASE_URL": "sqlite:///:memory:",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {
            "connect_args": {"check_same_thread": False},
            "poolclass": sqlalchemy.pool.StaticPool,
        },
        "AUTH_MODE": "sso",
        "SSO_BASE_URL": SSO,
        "SSO_CLIENT_ID": "cid",
        "SSO_CLIENT_SECRET": "secret",
        "SSO_CALLBACK_URL": "https://dashboard.test/auth/callback",
        "CDR1_BASE_URL": CDR,
        # Set on purpose: the review client must NOT send it.
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


def _csrf(client) -> str:
    html = client.get("/eps-granskning").get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "the queue page carries no CSRF token"
    return m.group(1)


def _confirm(client, guid, **form):
    form.setdefault("csrf_token", _csrf(client))
    return client.post(f"/eps-granskning/{guid}/bekrafta", data=form)


def _decide(client, guid, **form):
    form.setdefault("csrf_token", _csrf(client))
    form.setdefault("confirmed", "ja")
    return client.post(f"/eps-granskning/{guid}/beslut", data=form)


def _audit_rows(app):
    with app.app_context():
        return [
            {"route": r.route, "event": r.event_type, "user": r.user_guid,
             "patient": r.patient_guid, "status": r.response_status,
             "n": r.n_rows_returned, "payload": r.payload_snapshot or {}}
            for r in DashboardAudit.query.order_by(DashboardAudit.timestamp).all()]


# --------------------------------------------------------------------------
# the queue
# --------------------------------------------------------------------------

def test_queue_renders_items_grouped_per_patient(c, up):
    r = c.get("/eps-granskning")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    for answer in (ANSWER_1, ANSWER_2, ANSWER_3):
        assert answer in html
    for section in ("Allergier", "Läkemedel", "Tidigare sjukdomar", "Vaccinationer"):
        assert section in html
    # one group per patient, in cdr1's order of first appearance, and each
    # group holds that patient's items in cdr1's order
    groups = re.findall(r'<section class="card rv-group" data-patient="([^"]+)"(.*?)</section>',
                        html, re.S)
    assert [g[0] for g in groups] == [P1, P2]
    assert re.findall(r'data-review="([^"]+)"', groups[0][1]) == [R1, R3]
    assert re.findall(r'data-review="([^"]+)"', groups[1][1]) == [R2, R4]
    # marked as patient-reported with the patient-generated badge, once per item
    assert html.count('class="origin-badge"') == 4
    assert "patientrapporterat" in html
    # submitted time, in Swedish time (UTC+1 in November)
    assert "Inskickat 2026-11-20 10:05" in html
    assert "Testsson, Anna" in html
    assert 'id="rv-empty"' not in html and 'id="rv-problem"' not in html


def test_queue_is_read_with_the_operators_bearer_and_no_service_key(c, up):
    c.get("/eps-granskning")
    assert up.cdr_gets, "cdr1 was not asked"
    h = up.cdr_gets[-1]["headers"]
    assert h["Authorization"] == f"Bearer {TOKEN}"
    assert "X-Service-Key" not in h and "X-Source-Service" not in h
    assert ("status", "pending") in up.cdr_gets[-1]["params"]


def test_empty_queue_says_so(c, up):
    up.items = []
    r = c.get("/eps-granskning")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'id="rv-empty"' in html
    assert 'id="rv-problem"' not in html


def test_no_action_is_preselected_or_styled_as_recommended(c, up):
    """MDR: the page proposes no decision. The three actions share one class
    and the amend <select> for a yes/no answer starts on the blank option."""
    html = c.get("/eps-granskning").get_data(as_text=True)
    assert "btn-primary" not in html and "btn-success" not in html \
        and "btn-danger" not in html
    assert " open>" not in html and "autofocus" not in html


# --------------------------------------------------------------------------
# refusal is not an empty queue
# --------------------------------------------------------------------------

def test_cdr_403_renders_a_refusal_not_an_empty_queue(c, up):
    up.list_response = _Resp(403, {"error": "forbidden"})
    r = c.get("/eps-granskning")
    html = r.get_data(as_text=True)
    assert r.status_code == 403
    assert 'data-problem="forbidden"' in html
    assert 'id="rv-empty"' not in html
    assert "inga EPS-svar att granska" not in html


@pytest.mark.parametrize("response,code,status", [
    (_Resp(500, {"error": "boom"}), "unreachable", 502),
    (requests.ConnectionError("down"), "unreachable", 502),
    (_Resp(401, {"error": "unauthenticated"}), "cdr_unauthenticated", 502),
    (_HTML_404, "api_missing", 502),
    (_Resp(200, {"reviews": []}), "unreachable", 502),       # 200, wrong shape
    (_Resp(200, None, "<html>login</html>"), "unreachable", 502),
])
def test_a_failed_read_is_a_named_problem_not_an_empty_queue(c, up, response, code, status):
    up.list_response = response
    r = c.get("/eps-granskning")
    html = r.get_data(as_text=True)
    assert r.status_code == status
    assert f'data-problem="{code}"' in html
    assert 'id="rv-empty"' not in html


def test_unconfigured_cdr_is_named(app, up):
    app.config["CDR1_BASE_URL"] = ""
    client = app.test_client()
    with client.session_transaction() as s:
        s["sso_token"] = TOKEN
    r = client.get("/eps-granskning")
    assert r.status_code == 503
    assert 'data-problem="not_configured"' in r.get_data(as_text=True)
    assert up.cdr_gets == []


def test_count_for_the_nav_is_cdrs_number(c, up):
    r = c.get("/eps-granskning/antal")
    assert r.status_code == 200 and r.get_json() == {"count": 4}


def test_count_on_a_refusal_is_null_never_zero(c, up):
    up.list_response = _Resp(403, {"error": "forbidden"})
    r = c.get("/eps-granskning/antal")
    assert r.status_code == 403
    assert r.get_json() == {"count": None, "error": "forbidden"}


def test_nav_has_the_entry_point(c, up):
    html = c.get("/eps-granskning").get_data(as_text=True)
    assert 'href="/eps-granskning" id="eps-nav"' in html
    assert 'id="eps-nav-count" data-known="4"' in html
    # on a failed read the nav shows "?" rather than a number
    up.list_response = _Resp(500, {})
    html = c.get("/eps-granskning").get_data(as_text=True)
    assert 'id="eps-nav-count" data-known="?"' in html


# --------------------------------------------------------------------------
# decisions
# --------------------------------------------------------------------------

def test_confirmation_step_sends_nothing(c, up):
    r = _confirm(c, R1, action="approve")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'id="rv-confirm"' in html and ANSWER_1 in html
    assert "Inget har skickats ännu" in html
    assert up.cdr_posts == []


def test_approve_sends_the_request_and_the_item_leaves_the_queue(c, up):
    r = _decide(c, R1, action="approve")
    assert r.status_code == 303 and r.headers["Location"].endswith("/eps-granskning")
    assert len(up.cdr_posts) == 1
    p = up.cdr_posts[0]
    assert (p["guid"], p["verb"], p["json"]) == (R1, "approve", {})
    assert p["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert "X-Service-Key" not in p["headers"]
    html = c.get("/eps-granskning").get_data(as_text=True)
    assert f'data-review="{R1}"' not in html and f'data-review="{R3}"' in html
    assert "Svaret är godkänt" in html


def test_approve_with_a_note(c, up):
    _decide(c, R1, action="approve", note=NOTE)
    assert up.cdr_posts[0]["json"] == {"note": NOTE}


def test_amend_shows_the_original_beside_the_amended_value(c, up):
    r = _confirm(c, R1, action="amend", value="Penicillin V")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert re.search(r'id="rv-original">\s*' + re.escape(ANSWER_1), html)
    assert re.search(r'id="rv-amended">\s*Penicillin V', html)
    assert up.cdr_posts == []


def test_amend_and_approve_sends_the_amended_value(c, up):
    r = _decide(c, R1, action="amend", value="Penicillin V", note=NOTE)
    assert r.status_code == 303
    p = up.cdr_posts[0]
    assert (p["guid"], p["verb"]) == (R1, "approve")
    assert p["json"] == {"value": "Penicillin V", "note": NOTE}


def test_amending_a_number_sends_a_number(c, up):
    _decide(c, R4, action="amend", value="2")
    assert up.cdr_posts[0]["json"] == {"value": 2}


@pytest.mark.parametrize("guid,value", [
    (R1, ""), (R1, "   "), (R1, ANSWER_1),   # empty, blank, unchanged
    (R4, "tre"), (R4, "3"),                  # not a number, unchanged
])
def test_an_invalid_amendment_is_stopped(c, up, guid, value):
    for call in (_confirm, _decide):
        r = call(c, guid, action="amend", value=value)
        assert r.status_code == 400
        assert 'id="rv-form-error"' in r.get_data(as_text=True)
    assert up.cdr_posts == []


def test_reject_sends_the_reason(c, up):
    r = _decide(c, R2, action="reject", reason=REASON)
    assert r.status_code == 303
    p = up.cdr_posts[0]
    assert (p["guid"], p["verb"], p["json"]) == (R2, "reject", {"reason": REASON})
    html = c.get("/eps-granskning").get_data(as_text=True)
    assert f'data-review="{R2}"' not in html


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_reject_without_a_reason_is_stopped(c, up, reason):
    form = {"action": "reject"}
    if reason is not None:
        form["reason"] = reason
    r1 = _confirm(c, R2, **form)
    r2 = _decide(c, R2, **form)
    for r in (r1, r2):
        assert r.status_code == 400
        assert "Ett skäl krävs" in r.get_data(as_text=True)
    assert up.cdr_posts == [], "a reject without a reason reached cdr1"


def test_a_decision_that_was_not_confirmed_is_not_sent(c, up):
    r = _decide(c, R1, action="approve", confirmed="")
    assert r.status_code == 400
    assert up.cdr_posts == []


def test_an_unknown_action_is_stopped(c, up):
    r = _decide(c, R1, action="auto")
    assert r.status_code == 400
    assert up.cdr_posts == []


def test_409_tells_the_user_someone_else_decided(c, up):
    up.decision_response = _Resp(409, {"error": "already_decided"})
    r = _decide(c, R1, action="approve")
    assert r.status_code == 303
    assert len(up.cdr_posts) == 1
    up.decided[R1] = "endorsed"           # as it is, on cdr1
    html = c.get("/eps-granskning").get_data(as_text=True)
    assert "Någon annan har redan beslutat" in html
    assert "Svaret är godkänt" not in html
    assert f'data-review="{R1}"' not in html


def test_an_item_decided_before_the_click_is_reported_not_sent(c, up):
    token = _csrf(c)
    up.decided[R1] = "endorsed"
    r = _decide(c, R1, action="approve", csrf_token=token)
    assert r.status_code == 303 and up.cdr_posts == []
    assert "finns inte längre i din kö" in c.get("/eps-granskning").get_data(as_text=True)


def test_cdr_403_on_a_decision_is_a_refusal_page(c, up):
    """An off-unit admin may list and may not decide (contract s4.2)."""
    up.decision_response = _Resp(403, {"error": "forbidden"})
    r = _decide(c, R1, action="approve")
    html = r.get_data(as_text=True)
    assert r.status_code == 403
    assert "inget har registrerats" in html
    assert "Svaret är godkänt" not in html


def test_cdr_422_is_shown_in_swedish(c, up):
    up.decision_response = _Resp(422, {
        "error": "validation", "code": "wrong_type",
        "message": "the original is a date; the amended value must be an ISO date"})
    r = _decide(c, R1, action="amend", value="x")
    html = r.get_data(as_text=True)
    assert r.status_code == 422
    assert "fel typ" in html and "inget har registrerats" in html
    assert "the original is a date" not in html
    assert up.decided == {}


def test_an_unknown_cdr_422_code_is_shown_as_is(c, up):
    up.decision_response = _Resp(422, {"error": "value_not_allowed"})
    r = _decide(c, R1, action="amend", value="x")
    assert r.status_code == 422 and "value_not_allowed" in r.get_data(as_text=True)


def test_a_json_404_on_a_decision_is_reported(c, up):
    """The contract's 404; cdr1 as built answers a flat 403 instead."""
    up.decision_response = _Resp(404, {"error": "not_found"})
    r = _decide(c, R1, action="approve")
    assert r.status_code == 303
    assert "finns inte inom dina vårdenheter" in c.get("/eps-granskning").get_data(as_text=True)


def test_an_item_the_caller_may_not_decide_has_no_actions(c, up):
    """cdr1 marks each item `can_decide`. False = shown, with no buttons."""
    for it in up.items:
        it["can_decide"] = it["guid"] != R3
    html = c.get("/eps-granskning").get_data(as_text=True)
    blocks = dict(re.findall(r'<div class="rv-item" data-review="([^"]+)">(.*?)(?=<div class="rv-item"|</section>)',
                             html, re.S))
    assert "rv-nodecide" in blocks[R3] and "<form" not in blocks[R3]
    assert ANSWER_3 in blocks[R3]
    assert "rv-nodecide" not in blocks[R1] and blocks[R1].count("<form") == 3


def test_a_truncated_list_says_more_exist(c, up):
    up.list_response = _Resp(200, {"items": _items()[:2], "total": 7,
                                   "limit": 2, "truncated": True})
    html = c.get("/eps-granskning").get_data(as_text=True)
    assert "CDR anger 7 totalt, 2 visas här" in html
    assert c.get("/eps-granskning/antal").get_json() == {"count": 7}


def _admin(**kw):
    return _nurse(is_su_admin=True, affiliations=[], **kw)


def test_an_admin_must_attest_before_the_queue_is_read(app, c, up):
    """An SU admin lists every unit: break-glass, as in the picker."""
    up.blob = _admin()
    r = c.get("/eps-granskning")
    html = r.get_data(as_text=True)
    assert r.status_code == 428
    assert 'id="rv-attest"' in html and ANSWER_1 not in html
    assert up.cdr_gets == []
    r = c.post("/admin-read-attest", data={"reason": "Kontroll av kön",
                                            "next": "/eps-granskning"})
    assert r.status_code == 302
    r = c.get("/eps-granskning")
    assert r.status_code == 200 and ANSWER_1 in r.get_data(as_text=True)
    rows = [x for x in _audit_rows(app) if x["event"] == "eps_review_queue"]
    assert [x["payload"]["outcome"] for x in rows] == ["admin_attest_required", "listed"]


def test_an_unanswered_decision_is_reported_as_unknown(c, up):
    up.decision_response = requests.Timeout("slow")
    r = _decide(c, R1, action="approve")
    assert r.status_code == 502
    assert "OKÄNT" in r.get_data(as_text=True)


# --------------------------------------------------------------------------
# the access gate and CSRF
# --------------------------------------------------------------------------

OUTSIDE = {
    "a patient": {"user_guid": "p", "user_type": "patient", "is_su_admin": False,
                  "patient_link": {"ips_patient_guid": P1}},
    "a professional with no care unit": _nurse(affiliations=[]),
    "an analysis-phase professional with no care unit":
        _nurse(affiliations=[], session_phases=["analysis"]),
}


@pytest.mark.parametrize("who", sorted(OUTSIDE))
def test_a_user_outside_the_gate_is_refused(c, up, who):
    up.blob = OUTSIDE[who]
    assert c.get("/eps-granskning").status_code == 403
    assert c.get("/eps-granskning/antal").status_code == 403
    for path in (f"/eps-granskning/{R1}/bekrafta", f"/eps-granskning/{R1}/beslut"):
        r = c.post(path, data={"action": "approve", "confirmed": "ja"})
        assert r.status_code == 403
    assert up.cdr_gets == [] and up.cdr_posts == [], "cdr1 was called for a refused user"


def test_a_nurse_without_the_analysis_phase_is_admitted(c, up):
    """The queue is a care-delivery path. Unregistered it would fall to the
    analysis-phase gate and refuse the people it is for."""
    assert up.blob["session_phases"] == []
    assert c.get("/eps-granskning").status_code == 200


def test_must_change_password_is_bounced_to_sso(c, up):
    up.blob = _nurse(must_change_password=True)
    r = c.get("/eps-granskning")
    assert r.status_code == 302 and r.headers["Location"] == f"{SSO}/change-password"
    r = c.post(f"/eps-granskning/{R1}/beslut", data={"action": "approve"})
    assert r.status_code == 302
    assert up.cdr_gets == [] and up.cdr_posts == []


def test_no_session_is_sent_to_login(app, up):
    r = app.test_client().get("/eps-granskning")
    assert r.status_code == 302 and "/auth/login" in r.headers["Location"]
    assert up.cdr_gets == []


def test_a_revoked_token_is_sent_to_login(c, up):
    """The blob is validated per request, never cached."""
    assert c.get("/eps-granskning").status_code == 200
    up.blob = None
    r = c.get("/eps-granskning")
    assert r.status_code == 302 and "/auth/login" in r.headers["Location"]


@pytest.mark.parametrize("token", [None, "", "wrong-token"])
@pytest.mark.parametrize("step", ["bekrafta", "beslut"])
def test_csrf_missing_or_wrong_is_refused(c, up, token, step):
    _csrf(c)                                   # a token exists in the session
    gets_before = len(up.cdr_gets)
    data = {"action": "approve", "confirmed": "ja"}
    if token is not None:
        data["csrf_token"] = token
    r = c.post(f"/eps-granskning/{R1}/{step}", data=data)
    assert r.status_code == 403
    assert "CSRF" in r.get_data(as_text=True)
    assert up.cdr_posts == [] and len(up.cdr_gets) == gets_before


def test_csrf_token_in_the_header_is_accepted(c, up):
    token = _csrf(c)
    r = c.post(f"/eps-granskning/{R1}/beslut", headers={"X-CSRF-Token": token},
               data={"action": "approve", "confirmed": "ja"})
    assert r.status_code == 303 and len(up.cdr_posts) == 1


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------

def test_opening_the_queue_is_audited(app, c, up):
    c.get("/eps-granskning")
    rows = [r for r in _audit_rows(app) if r["event"] == "eps_review_queue"]
    assert len(rows) == 1
    row = rows[0]
    assert row["user"] == up.blob["user_guid"]
    assert (row["route"], row["status"], row["n"]) == ("GET /eps-granskning", 200, 4)
    assert row["payload"]["outcome"] == "listed"


def test_a_refused_read_is_audited_as_refused(app, c, up):
    up.list_response = _Resp(403, {"error": "forbidden"})
    c.get("/eps-granskning")
    rows = [r for r in _audit_rows(app) if r["event"] == "eps_review_queue"]
    assert [(r["status"], r["n"], r["payload"].get("outcome")) for r in rows] == \
        [(403, 0, "forbidden")]


@pytest.mark.parametrize("form,event", [
    ({"action": "approve", "note": NOTE}, "eps_review_approve"),
    ({"action": "amend", "value": "Penicillin V", "note": NOTE}, "eps_review_amend"),
    ({"action": "reject", "reason": REASON}, "eps_review_reject"),
])
def test_a_decision_is_audited_without_the_answer_text(app, c, up, form, event):
    _confirm(c, R1, **form)
    _decide(c, R1, **form)
    rows = _audit_rows(app)
    sent = [r for r in rows if r["event"] == event]
    assert len(sent) == 1
    row = sent[0]
    assert row["user"] == up.blob["user_guid"]
    assert row["patient"] == P1
    assert row["route"] == "POST /eps-granskning/<review_guid>/beslut"
    assert row["payload"]["review_guid"] == R1
    assert row["payload"]["outcome"] == "sent"
    # no answer, amended value, note or reason in ANY audit row of the flow
    dump = json.dumps(rows, ensure_ascii=False, default=str)
    for secret in (ANSWER_1, ANSWER_2, ANSWER_3, "Penicillin V", NOTE, REASON):
        assert secret not in dump
    # the review guid is never mistaken for a patient guid
    assert all(r["patient"] != R1 for r in rows)


def test_a_409_is_audited_as_already_decided(app, c, up):
    up.decision_response = _Resp(409, {"error": "already_decided"})
    _decide(c, R1, action="approve")
    rows = [r for r in _audit_rows(app) if r["event"] == "eps_review_approve"]
    assert [(r["payload"].get("outcome"), r["payload"].get("cdr_status"))
            for r in rows] == [("already_decided", 409)]


def test_a_csrf_refusal_is_audited(app, c, up):
    c.post(f"/eps-granskning/{R1}/beslut", data={"action": "approve", "confirmed": "ja"})
    rows = [r for r in _audit_rows(app) if r["event"] == "eps_review_decision"]
    assert [(r["status"], r["payload"].get("outcome")) for r in rows] == \
        [(403, "csrf_refused")]
