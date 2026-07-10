import uuid
from datetime import datetime, timezone, timedelta
import sqlalchemy
from app import create_app
from app.models import db, User, OrgMembership, ObservationCache


def _app():
    app = create_app({
        "TESTING": True,
        # Hermetic per-test in-memory DB (#441). StaticPool is required:
        # bare sqlite :memory: gives each connection a private db, so
        # seeded rows would be invisible to request-handling connections.
        # create_app overwrites SQLALCHEMY_DATABASE_URI from its DATABASE_URL
        # config key, so set both — otherwise an ambient DATABASE_URL env
        # var would silently re-point the test at a real Postgres.
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


def _in_org_client(app, orgs):
    """Test client whose caller is an in-org (non-admin) professional.

    The AUTH_MODE=off dev SU carries ``organization_ids: []``, so since
    #212 every /patient/<guid> read is an admin *off-org* read and renders
    the override-confirmation form instead of the patient dashboard. Give
    the caller a blob scoped to the seeded org(s) instead — the hook runs
    after the auth request-loader, so it overrides the dev blob."""
    from app.auth import _blob_to_user
    blob = {
        "user_guid": str(uuid.uuid4()),
        "email": "prof@test",
        "user_type": "professional",
        "is_su_admin": False,
        "effective_phases": ["analysis"],
        "organization_ids": list(orgs),
    }

    @app.before_request
    def _override():
        from flask import g
        g.access_blob = blob
        g.current_user = _blob_to_user(blob)

    return app.test_client()


def test_landing_lists_patients():
    app = _app()
    org, pat, cg = _seed(app)
    try:
        c = _in_org_client(app, [org])
        r = c.get("/")
        assert r.status_code == 200
        assert pat[:12].encode() in r.data
        # The landing table renders guid / observation count / latest —
        # concept names are not shown (stale pre-workspace assertion on
        # "B-glucose" removed, #441). The seeded patient has 3 rows.
        assert b"<td>3</td>" in r.data
    finally:
        with app.app_context():
            _cleanup(org)


def test_patient_view():
    app = _app()
    org, pat, cg = _seed(app)
    try:
        c = _in_org_client(app, [org])
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
