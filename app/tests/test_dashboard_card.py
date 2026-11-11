"""The patient card: the live euIPS summary (#980) and the two diagnosis
registers shown side by side, labelled, never merged (#984)."""
from __future__ import annotations

import pytest
import requests
import sqlalchemy
from flask import Flask

import app.routes.dashboard as dash
import app.services.ips_client as ipsmod
from app import create_app
from app.models import db
from app.services.ips_client import IpsClient, IpsUnreachable

SNOMED = "http://snomed.info/sct"
MMS = "http://id.who.int/icd/release/11/mms"


def _app():
    app = create_app({
        "TESTING": True, "DATABASE_URL": "sqlite:///:memory:",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {"connect_args": {"check_same_thread": False},
                                      "poolclass": sqlalchemy.pool.StaticPool},
        "AUTH_MODE": "off",
    })
    with app.app_context():
        db.create_all()
    return app


def _summary():
    return {"source": "ips", "sections": [
        {"key": "allergies", "title": "Allergies", "loinc": "48765-2",
         "status": "EXPLICITLY_ABSENT", "entries": [],
         "absent": {"code": "no-known-allergies", "narrative_text": "No known allergies"}},
        {"key": "problems", "title": "Problems", "loinc": "11450-4",
         "status": "PRESENT", "absent": None, "entries": [
             {"display": "Heart failure", "code": "84114007", "system": SNOMED,
              "date": "2024-01-01", "status": "active", "source": "ips"},
             {"display": "Type 2 diabetes mellitus", "code": "44054006",
              "system": SNOMED, "date": None, "status": "active", "source": "ips"}]},
        {"key": "medications", "title": "Medications", "loinc": "10160-0",
         "status": "MISSING", "entries": [], "absent": None},
    ]}


class _Diag:
    def __init__(self, code, display):
        self.code, self.display = code, display


class _Problems:
    is_definitively_empty = False

    def patient_card_fields(self):
        from app.dashboard.dto import Diagnosis
        return {"diagnoses": (Diagnosis(code="5A11", display="Diabetes mellitus typ 2",
                                        clinical_status="active",
                                        verification_status="confirmed"),),
                "diagnosis_scope_note": None}


class _Cdr1:
    base_url = "https://cdr.example"

    def list_org_patients(self, orgs, *, is_admin=False, reason=None):
        return [{"guid": "p1"}]


def _wire(monkeypatch, summary=None, ips_raises=None, icd_ok=True):
    import app.services.icd11_client as icd
    monkeypatch.setattr(dash.cdr1_client, "build_client", lambda: _Cdr1())
    monkeypatch.setattr(dash.ips_client, "is_configured", lambda: True)
    monkeypatch.setattr(dash.ips_client, "get_active_blocks", lambda *a, **k: [])

    class _C:
        def problem_list(self, guid, **kw):
            if not icd_ok:
                raise RuntimeError("icd11 down")
            return _Problems()
    monkeypatch.setattr(icd, "build_client", lambda: _C())

    class _Ips:
        def euips_summary(self, guid):
            if ips_raises:
                raise ips_raises
            return summary
    monkeypatch.setattr(dash.ips_client, "_default_client", lambda: _Ips())


def _card(app):
    with app.test_client() as c:
        return c.get("/dashboard/patients/p1").get_json()


# --------------------------------------------------------------------------
# the client
# --------------------------------------------------------------------------

def test_the_method_lives_ON_IpsClient_not_as_a_loose_function():
    """It was inserted by indentation before the next class; this proves where
    it landed."""
    assert callable(getattr(IpsClient, "euips_summary", None))
    assert not hasattr(ipsmod, "euips_summary")


class _R:
    def __init__(self, status, body=None, raw=False):
        self.status_code, self._b, self._raw = status, body, raw

    def json(self):
        if self._raw:
            raise ValueError
        return self._b


@pytest.fixture
def ctx():
    with Flask(__name__).test_request_context("/"):
        yield


def _client():
    return IpsClient(base_url="https://ips.example", token="t")


def test_a_404_is_an_ANSWER(ctx, monkeypatch):
    monkeypatch.setattr(ipsmod.requests, "get", lambda *a, **k: _R(404, {"error": "x"}))
    assert _client().euips_summary("p1") is None


def test_ips_scope_undetermined_503_is_NOT_an_empty_summary(ctx, monkeypatch):
    monkeypatch.setattr(ipsmod.requests, "get",
                        lambda *a, **k: _R(503, {"error": "scope_undetermined"}))
    with pytest.raises(IpsUnreachable):
        _client().euips_summary("p1")


