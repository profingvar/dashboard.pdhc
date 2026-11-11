"""GET /dashboard/events/<observation_guid> — one event's provenance (#983).

The route has NO patient guid, so the order of operations is the security
design: basis first (refused before cdr1 is asked), fetch scoped to the
caller's orgs, gate on the observation's OWN patient, then spärr — a blocked
point gets the same 404 as a missing one.

Gate tests use a real non-admin blob through `__wrapped__` (inside the audit
wrapper), not `AUTH_MODE: off`, whose blob is a dev SU admin.
"""
from __future__ import annotations

import pytest
import sqlalchemy
from flask import g

import app.routes.dashboard as dash
import app.services.cdr1_client as cdrmod
from app import create_app
from app.models import db
from app.services.cdr1_client import (
    Cdr1Client, Cdr1ProjectionTooOld, Cdr1Unreachable)
from app.services.ips_client import Block


def _app():
    app = create_app({
        "TESTING": True,
        "DATABASE_URL": "sqlite:///:memory:",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {
            "connect_args": {"check_same_thread": False},
            "poolclass": sqlalchemy.pool.StaticPool,
        },
        "AUTH_MODE": "off",
    })
    with app.app_context():
        db.create_all()
    return app


def _detail(patient="p1", context=None, raw=None, found=True):
    return {
        "observation_guid": "o1", "patient_guid": patient,
        "code": "urn:pdhc:concept/11111111-1111-1111-1111-111111111111",
        "at": "2026-04-12T10:00:00+00:00", "received_at": "2026-04-12T10:05:00+00:00",
        "value": 71.5, "unit": "kg", "value_string": None, "org_guid": "org-a",
        "ingest_raw_guid": "ing-1",
        "context": context if context is not None else {
            "plan_definition_guid": None, "contract_guid": "ct-1",
            "provider_org_guid": "prov-1", "service_request_guid": None,
            "care_plan_guid": None, "transaction_guid": "txn",
            "author_org_guid": None, "requesting_org_guid": None},
        "context_found": found, "context_filled_from_raw": [],
        "raw_fhir": raw if raw is not None else {"resourceType": "Observation"},
    }


class _Cdr1:
    base_url = "https://cdr.example"

    def __init__(self, detail=None, raise_=None, patients=None):
        self._d, self._raise = detail, raise_
        self._patients = patients if patients is not None else [{"guid": "p1"}]
        self.detail_calls = 0

    def observation_detail(self, guid, orgs, **kw):
        self.detail_calls += 1
        if self._raise:
            raise self._raise
        return self._d

    def list_org_patients(self, orgs, *, is_admin=False, reason=None):
        return self._patients


def _wire(monkeypatch, cdr1, blocks=None):
    monkeypatch.setattr(dash.cdr1_client, "build_client", lambda: cdr1)
    monkeypatch.setattr(dash.ips_client, "is_configured", lambda: True)
    monkeypatch.setattr(dash.ips_client, "get_active_blocks",
                        lambda *a, **k: blocks or [])


def _get(app, guid="o1"):
    with app.test_client() as c:
        r = c.get(f"/dashboard/events/{guid}")
        return r.status_code, r.get_json()


NON_ADMIN = {"user_guid": "u1", "user_type": "professional",
             "affiliations": [{"affiliation_guid": "a1", "role": "physician",
                               "care_unit_guid": "org-a"}]}


# --------------------------------------------------------------------------
# the client's two kinds of 404
# --------------------------------------------------------------------------

class _Resp:
    def __init__(self, status, body=None, html=False):
        self.status_code, self._body, self._html = status, body, html

    def json(self):
        if self._html:
            raise ValueError("html")
        return self._body


@pytest.fixture
def ctx():
    from flask import Flask
    with Flask(__name__).test_request_context("/"):
        yield


def test_cdr1s_OWN_not_found_is_an_answer(ctx, monkeypatch):
    monkeypatch.setattr(cdrmod.requests, "get", lambda *a, **k: _Resp(
        404, {"error": "not_found", "message": "x"}))
    c = Cdr1Client(base_url="https://cdr.example", token="t")
    assert c.observation_detail("o1", ["org-a"]) is None


def test_an_ABSENT_route_is_too_old_not_not_found(ctx, monkeypatch):
    """A cdr1 that predates #983 has no such route and answers Flask's HTML
    404. Reading that as "not found" would make every detail view say so,
    silently, until the deploy."""
    monkeypatch.setattr(cdrmod.requests, "get", lambda *a, **k: _Resp(404, html=True))
    c = Cdr1Client(base_url="https://cdr.example", token="t")
    with pytest.raises(Cdr1ProjectionTooOld):
        c.observation_detail("o1", ["org-a"])


