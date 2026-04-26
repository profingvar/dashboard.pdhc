import os
import uuid
from datetime import datetime, timezone, timedelta
from app import create_app
from app.models import db, User, OrgMembership, ObservationCache


def _app():
    return create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": os.environ.get("DATABASE_URL"),
        "AUTH_MODE": "off",
    })


def _seed(app):
    with app.app_context():
        org = str(uuid.uuid4())
        pat = str(uuid.uuid4())
        cg = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        for i in range(3):
            db.session.add(ObservationCache(
                source_obs_guid=str(uuid.uuid4()),
                patient_guid=pat, org_guid=org,
                concept_guid=cg, concept_name="B-glucose",
                value=5.0 + i * 0.2, unit="mmol/L",
                observed_at=now - timedelta(days=i),
            ))
        db.session.commit()
        return org, pat, cg


def _cleanup(org):
    ObservationCache.query.filter_by(org_guid=org).delete()
    db.session.commit()


def test_landing_lists_patients():
    app = _app()
    org, pat, cg = _seed(app)
    try:
        c = app.test_client()
        r = c.get("/")
        assert r.status_code == 200
        assert pat[:12].encode() in r.data
        assert b"B-glucose" in r.data
    finally:
        with app.app_context():
            _cleanup(org)


def test_patient_view():
    app = _app()
    org, pat, cg = _seed(app)
    try:
        c = app.test_client()
        r = c.get(f"/patient/{pat}")
        assert r.status_code == 200
        assert b"Patient dashboard" in r.data
        r404 = c.get(f"/patient/{uuid.uuid4()}")
        assert r404.status_code == 404
    finally:
        with app.app_context():
            _cleanup(org)


def test_api_series_bundle():
    app = _app()
    org, pat, cg = _seed(app)
    try:
        c = app.test_client()
        r = c.get(f"/api/v1/series?patient={pat}&concept={cg}")
        assert r.status_code == 200
        j = r.get_json()
        assert j["resourceType"] == "Bundle"
        assert j["total"] == 3
        assert all(e["resource"]["resourceType"] == "Observation" for e in j["entry"])
    finally:
        with app.app_context():
            _cleanup(org)


def test_capability_statement():
    app = _app()
    c = app.test_client()
    r = c.get("/metadata")
    assert r.status_code == 200
    j = r.get_json()
    assert j["resourceType"] == "CapabilityStatement"
    assert j["fhirVersion"] == "5.0.0"


def test_api_series_requires_args():
    app = _app()
    c = app.test_client()
    assert c.get("/api/v1/series").status_code == 400
