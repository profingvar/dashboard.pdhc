"""Shared pytest fixtures.

`fixtures_app` / `fixtures_client` (#974): an app with the SYNTHETIC dashboard
fixture layer installed (`app/dashboard/fixtures.py`) — three patients, five
in-memory upstreams, no network. Take a source down for one test with

    from app.dashboard import fixtures
    fixtures.set_failing(fixtures_app, "ips")        # or
    with fixtures.failing(fixtures_app, "plan", "request"): ...

Teardown clears the process-wide caches (plan's concept index, ips's block
cache), so fixture names never leak into the next test's app.
"""
from __future__ import annotations

import pytest
import sqlalchemy


@pytest.fixture
def fixtures_app():
    from app import create_app
    from app.dashboard import fixtures
    from app.models import db
    app = create_app({
        "TESTING": True,
        "DATABASE_URL": "sqlite:///:memory:",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {
            "connect_args": {"check_same_thread": False},
            "poolclass": sqlalchemy.pool.StaticPool,
        },
        "AUTH_MODE": "off",
        "DASHBOARD_FIXTURES": "1",
    })
    with app.app_context():
        db.create_all()
    yield app
    fixtures._reset_caches()


@pytest.fixture
def fixtures_client(fixtures_app):
    with fixtures_app.test_client() as c:
        yield c