# --------------------------------------------------------------------------
# the route
# --------------------------------------------------------------------------

def test_a_found_event_carries_every_provenance_field(monkeypatch):
    app = _app()
    raw = {"resourceType": "Observation",
           "referenceRange": [{"low": {"value": 60}, "high": {"value": 80}}],
           "interpretation": [{"coding": [{"code": "N"}]}],
           "method": {"text": "scale"}, "device": {"display": "Scale X"}}
    _wire(monkeypatch, _Cdr1(_detail(raw=raw)))
    status, body = _get(app)
    assert status == 200
    d = body["detail"]
    assert d["row"]["observationGuid"] == "o1"
    assert d["contractGuid"] == "ct-1"
    assert d["referenceLow"] == 60.0 and d["referenceHigh"] == 80.0
    assert d["interpretation"] == "N"
    assert d["method"] == "scale" and d["device"] == "Scale X"
    # every cdr1 context field, verbatim, nulls included
    assert "author_org_guid" in body["context"]
    assert body["context"]["care_plan_guid"] is None


def test_the_patients_org_is_NOT_filled_from_the_provider(monkeypatch):
    """Different organisations with different meanings (#774)."""
    app = _app()
    _wire(monkeypatch, _Cdr1(_detail()))
    _, body = _get(app)
    assert body["detail"]["patientOrgGuid"] is None
    assert body["detail"]["providerOrgGuid"] == "prov-1"


def test_not_found_is_a_flat_404(monkeypatch):
    app = _app()
    _wire(monkeypatch, _Cdr1(None))
    status, body = _get(app)
    assert status == 404 and body == dash._NOT_FOUND


def test_a_SPAERR_hidden_point_gets_the_SAME_404_as_a_missing_one(monkeypatch):
    """The existence of a blocked datapoint must not be disclosed."""
    app = _app()
    block = Block.from_dict({"guid": "b1", "patient_guid": "p1",
                             "source_scope_type": "clinic",
                             "source_scope_id": "org-a", "is_active": True})
    _wire(monkeypatch, _Cdr1(_detail()), blocks=[block])
    monkeypatch.setattr(dash.ips_client, "filter_blocked_points",
                        lambda pts, blocks: ([], []))
    hidden_status, hidden = _get(app)
    _wire(monkeypatch, _Cdr1(None))
    missing_status, missing = _get(app)
    assert hidden_status == missing_status == 404
    assert hidden == missing


def test_a_pre_983_cdr1_is_a_named_503(monkeypatch):
    app = _app()
    _wire(monkeypatch, _Cdr1(raise_=Cdr1ProjectionTooOld("old")))
    status, body = _get(app)
    assert status == 503 and body["ticket"] == "#983"


def test_an_unreachable_cdr1_is_a_503_not_a_404(monkeypatch):
    app = _app()
    _wire(monkeypatch, _Cdr1(raise_=Cdr1Unreachable("down", 503)))
    status, body = _get(app)
    assert status == 503 and body["error"] == "cdr1_unreachable"


def test_NO_BASIS_is_refused_BEFORE_cdr1_is_asked(monkeypatch):
    app = _app()
    cdr1 = _Cdr1(_detail())
    _wire(monkeypatch, cdr1)
    with app.test_request_context("/dashboard/events/o1"):
        g.access_blob = {"user_guid": "u9", "user_type": "patient"}
        resp, status = dash.event_detail.__wrapped__(observation_guid="o1")
    assert status == 403 and resp.get_json()["error"] == "no_basis"
    assert cdr1.detail_calls == 0, "cdr1 was asked before the basis was checked"


def test_the_gate_uses_the_OBSERVATIONS_patient_not_the_callers_word(monkeypatch):
    """cdr1 returns an observation of patient p-other; the caller's
    organisations do not hold that patient, so the gate refuses — on the
    patient the DATA names, which the caller never got to choose."""
    app = _app()
    cdr1 = _Cdr1(_detail(patient="p-other"), patients=[{"guid": "p1"}])
    _wire(monkeypatch, cdr1)
    with app.test_request_context("/dashboard/events/o1"):
        g.access_blob = NON_ADMIN
        resp, status = dash.event_detail.__wrapped__(observation_guid="o1")
        assert getattr(g, "_audit_patient_guid", None) == "p-other"
    assert status == 403
    assert resp.get_json()["error"] == "other_organisation"
