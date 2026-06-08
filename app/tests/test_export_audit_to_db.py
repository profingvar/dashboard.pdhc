"""Dashboard PDL #4 (#214) — researcher export audit moves from a
file at ``results/export_audit.log`` into the ``dashboard_audit``
table as ``route='research_export'`` rows.

Tests use the same db.session-mocking pattern as test_audit_read.py
(captured fixture) so they don't depend on SQLite's inability to
realise the JSONB/UUID columns the dashboard uses.

Covers:
- _record_export_audit() constructs a DashboardAudit row with the
  right shape (route, n_rows, payload_snapshot, etc.).
- It does NOT write to the EXPORT_AUDIT_LOG file (the old behaviour).
- flask migrate-export-audit-log reads the legacy file and inserts
  one row per JSON entry, idempotent across runs.
"""
from __future__ import annotations

import json
import os
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import Flask, g

from app import create_app
from app.routes import researcher as researcher_mod


# ---------------------------------------------------------------------------
# Mock the db.session that researcher.py + the CLI use, so we can capture
# rows without depending on a real database.
# ---------------------------------------------------------------------------

@pytest.fixture
def captured():
    rows: list[object] = []

    def fake_add(row):
        rows.append(row)

    def fake_commit():
        return None

    def fake_rollback():
        return None

    with patch.object(researcher_mod.db, "session",
                      SimpleNamespace(add=fake_add,
                                      commit=fake_commit,
                                      rollback=fake_rollback,
                                      remove=lambda: None,
                                      query=lambda *_a, **_kw: _EmptyQuery())):
        yield rows


class _EmptyQuery:
    """Stand-in for db.session.query(...) used by the CLI's idempotency
    pre-fetch. Returns no existing rows so every legacy entry is new."""
    def filter(self, *_a, **_kw):
        return self
    def all(self):
        return []


# ---------------------------------------------------------------------------
# Tiny app + request context so _record_export_audit can read g.access_blob
# ---------------------------------------------------------------------------

@pytest.fixture
def mini_app(tmp_path):
    app = Flask(__name__)
    app.config["EXPORT_AUDIT_LOG"] = str(tmp_path / "export_audit.log")
    return app


def _push_request_with_blob(app, blob=None):
    """Helper: returns a context manager that pushes a request context
    with ``g.access_blob`` set."""
    blob = blob or {
        "user_guid": "researcher-1",
        "organization_ids": ["org-x"],
        "session_id": "sess-1",
    }
    ctx = app.test_request_context("/api/cohort/test/export")
    ctx.push()
    g.access_blob = blob
    return ctx


# ---------------------------------------------------------------------------
# _record_export_audit
# ---------------------------------------------------------------------------

class TestRecordExportAudit:
    def test_writes_research_export_row_with_payload(self, mini_app, captured):
        ctx = _push_request_with_blob(mini_app)
        try:
            researcher_mod._record_export_audit(
                export_id="exp-123",
                cohort_id="coh-9",
                variables=["v1", "v2"],
                row_count=42,
            )
        finally:
            ctx.pop()

        assert len(captured) == 1
        row = captured[0]
        assert row.route == "research_export"
        assert row.n_rows_returned == 42
        assert row.response_status == 200
        assert row.user_guid == "researcher-1"
        assert row.user_org_guids == ["org-x"]
        assert row.session_id == "sess-1"
        assert row.patient_guid is None
        snap = row.payload_snapshot
        assert snap["export_id"] == "exp-123"
        assert snap["cohort_id"] == "coh-9"
        assert snap["variables"] == ["v1", "v2"]
        assert "at" in snap

    def test_does_not_write_to_export_audit_log_file(
        self, mini_app, captured,
    ):
        ctx = _push_request_with_blob(mini_app)
        log_path = mini_app.config["EXPORT_AUDIT_LOG"]
        try:
            researcher_mod._record_export_audit(
                export_id="exp-fwf",
                cohort_id="coh-fwf",
                variables=[],
                row_count=0,
            )
        finally:
            ctx.pop()
        assert not os.path.exists(log_path), (
            f"#214 dropped the file write; nothing should appear at {log_path}"
        )

    def test_falls_back_to_email_when_no_user_guid(self, mini_app, captured):
        ctx = _push_request_with_blob(mini_app, blob={
            "email": "fallback@example",
            "organization_ids": [],
        })
        try:
            researcher_mod._record_export_audit(
                export_id="e", cohort_id="c", variables=[], row_count=1,
            )
        finally:
            ctx.pop()
        assert captured[0].user_guid == "fallback@example"

    def test_anonymous_when_blob_missing(self, mini_app, captured):
        with mini_app.test_request_context("/x"):
            researcher_mod._record_export_audit(
                export_id="e", cohort_id="c", variables=[], row_count=0,
            )
        assert captured[0].user_guid == "anonymous"


