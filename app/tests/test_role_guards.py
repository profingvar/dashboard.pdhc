"""Role-guard tests — execution-plan §4.7 / §4.8.

Verifies that:
  - a researcher-only token hitting /api/nurse/* gets 403
  - a nurse-only token hitting /api/cohort gets 403
  - admin satisfies both
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
import sqlalchemy

from app import create_app


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


def test_nurse_endpoint_rejects_researcher_only(app_with_blob):
    app, holder = app_with_blob
    holder["blob"] = {"roles": ["researcher"], "is_su_admin": False,
                       "organization_ids": ["org-1"]}
    client = app.test_client()
    resp = client.get("/api/nurse/patient/some-guid")
    assert resp.status_code == 403


def test_nurse_endpoint_allows_nurse(app_with_blob):
    app, holder = app_with_blob
    holder["blob"] = {"roles": ["nurse"], "is_su_admin": False,
                       "organization_ids": ["org-1"]}
    client = app.test_client()
    # We don't have any CDRs in the registry, so the response will be 404
    # (no CDR returned the patient) — but it must NOT be 403.
    resp = client.get("/api/nurse/patient/some-guid")
    assert resp.status_code != 403


def test_researcher_endpoint_rejects_nurse_only(app_with_blob):
    app, holder = app_with_blob
    holder["blob"] = {"roles": ["nurse"], "is_su_admin": False,
                       "organization_ids": ["org-1"]}
    client = app.test_client()
    resp = client.post("/api/cohort", json={"cdr_ids": []})
    assert resp.status_code == 403


def test_researcher_endpoint_allows_researcher(app_with_blob):
    app, holder = app_with_blob
    holder["blob"] = {"roles": ["researcher"], "is_su_admin": False,
                       "organization_ids": ["org-1"]}
    client = app.test_client()
    resp = client.post("/api/cohort", json={"cdr_ids": []})
    # Without any CDRs it returns an empty cohort with 0 members → 201.
    assert resp.status_code in (200, 201)


def test_admin_satisfies_both(app_with_blob):
    app, holder = app_with_blob
    holder["blob"] = {"roles": [], "is_su_admin": True,
                       "organization_ids": []}
    client = app.test_client()
    nurse_resp = client.get("/api/nurse/patient/some-guid")
    research_resp = client.post("/api/cohort", json={"cdr_ids": []})
    assert nurse_resp.status_code != 403
    assert research_resp.status_code != 403


def test_anonymous_blocked(app_with_blob):
    app, holder = app_with_blob
    holder["blob"] = {}
    client = app.test_client()
    resp = client.get("/api/nurse/patient/some-guid")
    assert resp.status_code == 403
