"""Legacy entry-point redirects + FHIR metadata (#471 item 1).

The ObservationCache landing/patient views and /api/v1/series were retired
(Q6 = live CDR1 reads only). ``/`` and ``/patient/<guid>`` now redirect to the
CDR1-backed picker/charts; /metadata still serves a minimal CapabilityStatement.
"""
import sqlalchemy

from app import create_app
from app.models import db


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


def test_landing_redirects_to_select():
    r = _app().test_client().get("/")
    assert r.status_code == 302
    assert "/select" in r.headers["Location"]


def test_patient_redirects_to_charts():
    guid = "11111111-1111-1111-1111-111111111111"
    r = _app().test_client().get(f"/patient/{guid}")
    assert r.status_code == 302
    assert f"/patient/{guid}/charts" in r.headers["Location"]


def test_series_endpoint_gone():
    # The legacy ObservationCache series API was retired.
    assert _app().test_client().get("/api/v1/series?patient=x&concept=y").status_code == 404


def test_capability_statement():
    r = _app().test_client().get("/metadata")
    assert r.status_code == 200
    j = r.get_json()
    assert j["resourceType"] == "CapabilityStatement"
    assert j["fhirVersion"] == "5.0.0"
