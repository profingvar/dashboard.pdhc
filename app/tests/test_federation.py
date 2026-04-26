"""Federation-layer unit tests — execution-plan §4.8.

Covers the items the platform plan calls out for the federation:
  - cohort_predicate_builder
  - fanout_partial_failure
  - histogram_merge
  - lttb_downsample
  - agp_hourly_bands

The federation layer never opens a real socket here; we patch
``requests.request`` with a programmable fake.
"""
from __future__ import annotations

import math
from unittest.mock import patch

import pytest

from app.services.cohort import (
    CohortFilter,
    intersect_patient_sets,
    to_predicate_searches,
)
from app.services.federation import (
    CdrEndpoint,
    CdrRegistry,
    agp_hourly_bands,
    concat_series,
    fanout,
    lttb_downsample,
    merge_histograms,
)


# ---------------------------------------------------------------------------
# Cohort predicate builder
# ---------------------------------------------------------------------------

def test_cohort_predicate_builder_basic():
    raw = {
        "cdr_ids": ["cdr1", "cdr3"],
        "demographics": {"age_min": 40, "age_max": 70, "sex": "female"},
        "conditions": ["https://termbank.pdhc.se/CodeSystem/snomed/44054006"],
        "medications": ["https://termbank.pdhc.se/CodeSystem/atc/A10A"],
    }
    filt = CohortFilter.from_dict(raw)
    assert filt.cdr_ids == ["cdr1", "cdr3"]
    assert filt.age_min == 40 and filt.age_max == 70
    assert filt.sex == "female"
    assert filt.conditions == [
        "https://termbank.pdhc.se/CodeSystem/snomed/44054006"
    ]

    preds = to_predicate_searches(filt)
    types = [p[0] for p in preds]
    assert "Patient" in types
    assert types.count("Condition") == 1
    assert "MedicationStatement" in types

    pat_params = next(p[1] for p in preds if p[0] == "Patient")
    # Two birthdate constraints (le for age_min, ge for age_max) and gender.
    assert "birthdate" in pat_params
    assert pat_params["gender"] == "female"


def test_cohort_predicate_intersect():
    a = {"p1", "p2", "p3"}
    b = {"p2", "p3", "p4"}
    c = {"p3", "p4"}
    assert intersect_patient_sets([a, b, c]) == {"p3"}


def test_cohort_predicate_empty_returns_empty():
    assert intersect_patient_sets([]) == set()


# ---------------------------------------------------------------------------
# Fan-out partial failure
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status, body, raise_exc=None):
        self.status_code = status
        self._body = body
        self._raise = raise_exc
    def json(self):
        return self._body
    @property
    def text(self):
        return ""


def _registry_5() -> CdrRegistry:
    return CdrRegistry([
        CdrEndpoint("cdr1", "http://cdr1", "Norrland"),
        CdrEndpoint("cdr2", "http://cdr2", "Skåne"),
        CdrEndpoint("cdr3", "http://cdr3", "Västra"),
        CdrEndpoint("cdr4", "http://cdr4", "Östergötland"),
        CdrEndpoint("cdr5", "http://cdr5", "Mälardalen"),
    ])


def test_fanout_complete_when_all_ok():
    reg = _registry_5()

    def _req(method, url, params=None, json=None, headers=None, timeout=None):
        return _FakeResp(200, {"ok": True})

    with patch("app.services.federation.requests.request", side_effect=_req):
        r = fanout(reg, path="x")
    assert r.mode == "complete"
    assert len(r.succeeded) == 5
    assert r.failed == []


def test_fanout_degraded_with_minority_failures():
    reg = _registry_5()

    def _req(method, url, params=None, json=None, headers=None, timeout=None):
        return _FakeResp(503 if "cdr2" in url else 200, {"ok": True})

    with patch("app.services.federation.requests.request", side_effect=_req):
        r = fanout(reg, path="x")
    assert r.mode == "degraded"
    assert "cdr2" in r.failed
    assert len(r.succeeded) == 4


def test_fanout_error_when_majority_fails():
    reg = _registry_5()

    def _req(method, url, params=None, json=None, headers=None, timeout=None):
        if any(c in url for c in ("cdr1", "cdr2", "cdr3")):
            return _FakeResp(503, {})
        return _FakeResp(200, {"ok": True})

    with patch("app.services.federation.requests.request", side_effect=_req):
        r = fanout(reg, path="x")
    assert r.mode == "error"
    assert len(r.failed) == 3


def test_fanout_passes_org_and_admin_headers():
    reg = _registry_5()
    captured = {}

    def _req(method, url, params=None, json=None, headers=None, timeout=None):
        captured.update(headers or {})
        return _FakeResp(200, {"ok": True})

    with patch("app.services.federation.requests.request", side_effect=_req):
        fanout(reg, path="x", org_guids_header="org-1,org-2", is_admin_header=True,
               bearer_token="abc")
    assert captured.get("X-Org-Guids") == "org-1,org-2"
    assert captured.get("X-Is-Admin") == "1"
    assert captured.get("Authorization") == "Bearer abc"


# ---------------------------------------------------------------------------
# Histogram merge
# ---------------------------------------------------------------------------

