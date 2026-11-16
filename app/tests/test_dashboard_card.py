"""The patient card: the live euIPS summary (#980), whose problems section
IS the diagnosis chips since #984 (the CDR is the record, the EPS derived)."""
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


def test_the_chips_ARE_the_EPS_problems_section(monkeypatch):
    """#984 (operator, 2026-11-16): diagnoses are recorded in the CDR, the EPS
    is derived from it, and the card reads the EPS. One source — not the two
    side-by-side registers of 2026-11-11."""
    app = _app()
    _wire(monkeypatch, _summary())
    body = _card(app)
    diagnoses = body["patientCard"]["diagnoses"]
    assert [(d["code"], d["system"]) for d in diagnoses] == [("84114007", SNOMED), ("44054006", SNOMED)]
    assert "diagnosisSources" not in body and "diagnosisSourcesReconciled" not in body
    assert body["diagnosisSource"]["source"] == "eps"
    assert body["diagnosisSource"]["count"] == 2


def test_icd11_is_NOT_read_by_the_card(monkeypatch):
    """icd11's diagnoses reach the chips through the CDR, not by a side read."""
    app = _app()
    _wire(monkeypatch, _summary(), icd_ok=False)   # icd11 down must not matter
    body = _card(app)
    assert [d["code"] for d in body["patientCard"]["diagnoses"]] == ["84114007", "44054006"]
    assert not any(u.startswith("icd11") for u in body["unavailable"])


def test_each_chip_says_whether_a_CDR_row_backs_it(monkeypatch):
    s = _summary()
    probs = next(x for x in s["sections"] if x["key"] == "problems")
    probs["entries"][0].update(cdr_backed=True, cdr={"name": "cdr2", "guid": "c1"}, onset="2019-05-01")
    probs["entries"][1].update(cdr_backed=False, cdr=None)
    app = _app()
    _wire(monkeypatch, s)
    body = _card(app)
    d0, d1 = body["patientCard"]["diagnoses"]
    assert (d0["cdrBacked"], d0["cdr"], d0["onset"]) == (True, "cdr2", "2019-05-01")
    assert (d1["cdrBacked"], d1["cdr"]) == (False, None)
    assert body["diagnosisSource"]["cdrBacked"] == 1


def test_an_unreachable_ips_makes_the_diagnoses_UNKNOWN_not_empty(monkeypatch):
    app = _app()
    _wire(monkeypatch, ips_raises=IpsUnreachable("down"))
    body = _card(app)
    assert body["patientCard"]["epsSections"] == []
    assert body["patientCard"]["diagnoses"] == []
    assert any(u.startswith("eps:unreachable") for u in body["unavailable"])
    assert body["diagnosesDefinitivelyEmpty"] is None
    assert body["diagnosisSource"]["available"] is False


def test_a_patient_ips_does_not_know_is_said_so(monkeypatch):
    app = _app()
    _wire(monkeypatch, summary=None)
    body = _card(app)
    assert "eps:patient-not-in-ips-scope" in body["unavailable"]


def test_an_EXPLICITLY_ABSENT_problems_section_is_definitively_empty(monkeypatch):
    s = _summary()
    probs = next(x for x in s["sections"] if x["key"] == "problems")
    probs.update(status="EXPLICITLY_ABSENT", entries=[])
    app = _app()
    _wire(monkeypatch, s)
    body = _card(app)
    assert body["patientCard"]["diagnoses"] == [] and body["diagnosesDefinitivelyEmpty"] is True


def test_a_MISSING_problems_section_is_not_stated_and_says_so(monkeypatch):
    s = _summary()
    probs = next(x for x in s["sections"] if x["key"] == "problems")
    probs.update(status="MISSING", entries=[])
    app = _app()
    _wire(monkeypatch, s)
    body = _card(app)
    assert body["diagnosesDefinitivelyEmpty"] is False
    assert body["patientCard"]["diagnosisScopeNote"] == "Patientsammanfattningen anger inte om patienten har problem."
