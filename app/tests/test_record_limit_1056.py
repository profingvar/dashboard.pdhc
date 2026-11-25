"""#1056: record.pdhc asks dashboard for ONE observation's limit verdict, on
behalf of the professional who opened record's vidimering worklist (plan D7:
record makes no judgement of its own).

Through the real request loader with AUTH_MODE=sso. Patched: the outbound
HTTP boundary (`requests.get`) answering as sso.pdhc and request.pdhc (the
CarePlans, which request.pdhc gives only to the professional's own bearer as
X-API-Key and refuses to a service key), and cdr1/eps through the same
client-level fakes the other limit-route tests use. Synthetic data only.
"""
from __future__ import annotations

import json

import pytest
import requests
import sqlalchemy

import app.routes.dashboard as dash
from app import create_app
from app.models import DashboardAudit, db
from app.tests.test_dashboard_events import CODE, _wire

SSO, REQ = "https://sso.test", "https://request.test"
TOKEN = "tok-professional-1056"
KEY = "record-dashboard-key-1056"
UNIT = "cccccccc-1056-4000-8000-00000000000a"
TX = "tx-glu"
P = "aaaaaaaa-1056-4000-8000-000000000001"
PATH = "/dashboard/record/observations/obs-1/limit"
RECORD = {"X-Source-Service": "record.pdhc", "X-Service-Key": KEY,
          "Authorization": f"Bearer {TOKEN}"}


def _blob(**kw):
    b = {"user_guid": "dddddddd-1056-4000-8000-000000000001", "email": "lak@example.test",
         "user_type": "professional", "is_su_admin": False, "session_phases": [],
         "affiliations": [{"role": "doctor", "care_unit_guid": UNIT,
                           "care_unit_name": "Vårdcentralen Test"}]}
    b.update(kw)
    return b


def _care_plan(lo="3.0", hi="10.0"):
    return {"care_plan_guid": "cp-1", "status": "active", "patient_guid": P, "goals": [],
            "plan_definition_snapshot": {"title": "Diabetes", "version": "3", "activities": [
                {"guid": "act", "transactions": [
                    {"guid": TX, "concept_guid": "concept-glu", "range_min": lo,
                     "range_max": hi, "concept_unit_name": "mmol/L"}]}]}}


