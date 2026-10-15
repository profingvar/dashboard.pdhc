"""#827 — "no blocked records" and "we could not ask" were the same page.

The PDL Ch 4 §4 ¶3 banner rendered from `has_blocked_sources`, i.e. from
`has_any_active_block(blocks)`. `get_active_blocks` returns `[]` both when the
patient has no block and when ips was never reachable — and in production it
was the second case from the day the service was deployed, because
`IPS_BASE_URL` was never set (found by `deploy/smoke_siblings.py` on its first
run, #708).

So the page was silent, and a silent page reads as "this patient has nothing
blocked". These tests pin the distinction: with ips unconfigured the chart page
must SAY it could not check, and the JSON that drives the nurse views must
carry the fact rather than leaving the consumer to infer it from a False.

Deliberately NOT asserted: that the page refuses to render. cdr1 applies
`care_access_policy` on the care-delivery read itself (#468), so the rows are
already spärr-filtered one layer down; taking the whole page down over a
missing second layer would be the worse failure.
"""
from __future__ import annotations

import pytest
import sqlalchemy

from app import create_app
from app.models import db
from app.services.ips_client import Block
import app.routes.charts as charts


@pytest.fixture(autouse=True)
def _no_ambient_ips_url(monkeypatch):
    """Resolution is config-then-environment (#827), so an exported
    `IPS_BASE_URL` would make every "unconfigured" case below assert the
    opposite of what it reads."""
    monkeypatch.delenv("IPS_BASE_URL", raising=False)


def _app(**over):
    cfg = {
        "TESTING": True,
        "DATABASE_URL": "sqlite:///:memory:",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {
            "connect_args": {"check_same_thread": False},
            "poolclass": sqlalchemy.pool.StaticPool,
        },
        "AUTH_MODE": "off",
    }
    cfg.update(over)
    app = create_app(cfg)
    with app.app_context():
        db.create_all()
    return app


class _FakeCdr:
    base_url = "https://cdr.pdhc.se"

    def patient_summary(self, *a, **k):
        return []

    def patient_series(self, *a, **k):
        return []


PAT = "11111111-1111-4111-8111-111111111111"


def _page(app, monkeypatch, blocks=()):
    monkeypatch.setattr(charts, "build_client", lambda: _FakeCdr())
    monkeypatch.setattr(charts, "get_active_blocks", lambda g, **k: list(blocks))
    r = app.test_client().get(f"/patient/{PAT}/charts")
    assert r.status_code == 200, r.status_code
    return r.get_data(as_text=True)


def _block(scope_id="org-1"):
    return Block.from_dict({
        "guid": "b1", "patient_guid": PAT, "source_scope_type": "clinic",
        "source_scope_id": scope_id, "is_active": True,
    })


class TestTheChartPageSaysWhenItCouldNotCheck:

    def test_unconfigured_ips_renders_the_could_not_check_notice(
            self, monkeypatch):
        html = _page(_app(IPS_BASE_URL=""), monkeypatch)
        assert "Spärrstatus kunde inte kontrolleras" in html

    def test_it_states_that_silence_does_NOT_mean_no_block(self, monkeypatch):
        # The whole point: an operator or clinician reading the page must not
        # take the absent PDL banner as evidence.
        html = _page(_app(IPS_BASE_URL=""), monkeypatch)
        assert "inte</strong> att spärr saknas" in html

    def test_it_names_the_log_marker_so_the_cause_is_findable(
            self, monkeypatch):
        html = _page(_app(IPS_BASE_URL=""), monkeypatch)
        assert "SPARR_UNAVAILABLE" in html

    def test_a_CONFIGURED_ips_with_no_blocks_renders_NEITHER_notice(
            self, monkeypatch):
        # The negative case is the one that makes the notice meaningful.
        html = _page(_app(IPS_BASE_URL="https://ips.example"), monkeypatch)
        assert "Spärrstatus kunde inte kontrolleras" not in html
        assert "Uppgift om spärr" not in html

    def test_a_CONFIGURED_ips_WITH_a_block_renders_the_PDL_banner_only(
            self, monkeypatch):
        html = _page(_app(IPS_BASE_URL="https://ips.example"), monkeypatch,
                     blocks=[_block()])
        assert "Uppgift om spärr" in html
        assert "Spärrstatus kunde inte kontrolleras" not in html


class TestTheJsonCarriesTheDistinction:
    """The nurse views and the series endpoint are consumed by JS, which
    cannot see a template. `has_blocked_sources: false` alone is the same
    ambiguity one layer out."""

    def test_series_reports_sparr_checked_false_when_unconfigured(
            self, monkeypatch):
        app = _app(IPS_BASE_URL="")
        monkeypatch.setattr(charts, "build_client", lambda: _FakeCdr())
        monkeypatch.setattr(charts, "get_active_blocks", lambda g, **k: [])
        r = app.test_client().get(
            f"/api/v1/patient/{PAT}/series",
            headers={"X-Admin-Read-Reason": "unit-test attest"})
        assert r.status_code == 200, r.get_data(as_text=True)[:200]
        body = r.get_json()
        assert body["has_blocked_sources"] is False
        assert body["sparr_checked"] is False

    def test_series_reports_sparr_checked_true_when_configured(
            self, monkeypatch):
        app = _app(IPS_BASE_URL="https://ips.example")
        monkeypatch.setattr(charts, "build_client", lambda: _FakeCdr())
        monkeypatch.setattr(charts, "get_active_blocks", lambda g, **k: [])
        r = app.test_client().get(
            f"/api/v1/patient/{PAT}/series",
            headers={"X-Admin-Read-Reason": "unit-test attest"})
        assert r.status_code == 200, r.get_data(as_text=True)[:200]
        body = r.get_json()
        assert body["has_blocked_sources"] is False
        assert body["sparr_checked"] is True

    def test_the_two_fields_are_INDEPENDENT(self, monkeypatch):
        # A block found AND ips reachable: both true. If a future refactor
        # derives one from the other this fails.
        app = _app(IPS_BASE_URL="https://ips.example")
        monkeypatch.setattr(charts, "build_client", lambda: _FakeCdr())
        monkeypatch.setattr(charts, "get_active_blocks",
                            lambda g, **k: [_block()])
        r = app.test_client().get(
            f"/api/v1/patient/{PAT}/series",
            headers={"X-Admin-Read-Reason": "unit-test attest"})
        body = r.get_json()
        assert body["has_blocked_sources"] is True
        assert body["sparr_checked"] is True
