"""Nurse single-patient endpoints — care-delivery re-gate + spärr (#546).

The nurse views were folded from the analyse engine into cd-assist's
care-delivery surface: gated via ``/api/nurse`` being a clinical path
(``app.auth._is_clinical_path``) and spärr-filtered per patient exactly like
``routes/charts.py::series``. The federation never opens a socket here —
``nurse.fanout`` is patched with a programmable fake; spärr is exercised by
patching ``nurse.get_active_blocks``.
"""
import sqlalchemy

from app import create_app
from app.models import db
from app.services.ips_client import Block
from app.analyse.federation import FanoutResult, FanoutResponse
import app.routes.nurse as nurse


_PID = "11111111-1111-1111-1111-111111111111"
_ORG = "clinic-aaaa"
_CONCEPT_URI = "urn:pdhc:concept/concept-aaaa"


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


# --- fanout fakes ---------------------------------------------------------

def _fr(body, *, cdr_id="cdr1", ok=True):
    return FanoutResult(
        cdr_id=cdr_id, base_url="http://x", region_label="R",
        ok=ok, status_code=200 if ok else 500, body=body, elapsed_ms=1,
        error=None if ok else "500",
    )


def _resp(results):
    succeeded = [r.cdr_id for r in results if r.ok]
    failed = [r.cdr_id for r in results if not r.ok]
    mode = "complete" if not failed else ("degraded" if succeeded else "error")
    return FanoutResponse(mode=mode, results=results,
                          succeeded=succeeded, failed=failed)


def _obs_entry(concept=_CONCEPT_URI, value=6.5, at="2026-06-15T08:00:00Z", org=_ORG):
    # code_canonical prod form is urn:pdhc:concept/<guid>; the coding system+code
    # recombine to that via nurse._coding_uri (system rstrip('/') + '/' + code).
    system, _, code = concept.rpartition("/")
    return {"resource": {
        "resourceType": "Observation",
        "effectiveDateTime": at,
        "valueQuantity": {"value": value, "unit": "mmol/L"},
        "code": {"coding": [{"system": system, "code": code, "display": "Glucose"}]},
        "performer": [{"identifier": {"value": org}}],
    }}


def _block(scope=_ORG, *, lift_kind=None, lift_concepts=None,
           lift_from=None, lift_until=None):
    return Block(
        guid="b1", patient_guid=_PID, source_scope_type="clinic",
        source_scope_id=scope, is_active=True, lift_kind=lift_kind,
        lift_concept_guids=lift_concepts, lift_from_date=lift_from,
        lift_until_date=lift_until,
    )


# --- variable -------------------------------------------------------------

def test_variable_returns_points(monkeypatch):
    app = _app()
    monkeypatch.setattr(nurse, "get_active_blocks", lambda g: [])
    bundle = {"entry": [_obs_entry(value=6.0, at="2026-06-01T08:00:00Z"),
                        _obs_entry(value=7.0, at="2026-06-02T08:00:00Z")]}
    monkeypatch.setattr(nurse, "fanout", lambda *a, **k: _resp([_fr(bundle)]))
    r = app.test_client().get(
        f"/api/nurse/patient/{_PID}/variable/urn:pdhc:concept|conceptX")
    assert r.status_code == 200
    body = r.get_json()
    assert body["n_raw"] == 2
    assert body["has_blocked_sources"] is False
    assert [p["value"] for p in body["points"]] == [6.0, 7.0]


def test_variable_coarse_sparr_hides_blocked(monkeypatch):
    app = _app()
    monkeypatch.setattr(nurse, "get_active_blocks", lambda g: [_block(_ORG)])
    bundle = {"entry": [_obs_entry(org=_ORG), _obs_entry(org="other-clinic")]}
    monkeypatch.setattr(nurse, "fanout", lambda *a, **k: _resp([_fr(bundle)]))
    body = app.test_client().get(
        f"/api/nurse/patient/{_PID}/variable/urn:pdhc:concept|c").get_json()
    # lift off → every point from the blocked clinic dropped; other survives.
    assert body["n_raw"] == 1
    assert body["has_blocked_sources"] is True


def test_variable_lift_exposes_covered_concept(monkeypatch):
    app = _app()
    app.config["SPARR_LIFT_ENABLED"] = True
    monkeypatch.setattr(
        nurse, "get_active_blocks",
        lambda g: [_block(_ORG, lift_kind="indispensable_care",
                          lift_concepts=["concept-aaaa"],
                          lift_from="2026-01-01", lift_until="2026-12-31")],
    )
    bundle = {"entry": [_obs_entry(concept=_CONCEPT_URI, org=_ORG),
                        _obs_entry(concept="urn:pdhc:concept/concept-other", org=_ORG)]}
    monkeypatch.setattr(nurse, "fanout", lambda *a, **k: _resp([_fr(bundle)]))
    body = app.test_client().get(
        f"/api/nurse/patient/{_PID}/variable/urn:pdhc:concept|c").get_json()
    # lift exposes only the covered concept; the uncovered one stays hidden.
    assert body["n_raw"] == 1


# --- summary --------------------------------------------------------------

