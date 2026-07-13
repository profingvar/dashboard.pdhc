"""Patient picker + CDR1 client (#465 / #462 D3).

Covers the pure FHIR-bundle parsing and the /select route with a
monkeypatched CDR1 client (no network). The live CDR1 read + care-delivery
consent bypass are CDR-side (#468) and smoke-tested separately.
"""
import sqlalchemy

from app import create_app
from app.models import db
from app.services.cdr1_client import (
    Cdr1Client, parse_patient_bundle, parse_clinical_patients,
)
import app.routes.picker as picker


def _app(config=None):
    cfg = {
        "TESTING": True,
        "DATABASE_URL": "sqlite:///:memory:",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {
            "connect_args": {"check_same_thread": False},
            "poolclass": sqlalchemy.pool.StaticPool,
        },
    }
    if config:
        cfg.update(config)
    app = create_app(cfg)
    with app.app_context():
        db.create_all()
    return app


def test_parse_patient_bundle():
    bundle = {
        "resourceType": "Bundle",
        "entry": [
            {"resource": {
                "resourceType": "Patient", "id": "p1",
                "name": [{"family": "Ek", "given": ["Anna"]}],
                "birthDate": "1980-01-02",
            }},
            {"resource": {
                "resourceType": "Patient", "id": "p2",
                "name": [{"text": "Bo Berg"}],
            }},
            # non-Patient + id-less entries are skipped
            {"resource": {"resourceType": "Observation", "id": "o1"}},
            {"resource": {"resourceType": "Patient"}},
        ],
    }
    out = parse_patient_bundle(bundle)
    assert [p["guid"] for p in out] == ["p1", "p2"]
    assert out[0]["name"] == "Anna Ek"
    assert out[0]["birth_date"] == "1980-01-02"
    assert out[1]["name"] == "Bo Berg"


def test_parse_clinical_patients():
    body = {"patients": [
        {"patient_guid": "p1", "name": "Anna Ek", "birth_date": "1980-01-02",
         "observation_count": 4, "last_observed_at": "2026-04-12T10:00:00+00:00"},
        {"patient_guid": "p2", "name": "", "birth_date": None,
         "observation_count": 2, "last_observed_at": None},
        {"name": "no guid — skipped"},
    ]}
    out = parse_clinical_patients(body)
    assert [p["guid"] for p in out] == ["p1", "p2"]
    assert out[0]["observation_count"] == 4
    assert out[0]["birth_date"] == "1980-01-02"


def test_client_no_base_url_returns_empty():
    # Unconfigured CDR1 → empty, never raises (local dev / tests).
    app = _app()
    with app.app_context():
        assert Cdr1Client(base_url="").list_org_patients(["org1"]) == []


def test_client_non_admin_no_orgs_sees_nobody():
    app = _app()
    with app.app_context():
        c = Cdr1Client(base_url="https://cdr.pdhc.se")
        assert c.list_org_patients([], is_admin=False) == []


def test_select_route_lists_and_scopes(monkeypatch):
    app = _app()
    captured = {}

    class FakeClient:
        base_url = "https://cdr.pdhc.se"

        def list_org_patients(self, orgs, *, is_admin=False):
            captured["orgs"] = orgs
            captured["is_admin"] = is_admin
            return [
                {"guid": "p-zeta", "name": "Zeta", "birth_date": None},
                {"guid": "p-alpha", "name": "Alpha", "birth_date": "1975-05-05"},
            ]

    monkeypatch.setattr(picker, "build_client", lambda: FakeClient())

    r = app.test_client().get("/select")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    # both patients rendered, sorted by name (Alpha before Zeta)
    assert html.index("p-alpha") < html.index("p-zeta")
    assert "Alpha" in html and "Zeta" in html
    # dev SU is admin → no org restriction forwarded
    assert captured["is_admin"] is True
    assert captured["orgs"] == []


def test_select_route_unconfigured_banner(monkeypatch):
    app = _app()

    class FakeClient:
        base_url = ""

        def list_org_patients(self, orgs, *, is_admin=False):
            return []

    monkeypatch.setattr(picker, "build_client", lambda: FakeClient())
    r = app.test_client().get("/select")
    assert r.status_code == 200
    assert "CDR1_BASE_URL" in r.get_data(as_text=True)
