"""Tests for the @audit_read decorator (ticket #211).

These exercise the decorator's contract in isolation: a fresh tiny
Flask app with the decorator applied to dummy views and the
``DashboardAudit`` row write mocked. Avoids depending on the global
JSONB+UUID model registry that the existing dashboard tests note as
SQLite-incompatible.

What we verify:
  - one row per successful call
  - one row per 4xx (HTTPException raised by ``abort``)
  - status, route rule, and patient guid (from URL view_args)
  - n_rows inferred from common JSON shapes
  - n_rows override via ``g._audit_n_rows``
  - user_guid + organization_ids snapshot from ``g.access_blob``
  - falls back to ``?patient=`` query arg
  - silent on DB write failures (decorator never breaks the response)
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import Flask, abort, g, jsonify

from app.services import audit as audit_module
from app.services.audit import audit_read


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def captured():
    """Collect DashboardAudit instances that would have been committed."""
    rows: list[object] = []

    def fake_add(row):
        rows.append(row)

    def fake_commit():
        return None

    def fake_rollback():
        return None

    with patch.object(audit_module.db, "session",
                      SimpleNamespace(add=fake_add,
                                      commit=fake_commit,
                                      rollback=fake_rollback)):
        yield rows


@pytest.fixture
def app_with_blob():
    """Tiny Flask app with a synthetic access blob installed on ``g``."""
    app = Flask(__name__)

    @app.before_request
    def _install_blob():
        g.access_blob = {
            "user_guid": "user-abc",
            "organization_ids": ["org-1", "org-2"],
            "session_id": None,
        }
    return app


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_one_row_per_success(app_with_blob, captured):
    @app_with_blob.get("/patient/<guid>")
    @audit_read
    def view(guid):
        return jsonify({"patient_guid": guid, "n": 3})

    client = app_with_blob.test_client()
    r = client.get("/patient/PAT-001")
    assert r.status_code == 200
    assert len(captured) == 1
    row = captured[0]
    assert row.response_status == 200
    assert row.route == "GET /patient/<guid>"
    assert row.patient_guid == "PAT-001"
    assert row.user_guid == "user-abc"
    assert row.user_org_guids == ["org-1", "org-2"]
    assert row.session_id is None
    # ``n`` is an explicit count field, so n_rows uses it.
    assert row.n_rows_returned == 3


def test_n_rows_from_entry_list(app_with_blob, captured):
    @app_with_blob.get("/bundle")
    @audit_read
    def view():
        return jsonify({
            "resourceType": "Bundle",
            "entry": [{"resource": {"id": "a"}}, {"resource": {"id": "b"}}],
        })
    client = app_with_blob.test_client()
    r = client.get("/bundle")
    assert r.status_code == 200
    assert captured[0].n_rows_returned == 2


def test_n_rows_override_via_g(app_with_blob, captured):
    @app_with_blob.get("/stream")
    @audit_read
    def view():
        g._audit_n_rows = 42
        return "streamed"
    client = app_with_blob.test_client()
    r = client.get("/stream")
    assert r.status_code == 200
    assert captured[0].n_rows_returned == 42


def test_query_arg_patient_fallback(app_with_blob, captured):
    @app_with_blob.get("/series")
    @audit_read
    def view():
        return jsonify({"total": 0})
    client = app_with_blob.test_client()
    r = client.get("/series?patient=PAT-009&concept=X")
    assert r.status_code == 200
    assert captured[0].patient_guid == "PAT-009"


def test_patient_view_arg_takes_precedence_over_query(app_with_blob, captured):
    @app_with_blob.get("/p/<guid>")
    @audit_read
    def view(guid):
        return jsonify({"total": 0})
    client = app_with_blob.test_client()
    r = client.get("/p/PAT-URL?patient=PAT-QUERY")
    assert r.status_code == 200
    assert captured[0].patient_guid == "PAT-URL"


def test_aggregated_route_has_no_patient_guid(app_with_blob, captured):
    @app_with_blob.get("/cohort/<cohort_id>/scatter")
    @audit_read
    def view(cohort_id):
        return jsonify({"cohort_id": cohort_id, "points": [{"x": 1, "y": 2}]})
    client = app_with_blob.test_client()
    r = client.get("/cohort/C-1/scatter")
    assert r.status_code == 200
    assert captured[0].patient_guid is None
    assert captured[0].n_rows_returned == 1


# ---------------------------------------------------------------------------
# Deny / error paths
# ---------------------------------------------------------------------------

def test_one_row_on_4xx_abort(app_with_blob, captured):
    @app_with_blob.get("/patient/<guid>")
    @audit_read
    def view(guid):
        abort(404)
    client = app_with_blob.test_client()
    r = client.get("/patient/MISSING")
    assert r.status_code == 404
    assert len(captured) == 1
    row = captured[0]
    assert row.response_status == 404
    assert row.patient_guid == "MISSING"
    assert row.n_rows_returned is None


def test_one_row_on_403(app_with_blob, captured):
    @app_with_blob.get("/patient/<guid>")
    @audit_read
    def view(guid):
        abort(403)
    client = app_with_blob.test_client()
    r = client.get("/patient/SECRET")
    assert r.status_code == 403
    assert captured[0].response_status == 403


def test_no_row_on_5xx_pre_return(app_with_blob, captured):
    """If the route raises a non-HTTP exception, the decorator does not
    fabricate an audit row — we have no audit context to log."""
    @app_with_blob.get("/boom")
    @audit_read
    def view():
        raise RuntimeError("kaboom")
    client = app_with_blob.test_client()
    # Flask propagates as 500 via the default handler; suppress
    # raise-test mode.
    app_with_blob.config["TESTING"] = False
    r = client.get("/boom")
    assert r.status_code == 500
    assert captured == []


# ---------------------------------------------------------------------------
# Resilience
# ---------------------------------------------------------------------------

def test_decorator_swallows_audit_write_failures(app_with_blob, captured):
    """An audit-write failure must not break the response to the
    caller."""
    @app_with_blob.get("/x")
    @audit_read
    def view():
        return jsonify({"ok": True})

    def boom_add(_row):
        raise RuntimeError("db down")

    with patch.object(audit_module.db, "session",
                      SimpleNamespace(add=boom_add,
                                      commit=lambda: None,
                                      rollback=lambda: None)):
        client = app_with_blob.test_client()
        r = client.get("/x")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True}


def test_tuple_status_is_captured(app_with_blob, captured):
    @app_with_blob.get("/maybe")
    @audit_read
    def view():
        return jsonify({"n": 0}), 201
    client = app_with_blob.test_client()
    r = client.get("/maybe")
    assert r.status_code == 201
    assert captured[0].response_status == 201
