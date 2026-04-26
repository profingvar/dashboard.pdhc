import os
import uuid
from unittest.mock import patch
from app import create_app
from app.models import db, User, ObservationCache, RefreshLog
from app.services.gateway_client import GatewayClient, normalise, refresh_org


def _app():
    return create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": os.environ.get("DATABASE_URL"),
    })


SAMPLE_BUNDLE = {
    "resourceType": "Bundle",
    "entry": [
        {"resource": {
            "resourceType": "Observation",
            "id": "11111111-1111-1111-1111-111111111111",
            "subject": {"reference": "Patient/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
            "code": {"coding": [{"code": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "display": "B-glucose"}]},
            "valueQuantity": {"value": 5.4, "unit": "mmol/L"},
            "effectiveDateTime": "2026-04-01T10:00:00Z",
        }},
        {"resource": {"resourceType": "Observation", "id": "bad"}},  # dropped by normalise
    ],
}


def test_gateway_client_uses_observations_endpoint_with_bearer():
    """GatewayClient must hit /api/v1/observations with Authorization: Bearer
    and the org as the `organization` query param."""
    captured = {}

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"resourceType": "Bundle", "entry": []}

    def _fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return _Resp()

    org = str(uuid.uuid4())
    client = GatewayClient(token="abc123", base_url="https://gateway.pdhc.se")
    with patch("app.services.gateway_client.requests.get", side_effect=_fake_get):
        rows = client.fetch_observations(org)
    assert rows == []
    assert captured["url"] == "https://gateway.pdhc.se/api/v1/observations"
    assert captured["params"] == {"organization": org}
    assert captured["headers"]["Authorization"] == "Bearer abc123"


def test_normalise_valid_and_invalid():
    org = str(uuid.uuid4())
    good = normalise(SAMPLE_BUNDLE["entry"][0]["resource"], org)
    assert good and good.concept_name == "B-glucose" and good.value == 5.4
    assert normalise(SAMPLE_BUNDLE["entry"][1]["resource"], org) is None


def test_refresh_org_populates_cache():
    app = _app()
    with app.app_context():
        u = User(username=f"gw_{uuid.uuid4().hex[:6]}")
        db.session.add(u)
        db.session.commit()
        org = str(uuid.uuid4())

        client = GatewayClient(token="test-bearer", base_url="http://mock")
        with patch.object(client, "fetch_observations", return_value=[e["resource"] for e in SAMPLE_BUNDLE["entry"]]):
            log = refresh_org(u.guid, org, client=client)

        assert log.status == "ok"
        assert log.rows_fetched == 1
        assert ObservationCache.query.filter_by(org_guid=org).count() == 1

        # cleanup
        ObservationCache.query.filter_by(org_guid=org).delete()
        RefreshLog.query.filter_by(user_guid=u.guid).delete()
        User.query.filter_by(guid=u.guid).delete()
        db.session.commit()
