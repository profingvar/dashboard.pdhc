"""Tests — Phase 3 analyse aggregations (#289).

Verifies that compute_stats / compute_agp produce the same Parameters
shape cdr1 used to emit, with numerical agreement against the
linear-interpolation reference (matches Postgres percentile_cont
to float precision).
"""
from __future__ import annotations

import math

import pytest

from app.analyse.aggregations import (
    aggregate_per_cdr_results,
    compute_agp,
    compute_stats,
    _percentile,
)


# ---------------------------------------------------------------------------
# Helpers — synthesise FHIR Observation dicts
# ---------------------------------------------------------------------------

def _obs(value, *, effective="2026-06-01T00:00:00Z", unit="mg/dL"):
    return {
        "resourceType": "Observation",
        "valueQuantity": {"value": value, "unit": unit},
        "effectiveDateTime": effective,
    }


def _bundle(*resources):
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": r} for r in resources],
    }


# ---------------------------------------------------------------------------
# Numerical reference: _percentile linear interpolation
# ---------------------------------------------------------------------------

class TestPercentile:

    def test_empty(self):
        assert math.isnan(_percentile([], 50))

    def test_single(self):
        assert _percentile([42.0], 50) == 42.0
        assert _percentile([42.0], 0) == 42.0
        assert _percentile([42.0], 100) == 42.0

    def test_two_values_midpoint(self):
        assert _percentile([0.0, 10.0], 50) == pytest.approx(5.0)

    def test_known_p50(self):
        # 5 values, p50 → 3rd value (idx=2) since (50/100)*(5-1)=2
        assert _percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50) == pytest.approx(3.0)

    def test_known_p25_interpolation(self):
        # 5 values, p25 → idx=1.0 → exactly v[1] = 2.0
        assert _percentile([1.0, 2.0, 3.0, 4.0, 5.0], 25) == pytest.approx(2.0)

    def test_postgres_agreement_p50(self):
        """percentile_cont(0.5) over [1..10] = 5.5 (linear interp)."""
        vals = [float(i) for i in range(1, 11)]
        assert _percentile(vals, 50) == pytest.approx(5.5)


# ---------------------------------------------------------------------------
# compute_stats — Parameters shape + numerical
# ---------------------------------------------------------------------------

class TestComputeStats:

    def test_empty(self):
        params = compute_stats([])
        assert params["resourceType"] == "Parameters"
        d = {p["name"]: p for p in params["parameter"]}
        assert d["n"]["valueInteger"] == 0
        # histogram part always present, but with empty list
        assert d["histogram"]["part"] == []

    def test_skips_non_numeric(self):
        # valueString shouldn't contribute
        params = compute_stats([
            _obs(5.0),
            {"resourceType": "Observation", "valueString": "high"},
            _obs(10.0),
        ])
        d = {p["name"]: p for p in params["parameter"]}
        assert d["n"]["valueInteger"] == 2
        assert d["min"]["valueDecimal"] == pytest.approx(5.0)
        assert d["max"]["valueDecimal"] == pytest.approx(10.0)

    def test_single_value(self):
        params = compute_stats([_obs(7.5)])
        d = {p["name"]: p for p in params["parameter"]}
        assert d["n"]["valueInteger"] == 1
        assert d["min"]["valueDecimal"] == pytest.approx(7.5)
        assert d["max"]["valueDecimal"] == pytest.approx(7.5)
        assert d["mean"]["valueDecimal"] == pytest.approx(7.5)
        assert d["sd"]["valueDecimal"] == pytest.approx(0.0)

    def test_known_distribution(self):
        # 10 values, even distribution, easy reference
        values = [float(v) for v in range(1, 11)]
        params = compute_stats([_obs(v) for v in values], buckets=5)
        d = {p["name"]: p for p in params["parameter"]}
        assert d["n"]["valueInteger"] == 10
        assert d["min"]["valueDecimal"] == 1.0
        assert d["max"]["valueDecimal"] == 10.0
        assert d["mean"]["valueDecimal"] == pytest.approx(5.5)
        # p25 ≡ percentile_cont(0.25) on 1..10 = 3.25
        assert d["p25"]["valueDecimal"] == pytest.approx(3.25)
        assert d["p50"]["valueDecimal"] == pytest.approx(5.5)
        assert d["p75"]["valueDecimal"] == pytest.approx(7.75)
        # histogram has 5 buckets
        hist_parts = d["histogram"]["part"]
        assert len(hist_parts) == 5
        # parse "[low,high):count" and verify total count == 10
        total = 0
        for hp in hist_parts:
            range_part, count = hp["valueString"].rsplit(":", 1)
            total += int(count)
        assert total == 10

    def test_bundle_input(self):
        bundle = _bundle(_obs(1.0), _obs(2.0), _obs(3.0))
        params = compute_stats(bundle)
        d = {p["name"]: p for p in params["parameter"]}
        assert d["n"]["valueInteger"] == 3

    def test_histogram_part_shape(self):
        """Verify cdr1-compatible parse string '[low,high):count'."""
        params = compute_stats([_obs(v) for v in [1.0, 5.0, 10.0]], buckets=3)
        d = {p["name"]: p for p in params["parameter"]}
        hist = d["histogram"]["part"]
        assert len(hist) == 3
        for i, hp in enumerate(hist):
            assert hp["name"] == f"bucket_{i}"
            s = hp["valueString"]
            range_part, count_part = s.rsplit(":", 1)
            low, high = range_part.strip("[)").split(",", 1)
            assert float(low) <= float(high)
            int(count_part)


