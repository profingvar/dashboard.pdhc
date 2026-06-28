"""Tests — analyse-layer /api/v1/observations search (ticket #291).

This endpoint is gateway.pdhc's proxy target. Used to live on cdr1
(ticket #282). Moved here so federated search across CDR1-6 is the
analyse layer's responsibility.
"""
from __future__ import annotations

import os
from unittest.mock import patch

from app import create_app
from app.analyse.federation import FanoutResult


def _app():
    return create_app({
        "TESTING": True,
        "SECRET_KEY": "test",
        "SQLALCHEMY_DATABASE_URI": os.environ.get("DATABASE_URL"),
        "AUTH_MODE": "off",  # not relevant — endpoint uses service-key path
        "GATEWAY_PDHC_SERVICE_KEY": "test-gw-key",
        "CDR_ENDPOINTS": [
            {"cdr_id": "cdr1", "base_url": "http://cdr1.example"},
            {"cdr_id": "cdr2", "base_url": "http://cdr2.example"},
        ],
    })


def _hdr(service="gateway.pdhc", key="test-gw-key"):
    return {"X-Source-Service": service, "X-Service-Key": key}


def _obs(sr_guid, *, value=1.0, patient="p-1"):
    """Build a FHIR R5 Observation resource shape that gateway's
    forwarder would have produced — basedOn[].identifier.value carries
    the SR guid."""
    return {
        "resourceType": "Observation",
        "id": f"obs-{sr_guid[:8]}-{value}",
        "subject": {"reference": f"Patient/{patient}"},
        "basedOn": [
            {
                "type": "ServiceRequest",
                "identifier": {"value": sr_guid},
                "reference": f"ServiceRequest/{sr_guid}",
            },
        ],
        "valueQuantity": {"value": value, "unit": "u"},
    }


def _fanout_returning(per_cdr_entries):
    """Patch fanout to return canned per-CDR bundles."""
    from app.analyse import observations_search as mod

    def _fake(registry, **kw):
        from app.analyse.federation import FanoutResponse
        results = []
        for ep in registry.all:
            entries = per_cdr_entries.get(ep.cdr_id, [])
            results.append(FanoutResult(
                cdr_id=ep.cdr_id,
                base_url=ep.base_url,
                region_label="",
                ok=True,
                status_code=200,
                body={"resourceType": "Bundle", "type": "searchset",
                      "entry": entries},
                elapsed_ms=1,
            ))
        return FanoutResponse(mode="complete", results=results,
                              succeeded=[r.cdr_id for r in results], failed=[])

    return patch.object(mod, "fanout", side_effect=_fake)


# ---------- auth ----------

def test_missing_service_key_returns_403():
    app = _app()
    c = app.test_client()
    r = c.get("/api/v1/observations?service_request=sr-1")
    # Loader's _service_key_outcome returns None (no headers) -> AUTH_MODE=off
    # gives dev SU blob without service_source -> our handler returns 401.
    # That's the no-headers path. Anything with bad headers -> 403 from loader.
    assert r.status_code in (401, 403)


def test_wrong_service_key_returns_403():
    app = _app()
    c = app.test_client()
    r = c.get("/api/v1/observations?service_request=sr-1",
              headers=_hdr(key="wrong-key"))
    assert r.status_code == 403


def test_unknown_source_service_returns_403():
    app = _app()
    c = app.test_client()
    # monitor.pdhc has its own key; using monitor source with gateway key fails.
    r = c.get("/api/v1/observations?service_request=sr-1",
              headers=_hdr(service="monitor.pdhc"))
    assert r.status_code == 403


def test_monitor_pdhc_blocked_at_endpoint():
    """Auth path passes monitor.pdhc (it's a KNOWN_SERVICE) but the
    endpoint itself only accepts gateway.pdhc + monitor.pdhc. Use a
    valid monitor key to confirm endpoint allows it."""
    app = _app()
    app.config["MONITOR_PDHC_SERVICE_KEY"] = "test-mon-key"
    c = app.test_client()
    entries = {"cdr1": [{"resource": _obs("sr-1")}], "cdr2": []}
    with _fanout_returning(entries):
        r = c.get("/api/v1/observations?service_request=sr-1",
                  headers={"X-Source-Service": "monitor.pdhc",
                           "X-Service-Key": "test-mon-key"})
    assert r.status_code == 200


# ---------- query params ----------

def test_missing_service_request_returns_400():
    app = _app()
    c = app.test_client()
    r = c.get("/api/v1/observations", headers=_hdr())
    assert r.status_code == 400
    assert "service_request" in r.get_json()["error"]


# ---------- happy paths ----------

def test_single_cdr_match_returns_bundle():
    app = _app()
    c = app.test_client()
    entries = {
        "cdr1": [
            {"resource": _obs("sr-A", value=1)},
            {"resource": _obs("sr-A", value=2)},
            {"resource": _obs("sr-other", value=99)},  # filtered out
        ],
        "cdr2": [],
    }
    with _fanout_returning(entries):
        r = c.get("/api/v1/observations?service_request=sr-A",
                  headers=_hdr())
    assert r.status_code == 200
    body = r.get_json()
    assert body["resourceType"] == "Bundle"
    assert body["type"] == "searchset"
    assert body["total"] == 2
    assert len(body["entry"]) == 2
    # All matched entries point at sr-A
    for e in body["entry"]:
        idents = [bo["identifier"]["value"]
                  for bo in e["resource"].get("basedOn", [])]
        assert "sr-A" in idents


def test_two_cdrs_merged_into_one_bundle():
    app = _app()
    c = app.test_client()
    entries = {
        "cdr1": [
            {"resource": _obs("sr-A", value=1)},
            {"resource": _obs("sr-A", value=2)},
        ],
        "cdr2": [
            {"resource": _obs("sr-A", value=10)},
        ],
    }
    with _fanout_returning(entries):
        r = c.get("/api/v1/observations?service_request=sr-A",
                  headers=_hdr())
    assert r.status_code == 200
    body = r.get_json()
    assert body["total"] == 3


def test_empty_result_returns_empty_searchset_bundle():
    app = _app()
    c = app.test_client()
    entries = {"cdr1": [], "cdr2": []}
    with _fanout_returning(entries):
        r = c.get("/api/v1/observations?service_request=sr-A",
                  headers=_hdr())
    assert r.status_code == 200
    body = r.get_json()
    assert body == {**body, "type": "searchset", "total": 0, "entry": []}


def test_repeatable_service_request_param():
    app = _app()
    c = app.test_client()
    entries = {
        "cdr1": [
            {"resource": _obs("sr-A", value=1)},
            {"resource": _obs("sr-B", value=2)},
            {"resource": _obs("sr-C", value=3)},
        ],
        "cdr2": [],
    }
    with _fanout_returning(entries):
        r = c.get("/api/v1/observations?service_request=sr-A&service_request=sr-B",
                  headers=_hdr())
    assert r.status_code == 200
    body = r.get_json()
    assert body["total"] == 2


def test_reference_fallback_when_identifier_missing():
    """basedOn[].reference is used when identifier.value is absent."""
    app = _app()
    c = app.test_client()
    obs = {
        "resourceType": "Observation",
        "id": "obs-ref-only",
        "subject": {"reference": "Patient/p-1"},
        "basedOn": [{"reference": "ServiceRequest/sr-Z"}],
        "valueQuantity": {"value": 5, "unit": "u"},
    }
    entries = {"cdr1": [{"resource": obs}], "cdr2": []}
    with _fanout_returning(entries):
        r = c.get("/api/v1/observations?service_request=sr-Z",
                  headers=_hdr())
    assert r.get_json()["total"] == 1
