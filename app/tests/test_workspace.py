"""Workspace selector + nurse / researcher view rendering tests.

These pages are HTML shells; the data comes from the JSON-API on
fetch. We only need to verify route guards and that the templates
render their key markers.
"""
from __future__ import annotations

import pytest

from app import create_app


@pytest.fixture
def app_with_blob():
    app = create_app({
        "TESTING": True,
        "AUTH_MODE": "off",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "CDR_ENDPOINTS": [],
    })
    holder = {"blob": {}}

    @app.before_request
    def _set():
        from flask import g
        g.access_blob = holder["blob"]

    return app, holder


# ---------------------------------------------------------------------------
# /workspace
# ---------------------------------------------------------------------------

def test_workspace_selector_redirects_nurse_only(app_with_blob):
    app, h = app_with_blob
    h["blob"] = {"roles": ["nurse"], "is_su_admin": False, "organization_ids": []}
    c = app.test_client()
    resp = c.get("/workspace", follow_redirects=False)
    assert resp.status_code == 302
    assert "/nurse" in resp.headers["Location"]


def test_workspace_selector_redirects_researcher_only(app_with_blob):
    app, h = app_with_blob
    h["blob"] = {"roles": ["researcher"], "is_su_admin": False, "organization_ids": []}
    c = app.test_client()
    resp = c.get("/workspace", follow_redirects=False)
    assert resp.status_code == 302
    assert "/researcher" in resp.headers["Location"]


def test_workspace_selector_shows_chooser_for_dual_role(app_with_blob):
    app, h = app_with_blob
    h["blob"] = {"roles": ["nurse", "researcher"], "is_su_admin": False,
                  "organization_ids": []}
    c = app.test_client()
    resp = c.get("/workspace")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Nurse workspace" in body
    assert "Researcher workspace" in body


def test_workspace_selector_admin_shows_chooser(app_with_blob):
    app, h = app_with_blob
    h["blob"] = {"is_su_admin": True, "roles": [], "organization_ids": []}
    c = app.test_client()
    resp = c.get("/workspace")
    assert resp.status_code == 200


def test_workspace_selector_blocked_when_no_clinical_role(app_with_blob):
    app, h = app_with_blob
    h["blob"] = {"roles": ["other"], "is_su_admin": False, "organization_ids": []}
    c = app.test_client()
    resp = c.get("/workspace")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# /nurse and /researcher pages render markers
# ---------------------------------------------------------------------------

def test_nurse_page_renders(app_with_blob):
    app, h = app_with_blob
    h["blob"] = {"roles": ["nurse"], "is_su_admin": False, "organization_ids": []}
    c = app.test_client()
    resp = c.get("/nurse")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Patient GUID" in body
    assert "Ambulatory glucose profile" in body
    assert "Latest values" in body
    # The variable canonicals from the page must appear so a future
    # cdr.pdhc canonical rename gets caught here.
    assert "termbank.pdhc.se/CodeSystem/loinc/4548-4" in body


def test_researcher_page_renders(app_with_blob):
    app, h = app_with_blob
    h["blob"] = {"roles": ["researcher"], "is_su_admin": False, "organization_ids": []}
    c = app.test_client()
    resp = c.get("/researcher")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Build cohort" in body
    assert "CDR1" in body and "CDR5" in body  # all 5 listed
    assert "Histogram" in body and "Box / Violin" in body
    assert "Export" in body


def test_nurse_page_blocks_researcher_only(app_with_blob):
    app, h = app_with_blob
    h["blob"] = {"roles": ["researcher"], "is_su_admin": False, "organization_ids": []}
    c = app.test_client()
    resp = c.get("/nurse")
    assert resp.status_code == 403


def test_researcher_page_blocks_nurse_only(app_with_blob):
    app, h = app_with_blob
    h["blob"] = {"roles": ["nurse"], "is_su_admin": False, "organization_ids": []}
    c = app.test_client()
    resp = c.get("/researcher")
    assert resp.status_code == 403