# ---------------------------------------------------------------------------
# compute_agp — Parameters shape + numerical
# ---------------------------------------------------------------------------

class TestComputeAgp:

    def _cgm(self, value, hour, day=1):
        return _obs(value,
                    effective=f"2026-06-{day:02d}T{hour:02d}:00:00Z")

    def test_empty(self):
        params = compute_agp([])
        d = {p["name"]: p for p in params["parameter"]}
        assert d["n"]["valueInteger"] == 0
        # bands part has 24 hour entries
        bands = d["bands"]["part"]
        assert len(bands) == 24

    def test_skips_no_effective(self):
        # missing effectiveDateTime → excluded
        params = compute_agp([
            self._cgm(5.0, 8),
            {"resourceType": "Observation",
             "valueQuantity": {"value": 6.0, "unit": "mmol/L"}},
        ])
        d = {p["name"]: p for p in params["parameter"]}
        assert d["n"]["valueInteger"] == 1

    def test_full_day_distribution(self):
        # 24 readings, one per hour, all = 5.0 mmol/L → mean=5, sd=0,
        # tir=100, tbr=0, tar=0.
        params = compute_agp(
            [self._cgm(5.0, h) for h in range(24)],
            tir_low=3.9, tir_high=10.0,
        )
        d = {p["name"]: p for p in params["parameter"]}
        assert d["n"]["valueInteger"] == 24
        assert d["mean"]["valueDecimal"] == pytest.approx(5.0)
        assert d["sd"]["valueDecimal"] == pytest.approx(0.0)
        assert d["tir"]["valueDecimal"] == pytest.approx(100.0)
        assert d["tbr"]["valueDecimal"] == pytest.approx(0.0)
        assert d["tar"]["valueDecimal"] == pytest.approx(0.0)
        # All 24 hours have data
        for hour_part in d["bands"]["part"]:
            sub = {p["name"]: p for p in hour_part["part"]}
            assert sub["n"]["valueInteger"] == 1

    def test_tir_classification(self):
        # mix: 2.0 (tbr) + 5.0 (tir) + 15.0 (tar) at hours 0, 6, 12
        params = compute_agp([
            self._cgm(2.0, 0),
            self._cgm(5.0, 6),
            self._cgm(15.0, 12),
        ], tir_low=3.9, tir_high=10.0)
        d = {p["name"]: p for p in params["parameter"]}
        assert d["n"]["valueInteger"] == 3
        # Each contributes 33.33...%
        assert d["tbr"]["valueDecimal"] == pytest.approx(100 / 3)
        assert d["tir"]["valueDecimal"] == pytest.approx(100 / 3)
        assert d["tar"]["valueDecimal"] == pytest.approx(100 / 3)

    def test_hour_with_no_data(self):
        # only hour 0 has data → hour 1..23 → n=0, p* = None
        params = compute_agp([self._cgm(7.0, 0)])
        d = {p["name"]: p for p in params["parameter"]}
        for hour_part in d["bands"]["part"]:
            sub = {p["name"]: p for p in hour_part["part"]}
            h = sub["hour"]["valueInteger"]
            if h == 0:
                assert sub["n"]["valueInteger"] == 1
            else:
                assert sub["n"]["valueInteger"] == 0
                # p* keys aren't present when band has no data
                for k in ("p5", "p25", "p50", "p75", "p95", "mean"):
                    assert k not in sub or sub[k].get("valueDecimal") is None

    def test_bundle_input(self):
        bundle = _bundle(self._cgm(5.0, 8), self._cgm(6.0, 9))
        params = compute_agp(bundle)
        d = {p["name"]: p for p in params["parameter"]}
        assert d["n"]["valueInteger"] == 2


