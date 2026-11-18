"""Nurse view reads observations from EVERY CDR (2026-11-18).

Simulated and provider-delivered data land in a CDR other than the one
holding the Patient resource, or in a CDR with no Patient resource at all
(sim writes none). The summary used to read only the owning CDR's
$everything and answered 404 without a Patient, so such data never showed.
The AGP now also counts plan.pdhc's CGM concept, not only LOINC 41653-7.
"""
from app.analyse.federation import FanoutResult, FanoutResponse
import app.routes.nurse as nurse
from app.tests.test_nurse_sparr import _app, _obs_entry, _PID

CGM_CONCEPT = "urn:pdhc:concept/22d0f6c6-2438-4637-ad4e-9d85431b8edb"


def _fr(body, cdr_id, ok=True):
    return FanoutResult(cdr_id=cdr_id, base_url="http://x", region_label=cdr_id,
                        ok=ok, status_code=200 if ok else 500, body=body,
                        elapsed_ms=1, error=None if ok else "500")


def _resp(results):
    ok = [r.cdr_id for r in results if r.ok]
    bad = [r.cdr_id for r in results if not r.ok]
    return FanoutResponse(mode="complete" if not bad else "degraded",
                          results=results, succeeded=ok, failed=bad)


def _fake(patient_in, obs_by_cdr, calls=None):
    def fake(*a, **k):
        path = k.get("path", "")
        if calls is not None:
            calls.append((path, dict(k.get("params") or {})))
        if path.startswith("api/v1/fhir/Patient/") and "$everything" not in path:
            return _resp([_fr({"resourceType": "Patient", "id": _PID} if c in patient_in else None,
                              c, ok=c in patient_in) for c in ("cdr1", "cdr2", "cdr3")])
        if "$everything" in path:
            return _resp([_fr({"entry": []}, k["cdr_ids"][0])])
        return _resp([_fr({"entry": obs_by_cdr.get(c, [])}, c) for c in ("cdr1", "cdr2", "cdr3")])
    return fake


def test_summary_shows_values_held_in_a_cdr_other_than_the_owner(monkeypatch):
    app = _app()
    monkeypatch.setattr(nurse, "get_active_blocks", lambda g: [])
    monkeypatch.setattr(nurse, "fanout", _fake({"cdr1"}, {
        "cdr2": [_obs_entry(concept=CGM_CONCEPT, value=7.1, at="2026-11-17T08:00:00Z")]}))
    body = app.test_client().get(f"/api/nurse/patient/{_PID}").get_json()
    assert body["owner_cdr"] == "cdr1"
    assert body["observation_cdrs"] == ["cdr2"]
    assert [v["value"] for v in body["latest_values"]] == [7.1]


def test_summary_without_any_patient_resource_but_with_observations(monkeypatch):
    app = _app()
    monkeypatch.setattr(nurse, "get_active_blocks", lambda g: [])
    monkeypatch.setattr(nurse, "fanout", _fake(set(), {
        "cdr2": [_obs_entry(concept=CGM_CONCEPT, value=8.0)]}))
    r = app.test_client().get(f"/api/nurse/patient/{_PID}")
    assert r.status_code == 200
    body = r.get_json()
    assert body["patient_record_found"] is False
    assert body["owner_cdr"] is None
    assert body["patient"] == {"resourceType": "Patient", "id": _PID}
    assert len(body["latest_values"]) == 1


def test_summary_still_404_with_no_patient_and_no_visible_observation(monkeypatch):
    app = _app()
    monkeypatch.setattr(nurse, "get_active_blocks", lambda g: [])
    monkeypatch.setattr(nurse, "fanout", _fake(set(), {}))
    assert app.test_client().get(f"/api/nurse/patient/{_PID}").status_code == 404


def test_agp_asks_for_every_cgm_code_and_merges_per_cdr(monkeypatch):
    app = _app()
    monkeypatch.setattr(nurse, "get_active_blocks", lambda g: [])
    calls = []
    obs = [_obs_entry(concept=CGM_CONCEPT, value=v, at=f"2026-11-1{i}T08:00:00Z")
           for i, v in enumerate([6.0, 7.0, 8.0], start=1)]
    monkeypatch.setattr(nurse, "fanout", _fake(set(), {"cdr2": obs}, calls))
    body = app.test_client().get(f"/api/nurse/patient/{_PID}/agp?window=90d").get_json()
    codes = [p["code"] for path, p in calls if path == "api/v1/fhir/Observation"]
    assert codes == list(nurse.DEFAULT_CGM_CODES)
    # the fake answers every code with the same 3 entries; merged per CDR = 6.
    assert body["summary"]["n"] == 6
    assert body["succeeded_cdrs"] == ["cdr1", "cdr2", "cdr3"]


def test_a_cdr_failing_one_code_is_reported_failed(monkeypatch):
    app = _app()
    monkeypatch.setattr(nurse, "get_active_blocks", lambda g: [])
    n = {"i": 0}

    def fake(*a, **k):
        n["i"] += 1
        return _resp([_fr({"entry": []}, "cdr1"),
                      _fr(None, "cdr2", ok=(n["i"] == 1))])
    monkeypatch.setattr(nurse, "fanout", fake)
    body = app.test_client().get(f"/api/nurse/patient/{_PID}/agp").get_json()
    assert body["failed_cdrs"] == ["cdr2"]
    assert body["fanout_mode"] == "degraded"


def test_nurse_cgm_codes_override(monkeypatch):
    app = _app()
    app.config["NURSE_CGM_CODES"] = " a|1 , b|2 "
    with app.app_context():
        assert nurse._cgm_codes() == ["a|1", "b|2"]
