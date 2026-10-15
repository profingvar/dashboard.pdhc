"""#708 — an unconfigured ips returned "no blocks" in silence.

Found by `deploy/smoke_siblings.py` on its first run against production:
`IPS_BASE_URL` is NOT SET in dashboard's production environment. The
consequence is not an error anywhere:

  * `fetch_active_blocks` began with `if not self.base_url: return []`;
  * `blocked_clinic_ids([])` is empty, so no point is dropped;
  * `has_any_active_block([])` is False, so the PDL Ch 4 §4 ¶3 banner
    ("uppgift om att det finns spärrade uppgifter…") never renders.

So every spärr lookup this service has made has answered "no blocks" without
asking anyone. cdr1 applies `care_access_policy` itself on the care-delivery
read, so the data is still filtered one layer down — but a second layer that
is silently absent is not a second layer, and the banner is a legal
requirement, not a convenience.

The fix here is deliberately NOT to raise: raising would take a whole chart
page down over a second layer. It is to make the state LOUD and ASKABLE —
`is_configured()` exists so a caller can say "we could not find out" instead
of "there are no blocked records". Repointing the banner onto it is tracked
separately, because it touches several templates.
"""
from __future__ import annotations

import logging

import pytest
import sqlalchemy

from app import create_app
from app.models import db
from app.services import ips_client as ips_mod


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


@pytest.fixture(autouse=True)
def _reset():
    ips_mod._cache.invalidate()
    ips_mod._UNCONFIGURED_REPORTED = False
    yield
    ips_mod._UNCONFIGURED_REPORTED = False


PAT = "11111111-1111-4111-8111-111111111111"


class TestIsConfigured:
    """`get_active_blocks` returns [] whether ips is absent or has nothing to
    say, so "no blocked records" and "we could not find out" are the same
    value. This is the only way to tell them apart."""

    def test_false_when_IPS_BASE_URL_is_unset(self):
        with _app(IPS_BASE_URL="").app_context():
            assert ips_mod.is_configured() is False

    def test_false_for_a_WHITESPACE_url(self):
        # `os.environ.get(k, "")` means an unset variable arrives as "", and a
        # half-filled .env line arrives as whitespace. Neither is a url.
        with _app(IPS_BASE_URL="   ").app_context():
            assert ips_mod.is_configured() is False

    def test_true_when_it_is_set(self):
        with _app(IPS_BASE_URL="https://ips.example").app_context():
            assert ips_mod.is_configured() is True

    def test_it_does_not_raise_OUTSIDE_an_app_context(self):
        # Called from module-level code and from a background refresh, where
        # there is no request. Raising there would be a new failure mode.
        assert ips_mod.is_configured() is False


class TestTheSilenceIsNowLoud:

    def test_an_unconfigured_lookup_LOGS_an_error_with_a_greppable_marker(
            self, caplog):
        app = _app(IPS_BASE_URL="")
        with app.app_context():
            with caplog.at_level(logging.ERROR):
                blocks = ips_mod.IpsClient(base_url="").fetch_active_blocks(PAT)
        assert blocks == [], "it must still degrade rather than raise"
        text = caplog.text
        assert "SPARR_UNAVAILABLE" in text, (
            "monitoring cannot alert on a message it cannot grep for")
        assert "IPS_BASE_URL" in text, "the message must name what to set"

    def test_it_says_NOTHING_IS_HIDDEN_rather_than_just_naming_a_variable(
            self, caplog):
        """The consequence is the part an operator needs. "IPS_BASE_URL is not
        configured" reads like a warning about a missing feature; "nothing is
        hidden by this service" reads like what it is."""
        app = _app(IPS_BASE_URL="")
        with app.app_context():
            with caplog.at_level(logging.ERROR):
                ips_mod.IpsClient(base_url="").fetch_active_blocks(PAT)
        assert "NO BLOCKS" in caplog.text or "nothing is" in caplog.text.lower()

    def test_it_is_logged_ONCE_not_once_per_chart(self, caplog):
        """A chart page makes one lookup per patient and a nurse view several.
        Fifty identical lines about one deployment fact buries everything
        else in the log."""
        app = _app(IPS_BASE_URL="")
        with app.app_context():
            with caplog.at_level(logging.ERROR):
                for _ in range(5):
                    ips_mod.IpsClient(base_url="").fetch_active_blocks(PAT)
        assert caplog.text.count("SPARR_UNAVAILABLE") == 1


class TestTheDegradationDirectionIsUNCHANGED:
    """Asserted on purpose. The fix must not have turned a quiet second layer
    into a broken page: cdr1 still applies care_access_policy on the
    care-delivery read, and raising here would take a chart down over it."""

    def test_it_returns_an_empty_list_and_does_not_raise(self):
        with _app(IPS_BASE_URL="").app_context():
            assert ips_mod.IpsClient(base_url="").fetch_active_blocks(PAT) == []

    def test_get_active_blocks_also_still_degrades(self):
        with _app(IPS_BASE_URL="").app_context():
            assert ips_mod.get_active_blocks(PAT) == []
