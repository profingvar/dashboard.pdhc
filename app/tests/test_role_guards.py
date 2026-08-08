"""Role-guard tests — execution-plan §4.7 / §4.8.

Verifies that:
  - the nurse single-patient views (/api/nurse/*) are NO LONGER role-gated
    per-route: after the #546 fold they are CARE-DELIVERY clinical paths,
    enforced by the app-level route gate (``app.auth._is_clinical_path`` →
    ``has_care_delivery_access``), exactly like the /charts view. So the
    access decision is tested against those two functions directly here;
    the live-request 401/302 for an un-authenticated caller is covered in
    ``test_nurse_sparr.py::test_gate_rejects_when_sso_and_no_bearer``.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
import sqlalchemy

from app import create_app
from app.auth import has_care_delivery_access, _is_clinical_path


def _patch_blob(blob):
    """Replace install_request_loader's effect by setting g.access_blob
    inside a before_request hook just for this test app."""
    from flask import g

    def _set_blob():
        g.access_blob = blob

    return _set_blob


@pytest.fixture
def app_with_blob():
    """Yield a tuple of (app, set_blob_fn) so each test can choose the
    role mix it wants for the request."""
    app = create_app({
        "TESTING": True,
        "AUTH_MODE": "off",
        # Hermetic per-test in-memory DB (#441). StaticPool is required:
        # bare sqlite :memory: gives each connection a private db, so
        # rows written in one request would be invisible to the next.
        # create_app overwrites SQLALCHEMY_DATABASE_URI from its DATABASE_URL
        # config key, so set both — otherwise an ambient DATABASE_URL env
        # var would silently re-point the test at a real Postgres.
        "DATABASE_URL": "sqlite:///:memory:",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {
            "connect_args": {"check_same_thread": False},
            "poolclass": sqlalchemy.pool.StaticPool,
        },
        "CDR_ENDPOINTS": [],  # empty registry — endpoints not exercised here
    })
    with app.app_context():
        from app.models import db
        db.create_all()
    blob_holder = {"blob": {}}

    @app.before_request
    def _set():
        from flask import g
        g.access_blob = blob_holder["blob"]

    return app, blob_holder


def test_nurse_paths_are_care_delivery_clinical():
    """#546: /api/nurse/* is a clinical (care-delivery) path, not analysis."""
    assert _is_clinical_path("/api/nurse/patient/some-guid") is True
    assert _is_clinical_path("/api/nurse/patient/g/agp") is True


def test_nurse_care_delivery_gate_rejects_analysis_only_professional():
    """A researcher/analyst professional WITHOUT a care relationship (no
    care-unit scope) is denied the care-delivery nurse views — the fold's
    whole point: analysis phase alone no longer opens single-patient care
    views."""
    blob = {"user_type": "professional", "is_su_admin": False,
            "session_phases": ["analysis"], "affiliations": [],
            "organization_ids": []}
    assert has_care_delivery_access(blob) is False


def test_nurse_endpoint_allows_nurse(app_with_blob):
    app, holder = app_with_blob
    holder["blob"] = {"roles": ["nurse"], "is_su_admin": False,
                       "organization_ids": ["org-1"]}
    client = app.test_client()
    # We don't have any CDRs in the registry, so the response will be 404
    # (no CDR returned the patient) — but it must NOT be 403.
    resp = client.get("/api/nurse/patient/some-guid")
    assert resp.status_code != 403


def test_admin_satisfies_nurse(app_with_blob):
    app, holder = app_with_blob
    holder["blob"] = {"roles": [], "is_su_admin": True,
                       "organization_ids": []}
    client = app.test_client()
    nurse_resp = client.get("/api/nurse/patient/some-guid")
    assert nurse_resp.status_code != 403


def test_anonymous_blocked_from_care_delivery():
    """No blob → no care-delivery access (the app-level gate denies /api/nurse
    and /charts alike). Admin and a care-unit-scoped professional pass."""
    assert has_care_delivery_access(None) is False
    assert has_care_delivery_access({}) is False
    assert has_care_delivery_access({"is_su_admin": True}) is True
    assert has_care_delivery_access({
        "user_type": "professional",
        "affiliations": [{"care_unit_guid": "org-1", "role": "nurse"}],
    }) is True