def _stats_body(*, n, mean, sd, mn, mx, hist: list[tuple[float, float, int]]) -> dict:
    return {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "n", "valueInteger": n},
            {"name": "min", "valueDecimal": mn},
            {"name": "max", "valueDecimal": mx},
            {"name": "mean", "valueDecimal": mean},
            {"name": "sd", "valueDecimal": sd},
            {"name": "histogram", "part": [
                {"name": f"bucket_{i}", "valueString": f"[{lo},{hi}):{c}"}
                for i, (lo, hi, c) in enumerate(hist)
            ]},
        ],
    }


def _fake_result(cdr_id, body, *, ok=True):
    from app.services.federation import FanoutResult
    return FanoutResult(
        cdr_id=cdr_id, base_url=f"http://{cdr_id}", region_label=cdr_id,
        ok=ok, status_code=200 if ok else 503, body=body, elapsed_ms=10,
    )


def test_histogram_merge_preserves_total_count():
    """Sum of merged bucket counts equals sum of per-CDR counts."""
    a = _stats_body(n=100, mean=6.0, sd=0.8, mn=5.0, mx=8.0,
                    hist=[(5.0, 6.0, 30), (6.0, 7.0, 50), (7.0, 8.0, 20)])
    b = _stats_body(n=50, mean=7.0, sd=0.5, mn=6.0, mx=9.0,
                    hist=[(6.0, 7.0, 10), (7.0, 8.0, 25), (8.0, 9.0, 15)])
    merged = merge_histograms([
        _fake_result("cdr1", a), _fake_result("cdr2", b),
    ], buckets=10)
    assert merged["n"] == 150
    assert sum(b["count"] for b in merged["buckets"]) == 150
    # Global min/max from the union.
    assert merged["min"] == 5.0
    assert merged["max"] == 9.0


def test_histogram_merge_skips_failed_cdrs():
    a = _stats_body(n=100, mean=6.0, sd=0.5, mn=5.0, mx=8.0,
                    hist=[(5.0, 6.0, 30), (6.0, 7.0, 50), (7.0, 8.0, 20)])
    failed = _fake_result("cdr2", None, ok=False)
    merged = merge_histograms([_fake_result("cdr1", a), failed], buckets=10)
    assert merged["n"] == 100
    assert sum(b["count"] for b in merged["buckets"]) == 100


def test_histogram_merge_combined_mean_matches_weighted_average():
    a = _stats_body(n=100, mean=6.0, sd=0.0, mn=5.0, mx=7.0,
                    hist=[(5.0, 6.0, 50), (6.0, 7.0, 50)])
    b = _stats_body(n=300, mean=8.0, sd=0.0, mn=7.0, mx=9.0,
                    hist=[(7.0, 8.0, 150), (8.0, 9.0, 150)])
    merged = merge_histograms(
        [_fake_result("cdr1", a), _fake_result("cdr2", b)],
        buckets=10,
    )
    expected_mean = (100 * 6.0 + 300 * 8.0) / 400
    assert abs(merged["mean"] - expected_mean) < 1e-9


# ---------------------------------------------------------------------------
# concat_series tags rows with the source CDR
# ---------------------------------------------------------------------------

def test_concat_series_tags_each_row():
    body_a = {"entry": [{"resource": {"resourceType": "Observation", "id": "o1"}}]}
    body_b = {"entry": [{"resource": {"resourceType": "Observation", "id": "o2"}}]}
    rows = concat_series([
        _fake_result("cdr1", body_a),
        _fake_result("cdr2", body_b),
    ])
    assert {r["_cdr_id"] for r in rows} == {"cdr1", "cdr2"}
    assert {r["id"] for r in rows} == {"o1", "o2"}


# ---------------------------------------------------------------------------
# LTTB
# ---------------------------------------------------------------------------

def test_lttb_short_series_returned_unchanged():
    pts = [(0.0, 0.0), (1.0, 1.0), (2.0, 0.5)]
    out = lttb_downsample(pts, target=10)
    assert out == pts


def test_lttb_keeps_endpoints_and_extrema():
    """A spike in the middle survives downsampling."""
    pts = [(float(i), 0.0) for i in range(1000)]
    pts[500] = (500.0, 100.0)  # extremum
    out = lttb_downsample(pts, target=50)
    assert out[0] == pts[0]
    assert out[-1] == pts[-1]
    # The big spike must be in the output.
    assert any(p == (500.0, 100.0) for p in out), "extremum dropped by LTTB"


def test_lttb_target_size_respected():
    pts = [(float(i), float(i * 0.1)) for i in range(10000)]
    out = lttb_downsample(pts, target=2000)
    assert len(out) == 2000


# ---------------------------------------------------------------------------
# AGP hourly bands
# ---------------------------------------------------------------------------

def test_agp_hourly_bands_against_reference():
    """A flat 120 mg/dL series for 24 h: every hourly band collapses to
    120 and TIR=100 %."""
    pts = []
    for h in range(24):
        for s in range(0, 3600, 300):  # every 5 min within the hour
            pts.append((float(h * 3600 + s), 120.0))
    res = agp_hourly_bands(pts)
    for band in res["bands"]:
        assert band["p50"] == 120.0
        assert band["p5"] == 120.0
        assert band["p95"] == 120.0
    assert math.isclose(res["summary"]["tir"], 100.0)
    assert res["summary"]["tbr"] == 0.0


def test_agp_hypo_count_matches_naive_count():
    # 30 minutes below 70 — counts as one event.
    pts = [(0.0, 60.0)] * 6 + [(1800.0, 120.0)] * 12
    res = agp_hourly_bands(pts)
    assert res["summary"]["hypo_events"] == 1
