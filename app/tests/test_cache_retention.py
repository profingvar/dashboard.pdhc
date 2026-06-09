"""Ticket #213 — ObservationCache retention + admin scrub.

Two layers:

1. The pure-DB service (``services/cache_retention``): TTL sweep and
   targeted scrub. Exercised against a mocked ``ObservationCache.query``
   chain so the test doesn't depend on the dashboard model registry
   (which uses JSONB+UUID columns SQLite can't realise — same reason
   the other tests under app/tests/ use this pattern).

2. The admin route ``POST /admin/cache/scrub``: auth gate, missing-
   filter rejection, audit row shape (event_type='cache_scrub',
   patient_guid override, payload_snapshot carrying the filter +
   deleted count).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest
from flask import Flask, g

from app.services import audit as audit_module
from app.services import cache_retention as cr


# ---------------------------------------------------------------------------
# Service-layer
# ---------------------------------------------------------------------------

@pytest.fixture
def cr_db():
    """Patch the ``db.session`` + ``ObservationCache.query`` used by the
    cache_retention helpers. Returns the query-chain mock + commit /
    rollback spies so tests can assert on what was called."""
    chain = MagicMock(name="ObservationCache.query")
    chain.filter.return_value = chain
    chain.filter_by.return_value = chain
    # default: pretend 0 rows matched until each test sets it
    chain.delete.return_value = 0

    commit_spy = MagicMock(name="commit")
    rollback_spy = MagicMock(name="rollback")

    # Column attrs must support SQLAlchemy operators (<, ==). A bare
    # MagicMock raises TypeError on ``<``, so build a stub that returns
    # another mock from comparisons — that's what the real Column does.
    def _col(name):
        col = MagicMock(name=name)
        col.__lt__ = MagicMock(return_value=MagicMock(name=f"{name}<"))
        col.__eq__ = MagicMock(return_value=MagicMock(name=f"{name}=="))
        return col

    with patch.object(cr, "ObservationCache") as obs_cls, patch.object(
        cr.db, "session",
        SimpleNamespace(commit=commit_spy, rollback=rollback_spy),
    ):
        obs_cls.query = chain
        obs_cls.fetched_at = _col("fetched_at")
        obs_cls.patient_guid = _col("patient_guid")
        obs_cls.org_guid = _col("org_guid")
        yield SimpleNamespace(
            chain=chain, commit=commit_spy, rollback=rollback_spy,
            obs_cls=obs_cls,
        )


class TestSweepExpired:
    def test_returns_count_and_commits(self, cr_db):
        cr_db.chain.delete.return_value = 17
        n = cr.sweep_expired_observations(48)
        assert n == 17
        cr_db.commit.assert_called_once()
        # filter() must have been called with a fetched_at < cutoff clause.
        assert cr_db.chain.filter.called

    def test_ttl_zero_or_negative_is_noop(self, cr_db):
        # Defensive: a 0/negative TTL must not wipe the cache; return
        # 0 without touching delete.
        assert cr.sweep_expired_observations(0) == 0
        assert cr.sweep_expired_observations(-5) == 0
        cr_db.chain.delete.assert_not_called()
        cr_db.commit.assert_not_called()

    def test_rolls_back_on_error(self, cr_db):
        cr_db.chain.delete.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError):
            cr.sweep_expired_observations(48)
        cr_db.rollback.assert_called_once()
        cr_db.commit.assert_not_called()


class TestScrubObservations:
    def test_scrub_requires_at_least_one_filter(self, cr_db):
        with pytest.raises(ValueError):
            cr.scrub_observations()
        with pytest.raises(ValueError):
            cr.scrub_observations(patient_guid=None, org_guid=None)
        cr_db.chain.delete.assert_not_called()

    def test_scrub_by_patient_only(self, cr_db):
        cr_db.chain.delete.return_value = 5
        n = cr.scrub_observations(patient_guid="p1")
        assert n == 5
        cr_db.commit.assert_called_once()
        # patient_guid filter applied once, org_guid not applied.
        assert cr_db.chain.filter.call_count == 1

    def test_scrub_by_org_only(self, cr_db):
        cr_db.chain.delete.return_value = 9
        n = cr.scrub_observations(org_guid="org-x")
        assert n == 9
        assert cr_db.chain.filter.call_count == 1

    def test_scrub_by_both_intersects(self, cr_db):
        cr_db.chain.delete.return_value = 3
        n = cr.scrub_observations(
            patient_guid="p1", org_guid="org-x",
        )
        assert n == 3
        # Two filter() calls — one per supplied predicate.
        assert cr_db.chain.filter.call_count == 2

    def test_rolls_back_on_error(self, cr_db):
        cr_db.chain.delete.side_effect = RuntimeError("nope")
        with pytest.raises(RuntimeError):
            cr.scrub_observations(patient_guid="p1")
        cr_db.rollback.assert_called_once()


# ---------------------------------------------------------------------------
# Admin route
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


@pytest.fixture
def admin_app(captured):
    """Tiny Flask app exposing /admin/cache/scrub with a stubbed
    ``current_user`` and the ``scrub_observations`` helper mocked so
    the test focuses on auth + audit shape, not DB I/O."""
    from app.routes import admin as admin_module

    app = Flask(__name__)
    app.register_blueprint(admin_module.bp)

    # Default to admin; tests can monkey-patch per-test.
    state = SimpleNamespace(is_admin=True)
    scrub_stub = MagicMock(return_value=4)
    app._scrub_stub = scrub_stub  # so tests can inspect/configure
    app._state = state

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

    with patch.object(admin_module, "scrub_observations", scrub_stub):
        yield app


class TestCacheScrubEndpoint:
    def test_non_admin_is_forbidden(self, admin_app, captured):
        admin_app._state.is_admin = False
        c = admin_app.test_client()
        r = c.post("/admin/cache/scrub", json={"patient_guid": "p1"})
        assert r.status_code == 403
        admin_app._scrub_stub.assert_not_called()
        # Audit row still recorded (denials are auditable).
        assert captured[-1].response_status == 403
        assert captured[-1].event_type == "read"

    def test_missing_filter_returns_400(self, admin_app, captured):
        c = admin_app.test_client()
        r = c.post("/admin/cache/scrub", json={})
        assert r.status_code == 400
        admin_app._scrub_stub.assert_not_called()
        assert captured[-1].response_status == 400
        assert captured[-1].event_type == "read"

    def test_scrub_by_patient(self, admin_app, captured):
        admin_app._scrub_stub.return_value = 7
        c = admin_app.test_client()
        r = c.post("/admin/cache/scrub", json={
            "patient_guid": "p1",
            "reason": "patient deletion request",
        })
        assert r.status_code == 200
        body = r.get_json()
        assert body["deleted_count"] == 7
        admin_app._scrub_stub.assert_called_once_with(
            patient_guid="p1", org_guid=None,
        )
        # Audit row shape
        row = captured[-1]
        assert row.event_type == "cache_scrub"
        assert row.admin_justification == "patient deletion request"
        assert row.n_rows_returned == 7
        assert row.patient_guid == "p1"
        assert row.response_status == 200
        assert row.payload_snapshot == {
            "patient_guid": "p1",
            "org_guid": None,
            "reason": "patient deletion request",
            "deleted_count": 7,
        }

    def test_scrub_by_org_only(self, admin_app, captured):
        admin_app._scrub_stub.return_value = 13
        c = admin_app.test_client()
        r = c.post("/admin/cache/scrub", json={"org_guid": "org-x"})
        assert r.status_code == 200
        admin_app._scrub_stub.assert_called_once_with(
            patient_guid=None, org_guid="org-x",
        )
        row = captured[-1]
        assert row.event_type == "cache_scrub"
        assert row.patient_guid is None  # org-only scrub
        assert row.n_rows_returned == 13
        assert row.payload_snapshot["org_guid"] == "org-x"
        assert row.payload_snapshot["patient_guid"] is None

    def test_scrub_whitespace_filters_treated_as_missing(
        self, admin_app, captured,
    ):
        c = admin_app.test_client()
        r = c.post("/admin/cache/scrub", json={
            "patient_guid": "   ", "org_guid": "  ",
        })
        assert r.status_code == 400
        admin_app._scrub_stub.assert_not_called()

    def test_scrub_db_error_returns_500(self, admin_app, captured):
        admin_app._scrub_stub.side_effect = RuntimeError("db down")
        c = admin_app.test_client()
        r = c.post("/admin/cache/scrub", json={"patient_guid": "p1"})
        assert r.status_code == 500
        # No 'cache_scrub' event since the route bailed before the
        # success branch sets it.
        assert captured[-1].response_status == 500
        assert captured[-1].event_type == "read"
