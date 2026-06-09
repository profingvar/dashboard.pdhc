"""Ticket #212 — SU-admin off-org reads become an explicit audited lift.

Two layers of coverage:

1. The audit decorator (services/audit.py): when a route sets
   ``g._audit_event_type`` and ``g._audit_admin_justification``, the
   resulting ``DashboardAudit`` row carries those fields. The decorator
   defaults to ``event_type='read'`` and ``admin_justification=None``.

2. The patient view (routes/views.py::patient): off-org detection +
   confirmation form + override audit-row shape. The view's DB calls
   are mocked at the SQLAlchemy session level (dashboard's global model
   registry has JSONB/UUID columns SQLite can't realise, see the
   ``db.session`` mock pattern used in ``test_audit_read`` and
   ``test_export_audit_to_db``).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest
from flask import Flask, g, jsonify, render_template_string

from app.services import audit as audit_module
from app.services.audit import audit_read


# ---------------------------------------------------------------------------
# Decorator-layer fixtures (mirror test_audit_read.py)
# ---------------------------------------------------------------------------

class _Captured(list):
    """List subclass so we can stash the session mock on the fixture
    without monkey-patching builtin ``list``."""

    _session: MagicMock | None = None


@pytest.fixture
def captured():
    """Capture every DashboardAudit row the decorator would commit.

    The session is a MagicMock so other code paths (e.g. the patient
    view's ``db.session.query(...)``) that touch ``db.session`` don't
    blow up; ``query`` is set by the per-test ``view_patches`` fixture
    via ``captured._session.query = ...``."""
    rows = _Captured()
    session_mock = MagicMock()
    session_mock.add.side_effect = lambda r: rows.append(r)
    session_mock.commit.return_value = None
    session_mock.rollback.return_value = None
    session_mock.remove.return_value = None

    with patch.object(audit_module.db, "session", session_mock):
        rows._session = session_mock
        yield rows


@pytest.fixture
def app_with_blob():
    app = Flask(__name__)

    @app.before_request
    def _install_blob():
        g.access_blob = {
            "user_guid": "admin-xyz",
            "organization_ids": ["org-1"],
            "is_su_admin": True,
            "session_id": None,
        }

    return app


# ---------------------------------------------------------------------------
# Decorator layer
# ---------------------------------------------------------------------------

class TestAuditEventTypeColumn:
    def test_default_event_type_is_read(self, app_with_blob, captured):
        @app_with_blob.get("/patient/<guid>")
        @audit_read
        def view(guid):  # noqa: ARG001
            return jsonify({"n": 1})

        c = app_with_blob.test_client()
        r = c.get("/patient/p1")
        assert r.status_code == 200
        assert len(captured) == 1
        row = captured[0]
        assert row.event_type == "read"
        assert row.admin_justification is None

    def test_route_can_set_event_type_to_admin_override(
        self, app_with_blob, captured,
    ):
        @app_with_blob.get("/patient/<guid>")
        @audit_read
        def view(guid):  # noqa: ARG001
            g._audit_event_type = "admin_override"
            g._audit_admin_justification = "klagomålsärende #42"
            return jsonify({"n": 3})

        c = app_with_blob.test_client()
        r = c.get("/patient/p1")
        assert r.status_code == 200
        row = captured[-1]
        assert row.event_type == "admin_override"
        assert row.admin_justification == "klagomålsärende #42"

    def test_admin_override_required_shape(
        self, app_with_blob, captured,
    ):
        @app_with_blob.get("/patient/<guid>")
        @audit_read
        def view(guid):  # noqa: ARG001
            g._audit_event_type = "admin_override_required"
            g._audit_n_rows = 0
            return "form", 200

        c = app_with_blob.test_client()
        r = c.get("/patient/p1")
        assert r.status_code == 200
        row = captured[-1]
        assert row.event_type == "admin_override_required"
        assert row.admin_justification is None
        # No patient data was leaked, so n_rows should be 0.
        assert row.n_rows_returned == 0


# ---------------------------------------------------------------------------
# Patient view: off-org detection + justification gate
# ---------------------------------------------------------------------------

def _patient_app(*, user_is_admin, user_orgs, patient_org_guids,
                 captured_rows):
    """Build a tiny Flask app that exposes views.patient against a fully
    mocked ObservationCache. Real patient.html templates rely on too
    much; we stub render_template to return a JSON marker instead."""
    from app.routes import views as views_module

    app = Flask(__name__)
    app.register_blueprint(views_module.bp)

    @app.before_request
    def _install_current_user_and_blob():
        g.current_user = SimpleNamespace(
            is_admin=user_is_admin,
            org_ids=list(user_orgs),
            guid="user-xyz",
        )
        g.access_blob = {
            "user_guid": "user-xyz",
            "organization_ids": list(user_orgs),
            "is_su_admin": user_is_admin,
            "session_id": None,
        }

    return app


@pytest.fixture
def view_patches(captured):
    """Patch out the heavy collaborators of views.patient so the test
    focuses on the override-decision logic. Returns a holder the tests
    fill in per case. ``captured`` is required so we can route the
    view's ``db.session.query(...)`` through the same session mock the
    audit decorator writes to."""
    q_session_call = MagicMock(name="db.session.query")
    captured._session.query = q_session_call

    with patch(
        "app.routes.views.ObservationCache"
    ) as obs_cache, patch(
        "app.routes.views.get_active_blocks", return_value=[],
    ), patch(
        "app.routes.views.filter_blocked_rows", side_effect=lambda r, _b: r,
    ), patch(
        "app.routes.views.has_any_active_block", return_value=False,
    ), patch(
        "app.routes.views.render_template",
        side_effect=lambda name, **ctx: (
            f"TEMPLATE:{name}:{ctx.get('patient_guid', '?')}"
        ),
    ):
        yield SimpleNamespace(q_session=q_session_call, obs_cache=obs_cache)


def _make_distinct_orgs_query(orgs):
    """Build a chained mock that mimics
    db.session.query(...).filter_by(...).distinct().all()."""
    chain = MagicMock()
    chain.filter_by.return_value = chain
    chain.distinct.return_value = chain
    chain.all.return_value = [(o,) for o in orgs]
    return chain


def _make_obs_cache_query(rows):
    """ObservationCache.query.filter_by(...).filter(...).order_by(...).all()."""
    q = MagicMock()
    q.filter_by.return_value = q
    q.filter.return_value = q
    q.order_by.return_value = q
    q.all.return_value = rows
    return q


class TestPatientViewOverrideGate:
    def test_admin_in_org_no_justification_required(
        self, view_patches, captured,
    ):
        # Admin's orgs intersect patient's orgs → no override gate.
        view_patches.q_session.return_value = _make_distinct_orgs_query(
            ["org-1"],
        )
        # One mock observation row so the view can proceed past the
        # "no rows" abort.
        row = SimpleNamespace(
            patient_guid="p1", concept_guid="c1", concept_name="x",
            value=1.0, unit="u",
            observed_at=__import__("datetime").datetime(
                2026, 6, 1, tzinfo=__import__("datetime").timezone.utc,
            ),
            org_guid="org-1", raw=None,
        )
        view_patches.obs_cache.query = _make_obs_cache_query([row])

        app = _patient_app(
            user_is_admin=True, user_orgs=["org-1"],
            patient_org_guids=["org-1"], captured_rows=captured,
        )
        c = app.test_client()
        r = c.get("/patient/p1")
        # Patient template, not the override form.
        assert r.status_code == 200
        assert b"TEMPLATE:patient.html:p1" in r.data
        assert captured[-1].event_type == "read"
        assert captured[-1].admin_justification is None

    def test_admin_off_org_no_justification_shows_form(
        self, view_patches, captured,
    ):
        # Patient's data is in org-99 — admin's orgs are [org-1].
        view_patches.q_session.return_value = _make_distinct_orgs_query(
            ["org-99"],
        )
        view_patches.obs_cache.query = _make_obs_cache_query([])

        app = _patient_app(
            user_is_admin=True, user_orgs=["org-1"],
            patient_org_guids=["org-99"], captured_rows=captured,
        )
        c = app.test_client()
        r = c.get("/patient/p1")
        assert r.status_code == 200
        assert b"TEMPLATE:admin_override_required.html:p1" in r.data
        assert captured[-1].event_type == "admin_override_required"
        assert captured[-1].admin_justification is None
        assert captured[-1].n_rows_returned == 0

    def test_admin_off_org_with_justification_proceeds(
        self, view_patches, captured,
    ):
        view_patches.q_session.return_value = _make_distinct_orgs_query(
            ["org-99"],
        )
        row = SimpleNamespace(
            patient_guid="p1", concept_guid="c1", concept_name="x",
            value=1.0, unit="u",
            observed_at=__import__("datetime").datetime(
                2026, 6, 1, tzinfo=__import__("datetime").timezone.utc,
            ),
            org_guid="org-99", raw=None,
        )
        view_patches.obs_cache.query = _make_obs_cache_query([row])

        app = _patient_app(
            user_is_admin=True, user_orgs=["org-1"],
            patient_org_guids=["org-99"], captured_rows=captured,
        )
        c = app.test_client()
        r = c.get("/patient/p1?justification=klagomalsutredning")
        assert r.status_code == 200
        assert b"TEMPLATE:patient.html:p1" in r.data
        assert captured[-1].event_type == "admin_override"
        assert captured[-1].admin_justification == "klagomalsutredning"

    def test_admin_off_org_whitespace_justification_shows_form(
        self, view_patches, captured,
    ):
        view_patches.q_session.return_value = _make_distinct_orgs_query(
            ["org-99"],
        )
        view_patches.obs_cache.query = _make_obs_cache_query([])

        app = _patient_app(
            user_is_admin=True, user_orgs=["org-1"],
            patient_org_guids=["org-99"], captured_rows=captured,
        )
        c = app.test_client()
        r = c.get("/patient/p1?justification=%20%20%20")
        assert r.status_code == 200
        assert b"TEMPLATE:admin_override_required.html:p1" in r.data
        assert captured[-1].event_type == "admin_override_required"

    def test_non_admin_off_org_is_blocked_not_lifted(
        self, view_patches, captured,
    ):
        # Non-admin user with no overlap → existing behaviour: nothing
        # returned (rows query is empty), abort(404).
        view_patches.q_session.return_value = _make_distinct_orgs_query(
            ["org-99"],
        )
        view_patches.obs_cache.query = _make_obs_cache_query([])

        app = _patient_app(
            user_is_admin=False, user_orgs=["org-1"],
            patient_org_guids=["org-99"], captured_rows=captured,
        )
        c = app.test_client()
        r = c.get("/patient/p1")
        # No org overlap + no rows → 404 (existing path), event stays 'read'.
        assert r.status_code == 404
        assert captured[-1].event_type == "read"
        assert captured[-1].admin_justification is None

    def test_patient_with_no_data_is_not_off_org(
        self, view_patches, captured,
    ):
        # No rows for patient → patient_orgs is empty → off_org=False.
        # Admin proceeds and hits the 404 path normally.
        view_patches.q_session.return_value = _make_distinct_orgs_query([])
        view_patches.obs_cache.query = _make_obs_cache_query([])

        app = _patient_app(
            user_is_admin=True, user_orgs=["org-1"],
            patient_org_guids=[], captured_rows=captured,
        )
        c = app.test_client()
        r = c.get("/patient/p1")
        assert r.status_code == 404
        # No override gate triggered.
        assert captured[-1].event_type == "read"

    def test_admin_overlap_partial_does_not_require_lift(
        self, view_patches, captured,
    ):
        # Patient has data from org-1 AND org-99. Admin owns org-1.
        # Overlap exists → no off-org → no override required.
        view_patches.q_session.return_value = _make_distinct_orgs_query(
            ["org-1", "org-99"],
        )
        row = SimpleNamespace(
            patient_guid="p1", concept_guid="c1", concept_name="x",
            value=1.0, unit="u",
            observed_at=__import__("datetime").datetime(
                2026, 6, 1, tzinfo=__import__("datetime").timezone.utc,
            ),
            org_guid="org-1", raw=None,
        )
        view_patches.obs_cache.query = _make_obs_cache_query([row])

        app = _patient_app(
            user_is_admin=True, user_orgs=["org-1"],
            patient_org_guids=["org-1", "org-99"], captured_rows=captured,
        )
        c = app.test_client()
        r = c.get("/patient/p1")
        assert r.status_code == 200
        assert b"TEMPLATE:patient.html:p1" in r.data
        assert captured[-1].event_type == "read"
