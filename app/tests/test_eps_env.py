"""#1019 (epic #1015): dashboard reads EPS_BASE_URL first, IPS_BASE_URL as fallback.

dashboard's only IPS_* environment read is IPS_BASE_URL (it holds no ips key),
resolved in two places that must agree (#827): `create_app` loads it into the
config, and `ips_client.resolve_base_url` falls back to the environment when
the config is empty. Both go through `app.eps_env.eps_env`.

- the new name wins over the old one;
- the old name alone still works (an un-migrated .env keeps running);
- both unset is exactly what it was before: "", `is_configured()` False, and
  no invented default (#833).
"""
from __future__ import annotations

import pytest
import sqlalchemy

from app import create_app
from app.eps_env import eps_env
from app.models import db
from app.services import ips_client as ips_mod

NEW = "https://eps.example"
OLD = "https://ips.example"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("EPS_BASE_URL", "IPS_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    ips_mod._UNCONFIGURED_REPORTED = False
    yield
    ips_mod._UNCONFIGURED_REPORTED = False


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


# -- the helper ------------------------------------------------------------

def test_helper_new_name_wins(monkeypatch):
    monkeypatch.setenv("EPS_BASE_URL", NEW)
    monkeypatch.setenv("IPS_BASE_URL", OLD)
    assert eps_env("BASE_URL") == NEW


def test_helper_old_name_still_works(monkeypatch):
    monkeypatch.setenv("IPS_BASE_URL", OLD)
    assert eps_env("BASE_URL") == OLD


def test_helper_blank_new_name_falls_back(monkeypatch):
    monkeypatch.setenv("EPS_BASE_URL", "")
    monkeypatch.setenv("IPS_BASE_URL", OLD)
    assert eps_env("BASE_URL") == OLD


def test_helper_both_unset_is_unset():
    assert eps_env("BASE_URL") == ""


# -- create_app loads it into the config -----------------------------------

def test_config_new_name_wins(monkeypatch):
    monkeypatch.setenv("EPS_BASE_URL", NEW)
    monkeypatch.setenv("IPS_BASE_URL", OLD)
    assert _app().config["IPS_BASE_URL"] == NEW


def test_config_old_name_still_works(monkeypatch):
    monkeypatch.setenv("IPS_BASE_URL", OLD)
    assert _app().config["IPS_BASE_URL"] == OLD


def test_config_both_unset_is_empty_and_not_askable():
    app = _app()
    assert app.config["IPS_BASE_URL"] == ""
    with app.app_context():
        assert ips_mod.is_configured() is False


# -- resolve_base_url's environment fallback (config empty) ----------------

def test_resolver_fallback_new_name_wins(monkeypatch):
    app = _app(IPS_BASE_URL="")
    monkeypatch.setenv("EPS_BASE_URL", NEW)
    monkeypatch.setenv("IPS_BASE_URL", OLD)
    with app.app_context():
        assert ips_mod.resolve_base_url() == NEW


def test_resolver_fallback_old_name_still_works(monkeypatch):
    app = _app(IPS_BASE_URL="")
    monkeypatch.setenv("IPS_BASE_URL", OLD)
    with app.app_context():
        assert ips_mod.resolve_base_url() == OLD


def test_resolver_both_unset_is_empty():
    with _app(IPS_BASE_URL="").app_context():
        assert ips_mod.resolve_base_url() == ""


def test_the_client_and_the_question_agree_on_the_new_name(monkeypatch):
    # #827's property, re-asserted for the new variable.
    monkeypatch.setenv("EPS_BASE_URL", NEW)
    with _app().app_context():
        assert ips_mod.is_configured() is True
        assert ips_mod._default_client().base_url == NEW
