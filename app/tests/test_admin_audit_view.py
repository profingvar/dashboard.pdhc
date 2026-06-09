"""Ticket #215 — /admin/audit operator view + CSV export.

Focuses on the SU-only authz gate, the filter-construction layer, and
the CSV shape. The DB query path is mocked at ``DashboardAudit.query``
since dashboard's model registry uses JSONB+UUID columns SQLite can't
realise (the same constraint that drove the mock-based pattern in
``test_audit_read.py`` / ``test_export_audit_to_db.py``).
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest
from flask import Flask, g

from app.routes import admin as admin_module
from app.services import audit as audit_module


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _Captured(list):
    _session = None


@pytest.fixture
def captured():
    rows = _Captured()
    session_mock = MagicMock()
    session_mock.add.side_effect = lambda r: rows.append(r)
    session_mock.commit.return_value = None
    session_mock.rollback.return_value = None
    session_mock.remove.return_value = None

    with patch.object(audit_module.db, "session", session_mock):
        rows._session = session_mock
        yield rows


def _row(
    *,
    guid="row-guid",
    timestamp=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
    user_guid="user-a",
    user_org_guids=None,
    route="GET /patient/<guid>",
    event_type="read",
    patient_guid="p1",
    n_rows_returned=3,
    response_status=200,
    session_id=None,
    admin_justification=None,
    payload_snapshot=None,
):
    return SimpleNamespace(
        guid=guid, timestamp=timestamp, user_guid=user_guid,
        user_org_guids=user_org_guids or [],
        route=route, event_type=event_type, patient_guid=patient_guid,
        n_rows_returned=n_rows_returned, response_status=response_status,
        session_id=session_id, admin_justification=admin_justification,
        payload_snapshot=payload_snapshot,
    )


@pytest.fixture
def audit_app(captured):
    """Tiny app with /admin/audit mounted + the DashboardAudit.query
    chain mocked. The full base.html requires the views blueprint which
    we don't load here, so render_template is stubbed to capture the
    context and return a JSON-flavoured marker the tests inspect."""
    app = Flask(__name__)
    app.register_blueprint(admin_module.bp)

    state = SimpleNamespace(is_admin=True, rows=[], total=0)
    app._state = state
    rendered: list[dict] = []
    app._rendered = rendered

    @app.before_request
    def _install_user_and_blob():
        g.current_user = SimpleNamespace(
            guid="user-xyz", is_admin=state.is_admin, is_su=state.is_admin,
        )
        g.access_blob = {
            "user_guid": "user-xyz",
            "organization_ids": [],
            "is_su_admin": state.is_admin,
            "session_id": None,
        }

    chain = MagicMock(name="DashboardAudit.query")
    chain.filter.return_value = chain
    chain.order_by.return_value = chain
    chain.limit.return_value = chain
    chain.offset.return_value = chain
    chain.all.side_effect = lambda: list(state.rows)
    chain.count.side_effect = lambda: state.total

    def _fake_render(name, **ctx):
        rendered.append({"name": name, "ctx": ctx})
        # Plain string so tests can grep for tokens; serialise event
        # types verbatim so TestAuditView can assert on them.
        out = [f"TEMPLATE:{name}"]
        for r in ctx.get("rows", []) or []:
            out.append(
                f"row|{r.get('event_type')}|"
                f"{r.get('patient_guid','')}|"
                f"{r.get('admin_justification','') or ''}"
            )
        return "\n".join(out)

    with patch.object(
        admin_module.DashboardAudit, "query", chain, create=True,
    ), patch.object(
        admin_module, "render_template", side_effect=_fake_render,
    ):
        app._chain = chain
        yield app


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class TestAdminAuditAuth:
    def test_non_admin_view_is_forbidden(self, audit_app, captured):
        audit_app._state.is_admin = False
        c = audit_app.test_client()
        r = c.get("/admin/audit")
        assert r.status_code == 403
        # Denial still audited.
        assert captured[-1].response_status == 403

    def test_non_admin_export_is_forbidden(self, audit_app, captured):
        audit_app._state.is_admin = False
        c = audit_app.test_client()
        r = c.get("/admin/audit/export.csv")
        assert r.status_code == 403
        assert captured[-1].response_status == 403


# ---------------------------------------------------------------------------
# Filter construction
# ---------------------------------------------------------------------------

class TestAuditFilters:
    def test_blank_filters_no_predicates(self, audit_app, captured):
        c = audit_app.test_client()
        r = c.get("/admin/audit")
        assert r.status_code == 200
        # Only order_by called, no filter() applied.
        audit_app._chain.filter.assert_not_called()

    def test_all_filters_applied(self, audit_app, captured):
        c = audit_app.test_client()
        qs = (
            "?from=2026-06-01T00:00:00Z"
            "&to=2026-06-08T00:00:00Z"
            "&user_guid=user-xyz"
            "&patient_guid=p1"
            "&route=GET+/patient/%3Cguid%3E"
            "&event_type=admin_override"
        )
        r = c.get("/admin/audit" + qs)
        assert r.status_code == 200
        # 6 predicates -> 6 .filter() calls.
        assert audit_app._chain.filter.call_count == 6

    def test_unparseable_date_skipped(self, audit_app, captured):
        c = audit_app.test_client()
        r = c.get("/admin/audit?from=not-a-date")
        assert r.status_code == 200
        # No predicate applied because parse returned None.
        audit_app._chain.filter.assert_not_called()

    def test_whitespace_filters_skipped(self, audit_app, captured):
        c = audit_app.test_client()
        r = c.get("/admin/audit?user_guid=%20%20&patient_guid=%20")
        assert r.status_code == 200
        audit_app._chain.filter.assert_not_called()

    def test_pagination_clamps_page_at_1(self, audit_app, captured):
        c = audit_app.test_client()
        r = c.get("/admin/audit?page=0")
        assert r.status_code == 200
        audit_app._chain.offset.assert_called_with(0)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class TestAuditView:
    def test_renders_rows(self, audit_app, captured):
        audit_app._state.rows = [
            _row(event_type="admin_override",
                 admin_justification="klagomalsutredning",
                 patient_guid="ppp-aaa"),
            _row(event_type="cache_scrub",
                 payload_snapshot={"deleted_count": 9}),
        ]
        audit_app._state.total = 2
        c = audit_app.test_client()
        r = c.get("/admin/audit")
        assert r.status_code == 200
        body = r.data
        assert b"admin_override" in body
        assert b"cache_scrub" in body
        assert b"klagomalsutredning" in body

    def test_view_writes_its_own_audit_row(self, audit_app, captured):
        audit_app._state.rows = []
        audit_app._state.total = 0
        c = audit_app.test_client()
        c.get("/admin/audit")
        # The view itself is a kontroll action.
        assert captured[-1].event_type == "admin_audit_view"
        assert captured[-1].response_status == 200


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

class TestAuditExport:
    def test_csv_header_and_rows(self, audit_app, captured):
        audit_app._state.rows = [
            _row(event_type="read", patient_guid="p1",
                 user_guid="u-1", n_rows_returned=3),
            _row(event_type="admin_override", patient_guid="p2",
                 user_guid="u-2",
                 admin_justification="utredning klagomål",
                 payload_snapshot={"deleted_count": 1}),
        ]
        audit_app._state.total = 2
        c = audit_app.test_client()
        r = c.get("/admin/audit/export.csv")
        assert r.status_code == 200
        assert r.mimetype == "text/csv"
        assert r.headers.get("Content-Disposition", "").startswith(
            "attachment",
        )
        body = r.data.decode("utf-8")
        lines = body.strip().splitlines()
        assert lines[0].startswith(
            "guid,timestamp,user_guid,user_org_guids,route,event_type,"
            "patient_guid,n_rows_returned,response_status,session_id,"
            "admin_justification,payload_snapshot",
        )
        assert any("admin_override" in line for line in lines[1:])
        assert any("utredning klag" in line for line in lines[1:])

    def test_csv_writes_audit_row_with_export_event(
        self, audit_app, captured,
    ):
        audit_app._state.rows = []
        audit_app._state.total = 0
        c = audit_app.test_client()
        c.get("/admin/audit/export.csv?event_type=admin_override")
        row = captured[-1]
        assert row.event_type == "admin_audit_export"
        # The filter that drove the export is preserved in the payload.
        assert row.payload_snapshot is not None
        assert row.payload_snapshot["filters"]["event_type"] == (
            "admin_override"
        )
        assert row.payload_snapshot["row_count"] == 0

    def test_csv_jsonifies_list_and_dict_cells(
        self, audit_app, captured,
    ):
        audit_app._state.rows = [
            _row(user_org_guids=["org-A", "org-B"],
                 payload_snapshot={"k": "v"}),
        ]
        audit_app._state.total = 1
        c = audit_app.test_client()
        r = c.get("/admin/audit/export.csv")
        body = r.data.decode("utf-8")
        # JSON-encoded list + dict appear in the cell text.
        assert '"[\\"org-A\\", \\"org-B\\"]"' in body or \
            '"[""org-A"", ""org-B""]"' in body
        assert '"{\\"k\\": \\"v\\"}"' in body or \
            '"{""k"": ""v""}"' in body
