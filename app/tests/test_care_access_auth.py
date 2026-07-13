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
    for p in ("/", "/refresh", "/select", "/patient/abc", "/api/v1/designs",
              "/api/v1/designs/xyz"):
        assert _is_clinical_path(p), p
    for p in ("/workspace", "/api/nurse/patient/x", "/api/cohort/build",
              "/api/v1/observations", "/admin/audit"):
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


def test_care_only_user_blocked_from_analyse_route():
    app = _app("sso")
    c = app.test_client()
    _login_as(c, _CARE_ONLY)
    # /workspace is analyse-engine → analysis-phase gate → 403 for care-only.
    assert c.get("/workspace").status_code == 403


def test_analysis_user_reaches_select():
    app = _app("sso")
    c = app.test_client()
    _login_as(c, _ANALYSIS)
    assert c.get("/select").status_code == 200