def test_unconfigured_and_malformed_and_network_all_RAISE(ctx, monkeypatch):
    with pytest.raises(IpsUnreachable):
        IpsClient(base_url="", token="t").euips_summary("p1")
    monkeypatch.setattr(ipsmod.requests, "get", lambda *a, **k: _R(200, {"no": "sections"}))
    with pytest.raises(IpsUnreachable):
        _client().euips_summary("p1")

    def boom(*a, **k):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(ipsmod.requests, "get", boom)
    with pytest.raises(IpsUnreachable):
        _client().euips_summary("p1")


def test_it_sends_the_operators_bearer(ctx, monkeypatch):
    """ips's require_auth reads ONLY Authorization (#783)."""
    seen = {}

    def fake(url, headers=None, timeout=None):
        seen["h"] = headers
        seen["url"] = url
        return _R(200, _summary())
    monkeypatch.setattr(ipsmod.requests, "get", fake)
    _client().euips_summary("p1")
    assert seen["h"]["Authorization"] == "Bearer t"
    assert seen["url"].endswith("/api/v1/patients/p1/euips-summary")


# --------------------------------------------------------------------------
# the card
# --------------------------------------------------------------------------

def test_the_live_summary_reaches_the_card(monkeypatch):
    app = _app()
    _wire(monkeypatch, _summary())
    body = _card(app)
    secs = {s["sectionCode"]: s for s in body["patientCard"]["epsSections"]}
    problems = secs["11450-4"]
    assert [e["display"] for e in problems["entries"]] == [
        "Heart failure", "Type 2 diabetes mellitus"]
    assert all(e["source"] == "ips" and e["system"] == SNOMED
               for e in problems["entries"])
    assert not any(u.startswith("eps:") for u in body["unavailable"])


def test_ABSENT_and_MISSING_are_different_on_the_card(monkeypatch):
    """"No known allergies" is a clinical statement; MISSING is the absence
    of any statement. The card must be able to tell them apart."""
    app = _app()
    _wire(monkeypatch, _summary())
    secs = {s["sectionCode"]: s for s in _card(app)["patientCard"]["epsSections"]}
    assert secs["48765-2"]["absentAssertion"] is True
    assert secs["48765-2"]["narrative"] == "No known allergies"
    assert secs["10160-0"]["absentAssertion"] is False
    assert secs["10160-0"]["narrative"] is None
    assert secs["10160-0"]["entries"] == []


def test_BOTH_registers_are_listed_LABELLED_and_NOT_reconciled(monkeypatch):
    app = _app()
    _wire(monkeypatch, _summary())
    body = _card(app)
    src = {s["source"]: s for s in body["diagnosisSources"]}
    assert src["icd11"]["label"] == "ICD-11-register"
    assert src["icd11"]["system"] == MMS and src["icd11"]["count"] == 1
    assert src["ips"]["label"] == "Patientsammanfattning (SNOMED CT)"
    assert src["ips"]["system"] == SNOMED and src["ips"]["count"] == 2
    assert body["diagnosisSourcesReconciled"] is False


def test_the_two_registers_are_NEVER_MERGED(monkeypatch):
    """The chips are icd11's alone. Two SNOMED problems from ips must not
    become chips, or two code systems would be presented as one list."""
    app = _app()
    _wire(monkeypatch, _summary())
    diagnoses = _card(app)["patientCard"]["diagnoses"]
    assert [d["code"] for d in diagnoses] == ["5A11"]
    assert all(d["system"] == MMS for d in diagnoses)


def test_an_unreachable_ips_degrades_ONLY_the_summary(monkeypatch):
    app = _app()
    _wire(monkeypatch, ips_raises=IpsUnreachable("down"))
    body = _card(app)
    assert body["patientCard"]["epsSections"] == []
    assert any(u.startswith("eps:unreachable") for u in body["unavailable"])
    assert [d["code"] for d in body["patientCard"]["diagnoses"]] == ["5A11"]
    src = {s["source"]: s for s in body["diagnosisSources"]}
    assert src["ips"]["available"] is False and src["ips"]["count"] is None
    assert src["icd11"]["available"] is True


def test_a_patient_ips_does_not_know_is_said_so(monkeypatch):
    app = _app()
    _wire(monkeypatch, summary=None)
    body = _card(app)
    assert "eps:patient-not-in-ips-scope" in body["unavailable"]


def test_an_unreachable_icd11_leaves_the_summary_standing(monkeypatch):
    app = _app()
    _wire(monkeypatch, _summary(), icd_ok=False)
    body = _card(app)
    assert body["patientCard"]["diagnoses"] == []
    src = {s["source"]: s for s in body["diagnosisSources"]}
    assert src["icd11"]["available"] is False
    assert src["ips"]["count"] == 2
