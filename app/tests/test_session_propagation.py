"""X2 operator-session propagation (#423) — dashboard.pdhc adoption.

dashboard forwards the operator's X-Operator-Session-Id on its federated CDR
reads (fanout to cdr1..cdr6) and on its gateway / ips onward calls, so the
operator session threads the analyse-layer hops.
"""
import os
from unittest.mock import patch

from app import create_app
from app.services.session_headers import (
    current_session_id,
    outbound_session_headers,
)
from app.services.cdr1_client import Cdr1Client
from app.services.ips_client import IpsClient
from app.analyse import federation
from app.analyse.federation import CdrEndpoint, CdrRegistry, fanout

SID = "sess-dash-1"


def _app():
    return create_app({
        "TESTING": True,
        "AUTH_MODE": "off",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
    })


def test_helper_resolves_and_gates():
    app = _app()
    with app.test_request_context("/", headers={"X-Operator-Session-Id": SID}):
        assert current_session_id() == SID
        assert outbound_session_headers() == {"X-Operator-Session-Id": SID}
    with app.test_request_context("/"):
        assert outbound_session_headers() == {}


def test_cdr1_and_ips_clients_carry_session():
    app = _app()
    with app.test_request_context("/", headers={"X-Operator-Session-Id": SID}):
        assert Cdr1Client(base_url="http://cdr1", service_key="k")._headers(
            [], False).get("X-Operator-Session-Id") == SID
        assert IpsClient(base_url="http://ips", service_key="k")._headers().get(
            "X-Operator-Session-Id") == SID


class _FakeResp:
    status_code = 200
    def json(self):
        return {"resourceType": "Bundle", "entry": []}
    def raise_for_status(self):
        pass


def test_fanout_forwards_operator_session_to_each_cdr():
    """The federated fanout injects X-Operator-Session-Id (resolved in the
    request thread) onto every per-CDR call."""
    app = _app()
    reg = CdrRegistry([
        CdrEndpoint(cdr_id="cdr1", base_url="http://cdr1"),
        CdrEndpoint(cdr_id="cdr2", base_url="http://cdr2"),
    ])
    seen = []
    with app.test_request_context("/", headers={"X-Operator-Session-Id": SID}):
        with patch.object(federation.requests, "request",
                          side_effect=lambda **kw: (seen.append(kw["headers"]), _FakeResp())[1]):
            fanout(reg, method="GET", path="/api/v1/fhir/Observation",
                   bearer_token="tok")
    assert seen, "fanout made no calls"
    for hdrs in seen:
        assert hdrs.get("X-Operator-Session-Id") == SID
