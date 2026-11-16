"""#931 — dashboard reads plan.pdhc with its own read_only service key."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services import plan_client as pc
from app.tests.test_plan_client import _app


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for v in ("PLAN_SERVICE_KEY", "PLAN_SOURCE_SERVICE", "PLAN_BASE_URL"):
        monkeypatch.delenv(v, raising=False)


def test_unset_key_sends_no_credential():
    assert pc.plan_read_headers() == {}
    assert "X-Service-Key" not in pc.PlanClient(base_url="http://p")._headers()


def test_env_key_outside_an_app_context(monkeypatch):
    monkeypatch.setenv("PLAN_SERVICE_KEY", "k-env")
    assert pc.plan_read_headers() == {"X-Source-Service": "dashboard.pdhc", "X-Service-Key": "k-env"}


def test_config_wins_over_env(monkeypatch):
    monkeypatch.setenv("PLAN_SERVICE_KEY", "k-env")
    app = _app(PLAN_SERVICE_KEY="k-cfg")
    with app.app_context():
        assert pc.plan_read_headers()["X-Service-Key"] == "k-cfg"


def test_the_transport_sends_the_key(monkeypatch):
    monkeypatch.setenv("PLAN_SERVICE_KEY", "k931")
    r = MagicMock(status_code=200); r.json.return_value = {"items": [], "total": 0}
    with patch.object(pc.requests, "get", return_value=r) as g:
        try:
            pc.PlanClient(base_url="http://p").get_concept("00000000-0000-0000-0000-000000000000")
        except Exception:
            pass
    assert g.called
    assert all(c.kwargs["headers"].get("X-Service-Key") == "k931" for c in g.call_args_list)
