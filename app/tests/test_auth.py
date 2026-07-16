"""SSO + AUTH_MODE=off tests. Mirrors gateway.pdhc/tests/test_sso_auth.py pattern."""
import uuid
from datetime import datetime, timezone
from unittest.mock import patch
import pytest
import sqlalchemy
from app import create_app
from app.models import db, User, ObservationCache
from app.auth import scope_to_user_orgs, has_analysis_access, _blob_to_user
from flask import g
from flask import session as flask_session


@pytest.fixture(autouse=True)
def _echo_revalidation():
    """Ticket #435 (analog of gateway.pdhc #424). The SSO request loader
    (``app.auth`` line ~247) re-validates the bearer token against SSO on
    *every* request (no caching, so an SSO-side logout takes effect
    immediately). That means a directly-set ``session['access_blob']`` is
    not trusted on its own — the re-validation call to the unreachable test
    SSO returns None and the loader 302-redirects before the phase gate.
    Patch ``app.auth.validate_sso_token`` to echo back whatever blob the
    test 'logged in' via ``_login_as`` (stored in the session), simulating a
    successful re-validation. Tests that exercise the no-token path
    (``test_sso_unauth_redirects_to_login``) are unaffected: the loader
    returns before calling ``validate_sso_token``.
    """
    def _echo(_token):
        try:
            return flask_session.get("access_blob")
        except RuntimeError:  # outside a request context
            return None
    with patch("app.auth.validate_sso_token", side_effect=_echo):
        yield


def _app(auth_mode="off"):
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "test",
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
        "AUTH_MODE": auth_mode,
        "SSO_BASE_URL": "https://sso.pdhc.se",
        "SSO_CLIENT_ID": "test-cid",
        "SSO_CLIENT_SECRET": "test-secret",
        "SSO_CALLBACK_URL": "https://dashboard.pdhc.se/auth/callback",
    })
    with app.app_context():
        db.create_all()
    return app


def _login_as(client, blob, token="test-token"):
    with client.session_transaction() as sess:
        sess["sso_token"] = token
        sess["access_blob"] = blob


def _mk_obs(org):
    return ObservationCache(
        source_obs_guid=str(uuid.uuid4()),
        patient_guid=str(uuid.uuid4()),
        org_guid=org,
        concept_guid=str(uuid.uuid4()),
        concept_name="X",
        value=1.0, unit="u",
        observed_at=datetime.now(timezone.utc),
    )


# ---------- AUTH_MODE=off ----------

def test_auth_off_loads_dev_user():
    app = _app("off")
    c = app.test_client()
    r = c.get("/healthz")
    assert r.status_code == 200
    # any protected route in off-mode resolves the dev SU; '/' now redirects
    # to the CDR1 picker (#471 item 1).
    r = c.get("/", follow_redirects=True)
    assert r.status_code == 200


# ---------- phase gate ----------

def test_phase_gate_admin_passes():
    blob = {"is_su_admin": True, "user_type": "professional", "effective_phases": []}
    assert has_analysis_access(blob)


def test_phase_gate_analysis_passes():
    blob = {"is_su_admin": False, "user_type": "professional", "effective_phases": ["analysis"]}
    assert has_analysis_access(blob)


def test_phase_gate_other_phase_denied():
    blob = {"is_su_admin": False, "user_type": "professional", "effective_phases": ["planning"]}
    assert not has_analysis_access(blob)


def test_phase_gate_non_professional_denied():
    blob = {"is_su_admin": False, "user_type": "patient", "effective_phases": ["analysis"]}
    assert not has_analysis_access(blob)


# ---------- SSO mode: redirect to /auth/login when no session ----------

def test_sso_unauth_redirects_to_login():
    app = _app("sso")
    c = app.test_client()
    r = c.get("/")
    assert r.status_code == 302
    assert "/auth/login" in r.location


def test_sso_authed_session_passes_phase_gate():
    app = _app("sso")
    c = app.test_client()
    uid = str(uuid.uuid4())
    blob = {
        "user_guid": uid,
        "email": "u@example.com",
        "user_type": "professional",
        "is_su_admin": False,
        "effective_phases": ["analysis"],
        "organization_ids": [str(uuid.uuid4())],
    }
    # Seed the User the blob refers to — the analysis dashboard "/" records a
    # refresh_log row FK'd to users.guid, which otherwise FK-violates (ticket
    # #435: separate from the revalidation-mock fix, needed for a green 200).
    with app.app_context():
        db.session.add(User(guid=uid, username="u@example.com", is_admin=False, is_su=False))
        db.session.commit()
    _login_as(c, blob)
    # '/' redirects to /select (both care-delivery gated); an affiliated/
    # org-scoped professional passes and lands on the picker (200).
    r = c.get("/", follow_redirects=True)
    assert r.status_code == 200


def test_sso_authed_without_phase_403():
    app = _app("sso")
    c = app.test_client()
    blob = {
        "user_guid": str(uuid.uuid4()),
        "user_type": "professional",
        "is_su_admin": False,
        "effective_phases": ["planning"],
        "organization_ids": [],
    }
    _login_as(c, blob)
    r = c.get("/")
    assert r.status_code == 403


# ---------- org scoping uses blob organization_ids ----------

def test_org_scoping_filters_non_admin():
    app = _app("off")
    with app.app_context():
        org_a = str(uuid.uuid4())
        org_b = str(uuid.uuid4())
        db.session.add_all([_mk_obs(org_a), _mk_obs(org_b)])
        db.session.commit()

        blob = {"is_su_admin": False, "organization_ids": [org_a]}
        with app.test_request_context("/"):
            g.current_user = _blob_to_user(blob)
            rows = scope_to_user_orgs(ObservationCache.query, ObservationCache.org_guid).all()
            assert all(r.org_guid == org_a for r in rows)
            assert any(r.org_guid == org_a for r in rows)

        ObservationCache.query.filter(
            ObservationCache.org_guid.in_([org_a, org_b])
        ).delete(synchronize_session=False)
        db.session.commit()


def test_org_scoping_admin_sees_all():
    app = _app("off")
    with app.app_context():
        org_a = str(uuid.uuid4())
        db.session.add(_mk_obs(org_a))
        db.session.commit()
        blob = {"is_su_admin": True, "organization_ids": []}
        with app.test_request_context("/"):
            g.current_user = _blob_to_user(blob)
            rows = scope_to_user_orgs(ObservationCache.query, ObservationCache.org_guid).all()
            assert any(r.org_guid == org_a for r in rows)
        ObservationCache.query.filter_by(org_guid=org_a).delete()
        db.session.commit()


# ---------- create-su CLI (Rule 23) ----------

def test_create_su_cli():
    app = _app("off")
    runner = app.test_cli_runner()
    name = f"su_{uuid.uuid4().hex[:6]}"
    r = runner.invoke(args=["create-su", "--username", name, "--password", "x"])
    assert r.exit_code == 0
    with app.app_context():
        u = User.query.filter_by(username=name).one()
        assert u.is_su and u.is_admin
        User.query.filter_by(guid=u.guid).delete()
        db.session.commit()