def test_summary_owner_and_latest_values(monkeypatch):
    app = _app()
    monkeypatch.setattr(nurse, "get_active_blocks", lambda g: [])
    patient_body = {"resourceType": "Patient", "id": _PID}
    everything = {"entry": [
        _obs_entry(value=5.5, at="2026-06-01T08:00:00Z"),
        _obs_entry(value=9.9, at="2026-06-10T08:00:00Z"),
        {"resource": {"resourceType": "Condition",
                      "code": {"coding": [{"system": "urn:snomed",
                                           "code": "44054006", "display": "T2DM"}]},
                      "onsetDateTime": "2020-01-01"}},
    ]}

    def fake_fanout(*a, **k):
        if "$everything" in k.get("path", ""):
            return _resp([_fr(everything)])
        return _resp([_fr(patient_body)])

    monkeypatch.setattr(nurse, "fanout", fake_fanout)
    body = app.test_client().get(f"/api/nurse/patient/{_PID}").get_json()
    assert body["owner_cdr"] == "cdr1"
    assert len(body["conditions"]) == 1
    assert len(body["latest_values"]) == 1
    assert body["latest_values"][0]["value"] == 9.9
    assert body["has_blocked_sources"] is False


def test_summary_404_when_no_owner(monkeypatch):
    app = _app()
    monkeypatch.setattr(nurse, "get_active_blocks", lambda g: [])
    monkeypatch.setattr(nurse, "fanout",
                        lambda *a, **k: _resp([_fr(None, ok=False)]))
    r = app.test_client().get(f"/api/nurse/patient/{_PID}")
    assert r.status_code == 404


def test_summary_sparr_hides_blocked_obs(monkeypatch):
    app = _app()
    monkeypatch.setattr(nurse, "get_active_blocks", lambda g: [_block(_ORG)])
    patient_body = {"resourceType": "Patient", "id": _PID}
    everything = {"entry": [_obs_entry(org=_ORG, value=5.5)]}

    def fake_fanout(*a, **k):
        if "$everything" in k.get("path", ""):
            return _resp([_fr(everything)])
        return _resp([_fr(patient_body)])

    monkeypatch.setattr(nurse, "fanout", fake_fanout)
    body = app.test_client().get(f"/api/nurse/patient/{_PID}").get_json()
    assert body["latest_values"] == []
    assert body["has_blocked_sources"] is True


# --- agp ------------------------------------------------------------------

def test_agp_sparr_drops_before_aggregation(monkeypatch):
    app = _app()
    monkeypatch.setattr(nurse, "get_active_blocks", lambda g: [_block(_ORG)])
    bundle = {"resourceType": "Bundle",
              "entry": [_obs_entry(org=_ORG, value=v,
                                   at=f"2026-06-0{i}T0{i}:00:00Z")
                        for i, v in enumerate([5.0, 6.0], start=1)]}
    monkeypatch.setattr(nurse, "fanout", lambda *a, **k: _resp([_fr(bundle)]))
    body = app.test_client().get(f"/api/nurse/patient/{_PID}/agp").get_json()
    # all CGM points came from the blocked clinic → aggregation sees nothing.
    assert body["summary"]["n"] == 0
    assert body["has_blocked_sources"] is True


# --- events ---------------------------------------------------------------

def test_events_strip_transient_key_and_hide_blocked_hypo(monkeypatch):
    app = _app()
    monkeypatch.setattr(nurse, "get_active_blocks", lambda g: [_block(_ORG)])
    enc_bundle = {"entry": [{"resource": {
        "resourceType": "Encounter",
        "period": {"start": "2026-05-01T10:00:00Z", "end": "2026-05-01T11:00:00Z"},
        "class": {"coding": [{"code": "AMB"}]},
    }}]}
    hypo_bundle = {"entry": [_obs_entry(concept="urn:pdhc:concept/hypo",
                                        value=2, org=_ORG)]}

    def fake_fanout(*a, **k):
        if "Encounter" in k.get("path", ""):
            return _resp([_fr(enc_bundle)])
        return _resp([_fr(hypo_bundle)])

    monkeypatch.setattr(nurse, "fanout", fake_fanout)
    body = app.test_client().get(f"/api/nurse/patient/{_PID}/events").get_json()
    # the encounter (no org) survives; the hypo from the blocked clinic is hidden.
    kinds = sorted(e["kind"] for e in body["events"])
    assert kinds == ["encounter"]
    assert body["has_blocked_sources"] is True
    assert all("org_guid" not in e for e in body["events"])


# --- gate -----------------------------------------------------------------

def test_gate_rejects_when_sso_and_no_bearer():
    app = create_app({
        "TESTING": True,
        "DATABASE_URL": "sqlite:///:memory:",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {
            "connect_args": {"check_same_thread": False},
            "poolclass": sqlalchemy.pool.StaticPool,
        },
        "AUTH_MODE": "sso",
    })
    with app.app_context():
        db.create_all()
    r = app.test_client().get(f"/api/nurse/patient/{_PID}/events")
    # care-delivery clinical path with no session/bearer → not admitted (401/403/302).
    assert r.status_code in (401, 403, 302)