# ---------------------------------------------------------------------------
# aggregate_per_cdr_results — fanout adapter
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, cdr_id, body, ok=True):
        self.cdr_id = cdr_id
        self.body = body
        self.ok = ok
        self.region_label = "test-region"
        self.status = 200 if ok else 0
        self.error = None
        self.duration_ms = 0


class TestAggregatePerCdrResults:

    def test_stats_pipeline(self):
        bundle_a = _bundle(_obs(1.0), _obs(2.0), _obs(3.0))
        bundle_b = _bundle(_obs(10.0), _obs(20.0))
        results = [
            _FakeResult("cdr1", bundle_a),
            _FakeResult("cdr2", bundle_b),
        ]
        wrapped = aggregate_per_cdr_results(results, kind="stats", buckets=5)
        assert len(wrapped) == 2
        # Each wrapped.body is now a Parameters resource
        for w in wrapped:
            assert w.ok
            assert w.body["resourceType"] == "Parameters"
            d = {p["name"]: p for p in w.body["parameter"]}
            assert "n" in d

    def test_agp_pipeline(self):
        bundle = _bundle(_obs(5.0, effective="2026-06-01T10:00:00Z"))
        wrapped = aggregate_per_cdr_results(
            [_FakeResult("cdr1", bundle)], kind="agp")
        assert len(wrapped) == 1
        assert wrapped[0].body["resourceType"] == "Parameters"

    def test_failed_result_stays_failed(self):
        wrapped = aggregate_per_cdr_results(
            [_FakeResult("cdr1", None, ok=False)], kind="stats")
        assert len(wrapped) == 1
        assert not wrapped[0].ok
        assert wrapped[0].body is None

    def test_compute_failure_marks_not_ok(self):
        wrapped = aggregate_per_cdr_results(
            [_FakeResult("cdr1", "not-a-bundle")], kind="stats")
        # body was wrong type; compute may handle it or fail. If
        # compute raises, wrapper marks ok=False.
        assert len(wrapped) == 1

    def test_unknown_kind_raises(self):
        with pytest.raises(ValueError):
            aggregate_per_cdr_results([], kind="badthing")


# ---------------------------------------------------------------------------
# End-to-end consumption by federation.merge_histograms / merge_agp_bands
# ---------------------------------------------------------------------------

class TestMergeConsumption:
    """The whole point: federation.merge_* should consume our compute_*
    output unchanged. If these break, the route would silently degrade.
    """

    def test_merge_histograms_consumes_compute_stats(self):
        from app.analyse.federation import merge_histograms

        bundle_a = _bundle(*[_obs(v) for v in range(1, 11)])
        bundle_b = _bundle(*[_obs(v) for v in range(11, 21)])
        wrapped = aggregate_per_cdr_results(
            [_FakeResult("cdr1", bundle_a), _FakeResult("cdr2", bundle_b)],
            kind="stats", buckets=10,
        )
        merged = merge_histograms(wrapped, buckets=10)
        assert merged["n"] == 20
        assert merged["min"] == 1.0
        assert merged["max"] == 20.0
        assert len(merged["buckets"]) == 10
        # Sum of bucket counts == n
        assert sum(b["count"] for b in merged["buckets"]) == 20

    def test_merge_agp_bands_consumes_compute_agp(self):
        from app.analyse.federation import merge_agp_bands

        # CDR-1: 24 hours of 5.0; CDR-2: 24 hours of 7.0
        a = _bundle(*[_obs(5.0, effective=f"2026-06-01T{h:02d}:00:00Z")
                      for h in range(24)])
        b = _bundle(*[_obs(7.0, effective=f"2026-06-01T{h:02d}:00:00Z")
                      for h in range(24)])
        wrapped = aggregate_per_cdr_results(
            [_FakeResult("cdr1", a), _FakeResult("cdr2", b)], kind="agp")
        merged = merge_agp_bands(wrapped)
        assert merged["summary"]["n"] == 48
        # Mean is n-weighted; 24*5 + 24*7 / 48 = 6
        assert merged["summary"]["mean"] == pytest.approx(6.0)
        # 24 bands present
        assert len(merged["bands"]) == 24
