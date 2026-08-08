"""Workspace selector + nurse view rendering tests.

These pages are HTML shells; the data comes from the JSON-API on
fetch. We only need to verify route guards and that the templates
render their key markers.

Since #543 extracted the group/population analyse-engine into the
separate analyse.pdhc service, the only clinical workspace here is the
nurse single-patient view, and /workspace always redirects straight to
it.
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


def test_workspace_selector_redirects_dual_role_to_nurse(app_with_blob):
    """A user who also holds the (now-external) researcher role still
    lands on the nurse workspace — #543 leaves nurse as the sole shell."""
    app, h = app_with_blob
    h["blob"] = {"roles": ["nurse", "researcher"], "is_su_admin": False,
                  "organization_ids": []}
    c = app.test_client()
    resp = c.get("/workspace", follow_redirects=False)
    assert resp.status_code == 302
    assert "/nurse" in resp.headers["Location"]


def test_workspace_selector_admin_redirects_to_nurse(app_with_blob):
    app, h = app_with_blob
    h["blob"] = {"is_su_admin": True, "roles": [], "organization_ids": []}
    c = app.test_client()
    resp = c.get("/workspace", follow_redirects=False)
    assert resp.status_code == 302
    assert "/nurse" in resp.headers["Location"]


def test_workspace_selector_blocked_when_no_clinical_role(app_with_blob):
    app, h = app_with_blob
    h["blob"] = {"roles": ["other"], "is_su_admin": False, "organization_ids": []}
    c = app.test_client()
    resp = c.get("/workspace")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# /nurse page renders markers
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


def test_nurse_page_blocks_non_clinical_user(app_with_blob):
    app, h = app_with_blob
    h["blob"] = {"roles": ["other"], "is_su_admin": False, "organization_ids": []}
    c = app.test_client()
    resp = c.get("/nurse")
    assert resp.status_code == 403
