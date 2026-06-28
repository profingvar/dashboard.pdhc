"""Tests — analyse-layer auxiliary endpoints (ticket #292).

Federated /api/v1/stats, /api/v1/canonical/<table>, and the two
openEHR composition search endpoints. All used to live on cdr1;
moved into the analyse layer so a single request returns a merged
view across CDR1-6.
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
        "AUTH_MODE": "off",
        "GATEWAY_PDHC_SERVICE_KEY": "test-gw-key",
        "CDR_ENDPOINTS": [
            {"cdr_id": "cdr1", "base_url": "http://cdr1.example"},
            {"cdr_id": "cdr2", "base_url": "http://cdr2.example"},
        ],
    })


def _hdr(service="gateway.pdhc", key="test-gw-key"):
    return {"X-Source-Service": service, "X-Service-Key": key}


def _fan(ok_bodies):
    from app.analyse.federation import FanoutResponse
    results = [
        FanoutResult(cdr_id=f"cdr{i+1}", base_url=f"http://cdr{i+1}",
                     region_label="", ok=True, status_code=200,
                     body=b, elapsed_ms=10)
        for i, b in enumerate(ok_bodies)
    ]
    return FanoutResponse(
        mode="complete",
        results=results,
        succeeded=[r.cdr_id for r in results],
        failed=[],
    )


def test_stats_requires_service_key():
    c = _app().test_client()
    r = c.get("/api/v1/stats")
    assert r.status_code in (401, 403)


def test_stats_rejects_unknown_source():
    c = _app().test_client()
    r = c.get("/api/v1/stats",
              headers={"X-Source-Service": "evil.pdhc", "X-Service-Key": "x"})
    assert r.status_code in (401, 403)


def test_stats_sums_across_cdrs():
    fan = _fan([
        {"ingest_raw": 100, "fhir_resources": 200, "openehr_compositions": 5,
         "health_observations": 10, "dedupe_registry": 50, "patients": 3},
        {"ingest_raw": 50, "fhir_resources": 150, "openehr_compositions": 0,
         "health_observations": 5, "dedupe_registry": 20, "patients": 2},
    ])
    with patch("app.analyse.stats.fanout", return_value=fan):
        c = _app().test_client()
        r = c.get("/api/v1/stats", headers=_hdr())
    assert r.status_code == 200
    body = r.get_json()
    assert body["ingest_raw"] == 150
    assert body["fhir_resources"] == 350
    assert body["patients"] == 5
    assert body["cdrs_total"] == 2
    assert body["cdrs_responded"] == 2
    assert body["mode"] == "complete"


def test_stats_degraded_when_one_cdr_fails():
    from app.analyse.federation import FanoutResponse
    results = [
        FanoutResult(cdr_id="cdr1", base_url="http://cdr1",
                     region_label="", ok=True, status_code=200,
                     body={"ingest_raw": 10, "fhir_resources": 20,
                           "openehr_compositions": 1,
                           "health_observations": 2, "dedupe_registry": 5,
                           "patients": 1},
                     elapsed_ms=5),
        FanoutResult(cdr_id="cdr2", base_url="http://cdr2",
                     region_label="", ok=False, status_code=503,
                     body=None, elapsed_ms=5, error="unreachable"),
    ]
    fan = FanoutResponse(mode="degraded", results=results,
                         succeeded=["cdr1"], failed=["cdr2"])
    with patch("app.analyse.stats.fanout", return_value=fan):
        c = _app().test_client()
        r = c.get("/api/v1/stats", headers=_hdr())
    body = r.get_json()
    assert body["ingest_raw"] == 10
    assert body["cdrs_responded"] == 1
    assert body["cdrs_total"] == 2
    assert body["mode"] == "degraded"


def test_canonical_unknown_table_404():
    c = _app().test_client()
    r = c.get("/api/v1/canonical/nope?patient_guid=p1", headers=_hdr())
    assert r.status_code == 404


def test_canonical_missing_patient_400():
    c = _app().test_client()
    r = c.get("/api/v1/canonical/health_observations", headers=_hdr())
    assert r.status_code == 400


def test_canonical_merges_rows_with_cdr_tag():
    fan = _fan([
        {"table": "health_observations", "patient_guid": "p1",
         "rows": [{"guid": "g1", "value": 1.0}]},
        {"table": "health_observations", "patient_guid": "p1",
         "rows": [{"guid": "g2", "value": 2.0}, {"guid": "g3", "value": 3.0}]},
    ])
    with patch("app.analyse.canonical.fanout", return_value=fan):
        c = _app().test_client()
        r = c.get("/api/v1/canonical/health_observations?patient_guid=p1",
                  headers=_hdr())
    assert r.status_code == 200
    body = r.get_json()
    assert body["total"] == 3
    assert {row["_cdr_id"] for row in body["rows"]} == {"cdr1", "cdr2"}


def test_openehr_composition_search_requires_patient():
    c = _app().test_client()
    r = c.get("/api/v1/openehr/composition", headers=_hdr())
    assert r.status_code == 400


def test_openehr_composition_search_merges():
    fan = _fan([
        {"compositions": [{"guid": "c-a"}, {"guid": "c-b"}]},
        {"compositions": [{"guid": "c-c"}]},
    ])
    with patch("app.analyse.openehr.fanout", return_value=fan):
        c = _app().test_client()
        r = c.get("/api/v1/openehr/composition?patient=p-1", headers=_hdr())
    assert r.status_code == 200
    body = r.get_json()
    assert body["total"] == 3
    cdr_ids = {x["_cdr_id"] for x in body["compositions"]}
    assert cdr_ids == {"cdr1", "cdr2"}


def test_openehr_ehr_compositions_path_variant():
    fan = _fan([
        {"compositions": [{"guid": "x"}]},
        {"compositions": []},
    ])
    with patch("app.analyse.openehr.fanout", return_value=fan):
        c = _app().test_client()
        r = c.get("/api/v1/openehr/ehr/p-1/compositions", headers=_hdr())
    assert r.status_code == 200
    body = r.get_json()
    assert body["total"] == 1
    assert body["compositions"][0]["_cdr_id"] == "cdr1"
