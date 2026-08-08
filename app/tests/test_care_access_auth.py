"""Care-delivery front door + route-aware gate (#463 / #462 D1).

The clinical dashboard's own routes are reachable by a treating clinician
WITHOUT the analysis phase (care relationship suffices); the analyse
engine's routes keep the analysis-phase gate. Verified as a unit
(has_care_delivery_access) and via the SSO request gate.
"""
import uuid
from unittest.mock import patch

import pytest
import sqlalchemy
from flask import session as flask_session

from app import create_app
from app.models import db
from app.auth import has_care_delivery_access, _is_clinical_path


@pytest.fixture(autouse=True)
def _echo_revalidation():
    """Echo the session blob back as the SSO re-validation result (same
    shim as test_auth.py — the loader re-validates on every request)."""
    def _echo(_token):
        try:
            return flask_session.get("access_blob")
        except RuntimeError:
            return None
    with patch("app.auth.validate_sso_token", side_effect=_echo):
        yield


def _app(auth_mode="sso"):
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "test",
        "DATABASE_URL": "sqlite:///:memory:",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {
            "connect_args": {"check_same_thread": False},
            "poolclass": sqlalchemy.pool.StaticPool,
        },
        "AUTH_MODE": auth_mode,
        "SSO_BASE_URL": "https://sso.pdhc.se",
        "SSO_CLIENT_ID": "cid",
        "SSO_CLIENT_SECRET": "sec",
        "SSO_CALLBACK_URL": "https://dashboard.pdhc.se/auth/callback",
    })
    with app.app_context():
        db.create_all()
    return app


def _login_as(client, blob, token="test-token"):
    with client.session_transaction() as sess:
        sess["sso_token"] = token
        sess["access_blob"] = blob


# ---- unit: has_care_delivery_access --------------------------------------

def test_care_access_admin():
    assert has_care_delivery_access({"is_su_admin": True})


def test_care_access_professional_with_affiliation():
    blob = {"user_type": "professional", "is_su_admin": False,
            "affiliations": [{"care_unit_guid": "org-x", "role": "nurse"}]}
    assert has_care_delivery_access(blob)


def test_care_access_professional_legacy_org_ids():
    blob = {"user_type": "professional", "is_su_admin": False,
            "organization_ids": ["org-y"]}
    assert has_care_delivery_access(blob)


def test_care_access_professional_no_scope_denied():
    assert not has_care_delivery_access(
        {"user_type": "professional", "is_su_admin": False})


def test_care_access_non_professional_denied():
    assert not has_care_delivery_access(
        {"user_type": "patient", "is_su_admin": False,
         "organization_ids": ["org-z"]})


def test_care_access_none():
    assert not has_care_delivery_access(None)


def test_clinical_path_classification():
    # #546/#543: /api/nurse/* + the nurse workspace page (/nurse, /workspace)
    # are care-delivery clinical paths; cd-assist has no analyse routes left.
    for p in ("/", "/refresh", "/select", "/patient/abc", "/api/v1/designs",
              "/api/v1/designs/xyz", "/api/nurse/patient/x", "/api/nurse/patient/g/agp",
              "/nurse", "/workspace"):
        assert _is_clinical_path(p), p
    for p in ("/admin/audit",):
        assert not _is_clinical_path(p), p


# ---- SSO gate: care-delivery user reaches clinical, not analyse ----------

_CARE_ONLY = {
    "user_guid": "11111111-1111-1111-1111-111111111111",
    "user_type": "professional",
    "is_su_admin": False,
    "affiliations": [{"affiliation_guid": "a1", "role": "nurse",
                      "care_unit_guid": "org-x"}],
    "session_phases": ["planning"],   # deliberately NOT analysis
}
_ANALYSIS = {
    **_CARE_ONLY,
    "user_guid": "22222222-2222-2222-2222-222222222222",
    "session_phases": ["analysis"],
}


def test_care_only_user_reaches_select():
    app = _app("sso")
    c = app.test_client()
    _login_as(c, _CARE_ONLY)
    assert c.get("/select").status_code == 200


def test_care_only_user_reaches_nurse_workspace():
    app = _app("sso")
    c = app.test_client()
    _login_as(c, _CARE_ONLY)
    # #543/#546: cd-assist has no analyse routes left; the nurse workspace is
    # care-delivery, so a care-only nurse (nurse affiliation, no analysis phase)
    # reaches /workspace, which redirects to the nurse view.
    r = c.get("/workspace")
    assert r.status_code in (302, 200)


def test_analysis_user_reaches_select():
    app = _app("sso")
    c = app.test_client()
    _login_as(c, _ANALYSIS)
    assert c.get("/select").status_code == 200


def test_callback_admits_care_only_user():
    """#546: the SSO callback admits a pure care-delivery user (care
    relationship, no analysis phase) — previously it was analysis-gated,
    which blocked a care-only nurse from ever logging in."""
    app = _app("sso")
    c = app.test_client()
    with c.session_transaction() as s:
        s["sso_state"] = "st1"
        s["sso_next"] = "/select"
    # callback resolves the token via app.routes.auth's own imported name.
    with patch("app.routes.auth.validate_sso_token", return_value=_CARE_ONLY):
        r = c.get("/auth/callback?token=tok&state=st1", follow_redirects=False)
    assert r.status_code == 302
    assert "/auth/login" not in r.headers.get("Location", "")  # admitted, not bounced


def test_callback_rejects_user_with_no_access():
    """A user with neither a care relationship nor the analysis phase is
    cleanly bounced back to login."""
    app = _app("sso")
    c = app.test_client()
    with c.session_transaction() as s:
        s["sso_state"] = "st2"
    no_access = {"user_type": "professional", "is_su_admin": False,
                 "session_phases": [], "affiliations": [], "organization_ids": []}
    with patch("app.routes.auth.validate_sso_token", return_value=no_access):
        r = c.get("/auth/callback?token=tok&state=st2", follow_redirects=False)
    assert r.status_code == 302
    assert "/auth/login" in r.headers.get("Location", "")