# ---------------------------------------------------------------------------
# flask migrate-export-audit-log
# ---------------------------------------------------------------------------

class TestMigrateExportAuditLogCLI:
    """The full app (create_app) is needed to register the command,
    but we still mock db.session so SQLite-incompat doesn't bite."""

    @pytest.fixture
    def full_app(self, tmp_path):
        app = create_app({
            "TESTING": True,
            "AUTH_MODE": "off",
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "EXPORT_AUDIT_LOG": str(tmp_path / "export_audit.log"),
        })
        return app

    @staticmethod
    def _write_legacy(path: str, entries: list[dict]):
        with open(path, "w") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")

    def test_migrates_each_entry_to_one_row(
        self, full_app, captured,
    ):
        log_path = full_app.config["EXPORT_AUDIT_LOG"]
        eid = str(uuid.uuid4())
        self._write_legacy(log_path, [{
            "export_id": eid, "cohort_id": "coh-1",
            "user": "legacy-user", "variables": ["v1", "v2"],
            "row_count": 42, "at": "2026-05-01T10:00:00+00:00",
        }])
        result = full_app.test_cli_runner().invoke(
            args=["migrate-export-audit-log"],
        )
        assert result.exit_code == 0, result.output
        assert "added=1" in result.output
        assert "skipped=0" in result.output
        assert len(captured) == 1
        row = captured[0]
        assert row.route == "research_export"
        assert row.n_rows_returned == 42
        assert row.user_guid == "legacy-user"
        snap = row.payload_snapshot
        assert snap["export_id"] == eid
        assert snap["migrated_from_file"] is True
        assert snap["cohort_id"] == "coh-1"
        assert snap["variables"] == ["v1", "v2"]

    def test_malformed_lines_counted_not_inserted(
        self, full_app, captured,
    ):
        log_path = full_app.config["EXPORT_AUDIT_LOG"]
        with open(log_path, "w") as fh:
            fh.write("not-json\n")
            fh.write(json.dumps({"no_export_id": True}) + "\n")
            fh.write(json.dumps({
                "export_id": str(uuid.uuid4()),
                "cohort_id": "c", "user": "u",
                "variables": [], "row_count": 7,
                "at": "2026-05-01T10:00:00+00:00",
            }) + "\n")
        result = full_app.test_cli_runner().invoke(
            args=["migrate-export-audit-log"],
        )
        assert "added=1" in result.output
        assert "malformed=2" in result.output
        assert len(captured) == 1
        assert captured[0].n_rows_returned == 7

    def test_missing_log_file_is_a_no_op(self, full_app, captured):
        result = full_app.test_cli_runner().invoke(
            args=["migrate-export-audit-log"],
        )
        assert result.exit_code == 0
        assert "no log to migrate" in result.output
        assert captured == []

    def test_dry_run_does_not_persist(self, full_app, captured):
        log_path = full_app.config["EXPORT_AUDIT_LOG"]
        self._write_legacy(log_path, [{
            "export_id": str(uuid.uuid4()), "cohort_id": "c",
            "user": "u", "variables": [],
            "row_count": 1, "at": "2026-05-01T10:00:00+00:00",
        }])
        result = full_app.test_cli_runner().invoke(
            args=["migrate-export-audit-log", "--dry-run"],
        )
        assert "added=1" in result.output
        assert "(dry-run, no commit)" in result.output
        # The row was constructed but not added to the session.
        assert captured == []

    def test_idempotency_via_existing_export_ids(self, full_app, tmp_path):
        """When the DB already contains rows with the same export_id
        the CLI must skip them. We simulate this by injecting a query
        that returns a row with the matching id in payload_snapshot."""
        log_path = full_app.config["EXPORT_AUDIT_LOG"]
        eid = str(uuid.uuid4())
        self._write_legacy(log_path, [{
            "export_id": eid, "cohort_id": "c", "user": "u",
            "variables": [], "row_count": 1,
            "at": "2026-05-01T10:00:00+00:00",
        }])
        added: list[object] = []

        class _ExistingQuery:
            """Returns a single pre-existing row with this export_id."""
            def filter(self, *_a, **_kw):
                return self
            def all(self):
                return [({"export_id": eid},)]

        session = SimpleNamespace(
            add=lambda r: added.append(r),
            commit=lambda: None,
            rollback=lambda: None,
            remove=lambda: None,
            query=lambda *_a, **_kw: _ExistingQuery(),
        )
        with patch.object(researcher_mod.db, "session", session):
            result = full_app.test_cli_runner().invoke(
                args=["migrate-export-audit-log"],
            )
        assert "added=0" in result.output
        assert "skipped=1" in result.output
        assert added == []
