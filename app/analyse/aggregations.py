"""Per-CDR aggregations for the analyse layer.

Phase 3 of the CDR1/Analyse split (ticket #289). Previously these
computations lived on cdr1 (``GET /Observation/$stats`` and
``GET /Observation/$agp`` in ``cdr.pdhc/cdr_app/app/api/fhir_read.py``)
and used Postgres ``percentile_cont`` for speed. After the split:

  - Each federated CDR returns raw Observations via the existing
    ``GET /api/v1/fhir/Observation`` search endpoint.
  - The dashboard analyse layer calls :func:`compute_stats` or
    :func:`compute_agp` on each per-CDR Bundle to produce the same
    Parameters shape cdr1 used to emit.
  - The existing ``federation.merge_histograms`` and
    ``federation.merge_agp_bands`` then combine the per-CDR Parameters
    into the cohort-wide result.

The output Parameters shape is byte-for-byte identical to what cdr1's
Python fallback (``_stats_python`` / ``_agp_python``) used to emit.
Numerical agreement with cdr1's Postgres path is exact for n/min/max
and within float precision for mean/sd/percentiles — both use linear
interpolation between adjacent values (``percentile_cont(0.5)`` ≡
``_percentile(values, 50)``).
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone
from typing import Any, Iterable

_AGP_HOURS = 24


# ---------------------------------------------------------------------------
# FHIR-Bundle helpers
# ---------------------------------------------------------------------------

def _iter_observations(bundle: Any) -> Iterable[dict]:
    """Yield Observation resources from a FHIR R5 searchset Bundle."""
    if not isinstance(bundle, dict):
        return
    for entry in bundle.get("entry") or []:
        res = entry.get("resource") if isinstance(entry, dict) else None
        if isinstance(res, dict) and res.get("resourceType") == "Observation":
            yield res


def _value_quantity(obs: dict) -> float | None:
    """Extract Observation.valueQuantity.value as float, else None.

    Skips valueString / valueCodeableConcept (non-numeric) — those
    can't contribute to stats / AGP percentiles.
    """
    vq = obs.get("valueQuantity")
    if not isinstance(vq, dict):
        return None
    v = vq.get("value")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _parse_iso_utc(s: str) -> datetime | None:
    if not isinstance(s, str):
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Numerical primitives — same as cdr1's _percentile / _histogram so the
# Parameters output is byte-for-byte identical.
# ---------------------------------------------------------------------------

def _percentile(sorted_values: list[float], p: float) -> float:
    """Linear-interpolation percentile on a pre-sorted list.

    Matches Postgres ``percentile_cont(p/100)`` within float precision
    and matches cdr1's prior Python fallback exactly.
    """
    if not sorted_values:
        return float("nan")
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx = (p / 100.0) * (len(sorted_values) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (idx - lo)


def _histogram(sorted_values: list[float], buckets: int) -> list[dict]:
    mn, mx = sorted_values[0], sorted_values[-1]
    if mn == mx:
        return [{"low": mn, "high": mx, "count": len(sorted_values)}]
    width = (mx - mn) / buckets
    edges = [mn + i * width for i in range(buckets + 1)]
    edges[-1] = mx + 1e-9
    counts = [0] * buckets
    for v in sorted_values:
        idx = min(int((v - mn) / width), buckets - 1)
        counts[idx] += 1
    return [
        {"low": edges[i], "high": edges[i + 1], "count": counts[i]}
        for i in range(buckets)
    ]


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def compute_stats(observations: Iterable[dict] | dict,
                  *, buckets: int = 20) -> dict:
    """Compute ``$stats`` Parameters body from a per-CDR observation set.

    ``observations`` may be either an iterable of FHIR Observation
    dicts, or a FHIR Bundle dict (will iterate its entries).

    Returns a Parameters resource exactly matching the shape cdr1 used
    to emit, so ``federation.merge_histograms`` consumes it without
    changes.
    """
    if isinstance(observations, dict) and observations.get("resourceType") == "Bundle":
        obs_iter = _iter_observations(observations)
    else:
        obs_iter = observations

    values = [v for v in (_value_quantity(o) for o in obs_iter) if v is not None]
    if not values:
        return _stats_parameters(
            n=0, min_=None, max_=None, mean=None, sd=None,
            p25=None, p50=None, p75=None, histogram=[],
        )

    values.sort()
    n = len(values)
    mn, mx = values[0], values[-1]
    mean = statistics.fmean(values)
    sd = statistics.pstdev(values) if n >= 2 else 0.0
    p25 = _percentile(values, 25)
    p50 = _percentile(values, 50)
    p75 = _percentile(values, 75)
    histogram = _histogram(values, max(1, min(int(buckets), 100)))

    return _stats_parameters(
        n=n, min_=mn, max_=mx, mean=mean, sd=sd,
        p25=p25, p50=p50, p75=p75, histogram=histogram,
    )


def compute_agp(observations: Iterable[dict] | dict,
                *, tir_low: float = 3.9, tir_high: float = 10.0) -> dict:
    """Compute ``$agp`` Parameters body from a per-CDR observation set.

    Same input shape as :func:`compute_stats`. Output matches cdr1's
    prior ``$agp`` so ``federation.merge_agp_bands`` consumes it.
    """
    if isinstance(observations, dict) and observations.get("resourceType") == "Bundle":
        obs_iter = _iter_observations(observations)
    else:
        obs_iter = observations

    by_hour: dict[int, list[float]] = {}
    all_vals: list[float] = []
    for obs in obs_iter:
        v = _value_quantity(obs)
        if v is None:
            continue
        eff = _parse_iso_utc(obs.get("effectiveDateTime") or "")
        if eff is None:
            continue
        by_hour.setdefault(eff.hour, []).append(v)
        all_vals.append(v)

    if not all_vals:
        return _empty_agp_parameters()

    bands = []
    for h in range(_AGP_HOURS):
        vals = sorted(by_hour.get(h, []))
        if not vals:
            bands.append({"hour": h, "n": 0,
                          "p5": None, "p25": None, "p50": None,
                          "p75": None, "p95": None, "mean": None})
            continue
        bands.append({
            "hour": h,
            "n": len(vals),
            "p5": _percentile(vals, 5),
            "p25": _percentile(vals, 25),
            "p50": _percentile(vals, 50),
            "p75": _percentile(vals, 75),
            "p95": _percentile(vals, 95),
            "mean": statistics.fmean(vals),
        })

    n = len(all_vals)
    mean = statistics.fmean(all_vals)
    sd = statistics.pstdev(all_vals) if n >= 2 else 0.0
    cv = (sd / mean * 100) if mean else 0.0
    tir = 100 * sum(1 for v in all_vals if tir_low <= v <= tir_high) / n
    tbr = 100 * sum(1 for v in all_vals if v < tir_low) / n
    tar = 100 * sum(1 for v in all_vals if v > tir_high) / n

    return _agp_parameters(
        n=n, mean=mean, sd=sd, cv=cv, tir=tir, tbr=tbr, tar=tar,
        tir_low=tir_low, tir_high=tir_high, bands=bands,
    )


# ---------------------------------------------------------------------------
# Parameters shaping — byte-for-byte identical to cdr1's prior emitters.
# ---------------------------------------------------------------------------

def _stats_parameters(*, n, min_, max_, mean, sd,
                     p25, p50, p75, histogram) -> dict:
    parts: list[dict] = [{"name": "n", "valueInteger": n}]
    if min_ is not None: parts.append({"name": "min", "valueDecimal": min_})
    if max_ is not None: parts.append({"name": "max", "valueDecimal": max_})
    if mean is not None: parts.append({"name": "mean", "valueDecimal": mean})
    if sd is not None: parts.append({"name": "sd", "valueDecimal": sd})
    if p25 is not None: parts.append({"name": "p25", "valueDecimal": p25})
    if p50 is not None: parts.append({"name": "p50", "valueDecimal": p50})
    if p75 is not None: parts.append({"name": "p75", "valueDecimal": p75})
    parts.append({
        "name": "histogram",
        "part": [{"name": f"bucket_{i}", "valueString":
                  f"[{b['low']},{b['high']}):{b['count']}"}
                 for i, b in enumerate(histogram)],
    })
    return {"resourceType": "Parameters", "parameter": parts}


def _empty_agp_parameters() -> dict:
    return {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "n", "valueInteger": 0},
            {"name": "tir_low", "valueDecimal": None},
            {"name": "tir_high", "valueDecimal": None},
            {"name": "bands", "part": [
                {"name": f"hour_{h}", "part": [
                    {"name": "hour", "valueInteger": h},
                    {"name": "n", "valueInteger": 0},
                ]} for h in range(_AGP_HOURS)
            ]},
        ],
    }


def _agp_parameters(*, n, mean, sd, cv, tir, tbr, tar,
                    tir_low, tir_high, bands) -> dict:
    band_parts = []
    for b in bands:
        bp = [
            {"name": "hour", "valueInteger": b["hour"]},
            {"name": "n", "valueInteger": b["n"]},
        ]
        for k in ("p5", "p25", "p50", "p75", "p95", "mean"):
            if b.get(k) is not None:
                bp.append({"name": k, "valueDecimal": b[k]})
        band_parts.append({"name": f"hour_{b['hour']}", "part": bp})
    parts = [
        {"name": "n", "valueInteger": n},
        {"name": "mean", "valueDecimal": mean},
        {"name": "sd", "valueDecimal": sd},
        {"name": "cv", "valueDecimal": cv},
        {"name": "tir", "valueDecimal": tir},
        {"name": "tbr", "valueDecimal": tbr},
        {"name": "tar", "valueDecimal": tar},
        {"name": "tir_low", "valueDecimal": tir_low},
        {"name": "tir_high", "valueDecimal": tir_high},
        {"name": "bands", "part": band_parts},
    ]
    return {"resourceType": "Parameters", "parameter": parts}


def aggregate_per_cdr_results(per_cdr_bundles, *, kind: str,
                              **kwargs) -> list[Any]:
    """Wrap a list of per-CDR fanout results, swapping each raw Bundle
    body for a computed Parameters body so ``merge_histograms`` /
    ``merge_agp_bands`` can consume them unchanged.

    Returns a list of objects shaped like ``FanoutResult`` but with
    ``body`` replaced. The merger only reads ``r.ok``, ``r.body``,
    ``r.cdr_id`` (and ``r.region_label`` for concat_series) so we
    return lightweight stand-ins.
    """
    if kind == "stats":
        compute = lambda b: compute_stats(b, buckets=kwargs.get("buckets", 20))
    elif kind == "agp":
        compute = lambda b: compute_agp(
            b, tir_low=kwargs.get("tir_low", 3.9),
            tir_high=kwargs.get("tir_high", 10.0))
    else:
        raise ValueError(f"unknown aggregation kind: {kind}")

    class _Wrapped:
        __slots__ = ("ok", "body", "cdr_id", "region_label", "status",
                     "error", "duration_ms")

    out = []
    for r in per_cdr_bundles or []:
        w = _Wrapped()
        w.ok = bool(getattr(r, "ok", False))
        w.cdr_id = getattr(r, "cdr_id", "")
        w.region_label = getattr(r, "region_label", "")
        w.status = getattr(r, "status", 0)
        w.error = getattr(r, "error", None)
        w.duration_ms = getattr(r, "duration_ms", 0)
        if w.ok:
            try:
                w.body = compute(getattr(r, "body", None))
            except Exception:  # any compute failure → mark not-ok
                w.ok = False
                w.body = None
                w.error = "aggregation_failed"
        else:
            w.body = None
        out.append(w)
    return out