class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body
        self.text = json.dumps(body)
        self.headers = {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class _Http:
    def __init__(self):
        self.blob = _blob()
        self.care_plans = [_care_plan()]
        self.request_down = False
        self.request_calls = []

    def get(self, url, headers=None, params=None, timeout=None, **kw):
        headers = headers or {}
        if url == f"{SSO}/api/auth/me/service":
            if headers.get("Authorization") != f"Bearer {TOKEN}":
                return _Resp(401, {"error": "invalid"})
            return _Resp(200, self.blob)
        if url == f"{REQ}/api/v1/careplans":
            self.request_calls.append(dict(headers))
            if "X-Service-Key" in headers:
                return _Resp(403, {"error": "service keys are not accepted"})
            if headers.get("X-API-Key") != TOKEN:
                return _Resp(401, {"error": "unauthenticated"})
            if self.request_down:
                return _Resp(503, {"error": "down"})
            return _Resp(200, {"careplans": self.care_plans})
        raise AssertionError(f"unexpected GET {url}")


class _Cdr1:
    base_url = "https://cdr.example"

    def __init__(self, value=12.0, version=1):
        self.value, self.version = value, version
        self.found = True

    def list_org_patients(self, orgs, *, is_admin=False, reason=None):
        return [{"guid": P}] if UNIT in orgs else []

    def observation_detail(self, guid, orgs, **kw):
        if not self.found or UNIT not in orgs:
            return None
        return {"patient_guid": P, "observation_guid": guid, "code": CODE,
                "version": self.version, "status": "final",
                "at": "2026-11-20T08:00:00Z", "org_guid": "org-perf", "value": self.value,
                "unit": "mmol/L", "ingest_raw_guid": "i", "received_at": None,
                "context_found": True, "context": {"transaction_guid": TX}}


@pytest.fixture
def world(monkeypatch):
    app = create_app({
        "TESTING": True, "DATABASE_URL": "sqlite:///:memory:",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {"connect_args": {"check_same_thread": False},
                                      "poolclass": sqlalchemy.pool.StaticPool},
        "AUTH_MODE": "sso", "SSO_BASE_URL": SSO, "SSO_CLIENT_ID": "cid",
        "SSO_CLIENT_SECRET": "sec", "REQUEST_BASE_URL": REQ, "RECORD_DASHBOARD_KEY": KEY})
    with app.app_context():
        db.create_all()
    http, cdr = _Http(), _Cdr1()
    monkeypatch.setattr(requests, "get", http.get)
    _wire(monkeypatch, cdr)

    class W:
        pass
    w = W()
    w.app, w.http, w.cdr = app, http, cdr
    return w


def _get(w, path=PATH, headers=RECORD, method="get"):
    with w.app.test_client() as c:
        r = getattr(c, method)(path, headers=headers)
        return r.status_code, r.get_json(silent=True), r


def test_record_on_behalf_of_the_professional_gets_the_verdict_and_version(world):
    status, body, r = _get(world)
    assert status == 200, body
    assert body["limit"]["status"] == "above" and body["limit"]["high"] == 10.0
    assert body["limit"]["sourceKind"] == "care_plan_range"
    assert (body["version"], body["value"], body["patient_guid"]) == (1, 12.0, P)
    # the CarePlans were read with the PROFESSIONAL's bearer, not a key
    assert world.http.request_calls and all(
        h.get("X-API-Key") == TOKEN and "X-Service-Key" not in h
        for h in world.http.request_calls)
    assert "Set-Cookie" not in r.headers          # no session minted for a service call
    with world.app.app_context():
        rows = DashboardAudit.query.filter(DashboardAudit.route.contains("record")).all()
        assert [(r.user_guid, r.response_status) for r in rows] == [
            ("dddddddd-1056-4000-8000-000000000001", 200)]   # logged as the PERSON


def test_inside_is_inside(world):
    world.cdr.value = 6.0
    assert _get(world)[1]["limit"]["status"] == "inside"


def test_unreadable_care_plans_are_unknown_never_no_limit(world):
    world.http.request_down = True
    body = _get(world)[1]
    assert body["limit"]["status"] == "unknown"
    assert any(u.startswith("limits:") for u in body["unavailable"])


@pytest.mark.parametrize("headers", [
    dict(RECORD, **{"X-Service-Key": "wrong"}),
    {k: v for k, v in RECORD.items() if k != "Authorization"},       # key alone
    {k: v for k, v in RECORD.items() if k != "X-Service-Key"},       # bearer alone
    dict(RECORD, Authorization="Basic abc"),
])
def test_record_needs_its_key_AND_a_bearer(world, headers):
    assert _get(world, headers=headers)[0] == 403


def test_an_unset_key_admits_nobody(world):
    world.app.config["RECORD_DASHBOARD_KEY"] = ""
    assert _get(world, headers=dict(RECORD, **{"X-Service-Key": ""}))[0] == 403
    assert _get(world)[0] == 403


@pytest.mark.parametrize("path", ["/dashboard/events/obs-1", "/dashboard/patients/" + P + "/events",
                                  "/loggkontroll/" + P])
def test_record_is_confined_to_its_prefix(world, path):
    assert _get(world, path=path)[0] == 403


def test_record_cannot_post(world):
    assert _get(world, method="post")[0] == 403


def test_a_bearer_sso_refuses_is_401(world):
    status, _, _ = _get(world, headers=dict(RECORD, Authorization="Bearer someone-else"))
    assert status == 401


def test_must_change_password_is_refused(world):
    world.http.blob = _blob(must_change_password=True)
    assert _get(world)[0] == 403


def test_a_professional_at_another_unit_gets_the_same_404(world):
    world.http.blob = _blob(affiliations=[{"role": "doctor", "care_unit_guid":
                                           "cccccccc-1056-4000-8000-0000000000bb"}])
    status, body, _ = _get(world)
    assert status == 404 and body["error"] == "not_found"


def test_a_blocked_point_is_the_same_404(world, monkeypatch):
    class B:
        pass
    monkeypatch.setattr(dash, "sparr_state", lambda p: type("S", (), {"blocks": [B()],
                                                                       "checked": True})())
    monkeypatch.setattr(dash.ips_client, "filter_blocked_points", lambda pts, blocks: ([], []))
    status, body, _ = _get(world)
    assert status == 404 and body["error"] == "not_found"
