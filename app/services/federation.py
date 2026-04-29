"""Federation backend — fan-out across the 5 demonstrator CDRs.

Platform-plan execution §4.1. Nothing here calls out to the CDRs at
import time; the registry is built from app config and probed on demand.

Public surface:
  - ``CdrRegistry`` — the list of known CDR endpoints + a discover() ping
  - ``fanout`` — concurrent GET / POST against every CDR with per-CDR
    timeout and partial-result tolerance
  - ``merge_histograms`` — combines per-CDR ``$stats`` histograms into a
    cross-cohort histogram with preserved counts
  - ``concat_series`` — concatenates raw series per CDR, tagging each
    point with its source ``org_guid`` for per-region colouring
  - ``lttb_downsample`` — largest-triangle-three-buckets downsample that
    preserves visual extrema (used for AGP / variable charts)
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import requests


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CdrEndpoint:
    """One CDR instance the dashboard knows about.

    ``cdr_id`` is the short stable label (e.g. ``"cdr1"``); the
    federation always tags partial-result rows with this so the UI
    can distinguish "data from CDR3 only" vs "data from CDR2 + CDR4".
    """
    cdr_id: str
    base_url: str
    region_label: str = ""


class CdrRegistry:
    """In-process registry of CDR endpoints.

    Built from ``app.config["CDR_ENDPOINTS"]`` (list of dicts) at startup.
    `discover()` pings each one's ``/healthz`` and returns the reachable
    subset; the dashboard uses that for the boot-time banner showing
    which CDRs are currently online.
    """

    def __init__(self, endpoints: list[CdrEndpoint]):
        self._endpoints = list(endpoints)

    @classmethod
    def from_config(cls, app_config: dict) -> "CdrRegistry":
        endpoints = []
        for raw in app_config.get("CDR_ENDPOINTS", []) or []:
            endpoints.append(CdrEndpoint(
                cdr_id=raw["cdr_id"],
                base_url=raw["base_url"].rstrip("/"),
                region_label=raw.get("region_label", ""),
            ))
        return cls(endpoints)

    @property
    def all(self) -> list[CdrEndpoint]:
        return list(self._endpoints)

    def filter(self, cdr_ids: Iterable[str] | None = None) -> list[CdrEndpoint]:
        if not cdr_ids:
            return self.all
        wanted = set(cdr_ids)
        return [e for e in self._endpoints if e.cdr_id in wanted]

    def discover(self, *, timeout: float = 2.0) -> dict[str, bool]:
        """Ping each ``/healthz``; return ``{cdr_id: reachable_bool}``."""
        out: dict[str, bool] = {}
        for ep in self._endpoints:
            try:
                resp = requests.get(f"{ep.base_url}/healthz", timeout=timeout)
                out[ep.cdr_id] = (resp.status_code == 200)
            except requests.RequestException:
                out[ep.cdr_id] = False
        return out


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------

@dataclass
class FanoutResult:
    """One per-CDR result. ``ok`` is False on timeout / network failure /
    non-2xx."""
    cdr_id: str
    base_url: str
    region_label: str
    ok: bool
    status_code: int
    body: Any | None
    elapsed_ms: int
    error: str | None = None


@dataclass
class FanoutResponse:
    """Aggregate of every CDR call. ``mode`` is one of:
        complete   — every CDR returned ok
        degraded   — some succeeded, some failed (still returnable)
        error      — majority failed (caller surfaces 503)
    """
    mode: str  # "complete" | "degraded" | "error"
    results: list[FanoutResult]
    succeeded: list[str]
    failed: list[str]


def fanout(
    registry: CdrRegistry,
    *,
    method: str = "GET",
    path: str,
    cdr_ids: Iterable[str] | None = None,
    params: dict | None = None,
    json: dict | None = None,
    extra_headers: dict | None = None,
    timeout: float = 2.0,
    bearer_token: str | None = None,
    org_guids_header: str | None = None,
    is_admin_header: bool = False,
    max_workers: int = 8,
) -> FanoutResponse:
    """Call ``method <base>/<path>`` against every CDR in parallel.

    Per-CDR timeout is ``timeout``. Returns a :class:`FanoutResponse`
    with ``mode = complete | degraded | error`` based on success ratio
    (per execution-plan §4.1.b: degraded if 1–2 of 5 fail; error if
    majority fails).

    The caller's bearer token + org claim are forwarded so each CDR can
    enforce its own Rule 24 filter (§4.1.c).
    """
    targets = registry.filter(cdr_ids)
    if not targets:
        return FanoutResponse(mode="error", results=[], succeeded=[], failed=[])

    headers = {"Accept": "application/json"}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    else:
        # No human SSO bearer in scope (service-key call, monitor /
        # cron / smoke / Phase-4 backend running before SSO is wired).
        # Fall back to the dashboard's outbound service-key — the CDRs
        # accept "dashboard.pdhc" via their KNOWN_FHIR_SERVICES table.
        try:
            from flask import current_app
            sk = current_app.config.get("DASHBOARD_PDHC_SERVICE_KEY", "")
        except RuntimeError:
            sk = ""
        if sk:
            headers["X-Source-Service"] = "dashboard.pdhc"
            headers["X-Service-Key"] = sk
    if org_guids_header:
        headers["X-Org-Guids"] = org_guids_header
    if is_admin_header:
        headers["X-Is-Admin"] = "1"
    if extra_headers:
        headers.update(extra_headers)

    results: list[FanoutResult] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(_call_one, ep, method, path, params, json, headers, timeout): ep
            for ep in targets
        }
        for fut in as_completed(futures):
            results.append(fut.result())

    # Sort by registry order so callers can reason about layout.
    by_id = {r.cdr_id: r for r in results}
    ordered = [by_id[ep.cdr_id] for ep in targets if ep.cdr_id in by_id]

    succeeded = [r.cdr_id for r in ordered if r.ok]
    failed = [r.cdr_id for r in ordered if not r.ok]

    if not failed:
        mode = "complete"
    elif len(succeeded) > len(failed):
        mode = "degraded"
    else:
        mode = "error"

    return FanoutResponse(
        mode=mode,
        results=ordered,
        succeeded=succeeded,
        failed=failed,
    )


def _call_one(ep: CdrEndpoint, method: str, path: str,
              params: dict | None, json: dict | None,
              headers: dict, timeout: float) -> FanoutResult:
    import time
    url = f"{ep.base_url}/{path.lstrip('/')}"
    t0 = time.monotonic()
    try:
        resp = requests.request(
            method=method,
            url=url,
            params=params,
            json=json,
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException as e:
        elapsed = int((time.monotonic() - t0) * 1000)
        return FanoutResult(
            cdr_id=ep.cdr_id,
            base_url=ep.base_url,
            region_label=ep.region_label,
            ok=False,
            status_code=0,
            body=None,
            elapsed_ms=elapsed,
            error=str(e),
        )
    elapsed = int((time.monotonic() - t0) * 1000)
    body = None
    try:
        body = resp.json()
    except (ValueError,):
        body = {"_raw": resp.text[:500]}

    return FanoutResult(
        cdr_id=ep.cdr_id,
        base_url=ep.base_url,
        region_label=ep.region_label,
        ok=200 <= resp.status_code < 300,
        status_code=resp.status_code,
        body=body,
        elapsed_ms=elapsed,
        error=None if (200 <= resp.status_code < 300) else f"{resp.status_code}",
    )


# ---------------------------------------------------------------------------
# Aggregators
# ---------------------------------------------------------------------------

def merge_histograms(per_cdr: list[FanoutResult],
                      *,
                      buckets: int = 20) -> dict:
    """Merge per-CDR ``$stats`` Parameters bodies into one histogram +
    summary stats.

    cdr.pdhc returns ``Parameters`` with parts:
      n / min / max / mean / sd / p25 / p50 / p75 / histogram

    We merge by:
      - global min/max from all per-CDR mins / maxes
      - re-bucket each CDR's histogram into a uniform [global_min,
        global_max] grid with ``buckets`` slots
      - sum bucket counts across CDRs
      - n = sum(n_cdr); mean / sd are recomputed from per-CDR mean+n+sd
        using parallel-variance combine (so we don't need raw values)

    Returns a JSON-shaped result with the merged histogram, per-CDR
    breakdown, and the union summary stats.
    """
    contributions: list[tuple[str, dict]] = []
    for r in per_cdr:
        if not r.ok or not isinstance(r.body, dict):
            continue
        params_list = r.body.get("parameter") or []
        params = {p.get("name"): p for p in params_list}
        if not params.get("n"):
            continue
        contributions.append((r.cdr_id, params))

    if not contributions:
        return {"n": 0, "buckets": [], "per_cdr": [], "min": None, "max": None}

    # Per-CDR scalars
    per_cdr_summary = []
    n_total = 0
    for cdr_id, params in contributions:
        n_i = int(params.get("n", {}).get("valueInteger", 0))
        mean_i = _decimal_or_none(params.get("mean"))
        sd_i = _decimal_or_none(params.get("sd")) or 0.0
        min_i = _decimal_or_none(params.get("min"))
        max_i = _decimal_or_none(params.get("max"))
        per_cdr_summary.append({
            "cdr_id": cdr_id,
            "n": n_i, "mean": mean_i, "sd": sd_i,
            "min": min_i, "max": max_i,
        })
        n_total += n_i

    # Global min/max — needed before re-bucketing.
    mins = [s["min"] for s in per_cdr_summary if s["min"] is not None]
    maxs = [s["max"] for s in per_cdr_summary if s["max"] is not None]
    if not mins or not maxs or n_total == 0:
        return {"n": 0, "buckets": [], "per_cdr": per_cdr_summary,
                "min": None, "max": None}
    global_min = min(mins)
    global_max = max(maxs)
    if global_max == global_min:
        # Degenerate single-value cohort: one bucket with all the n.
        merged = [{"low": global_min, "high": global_max, "count": n_total}]
        return _stats_summary(per_cdr_summary, merged, n_total, global_min, global_max)

    width = (global_max - global_min) / buckets
    counts = [0] * buckets
    for cdr_id, params in contributions:
        cdr_buckets = _parse_histogram_part(params.get("histogram", {}))
        for low, high, count in cdr_buckets:
            mid = (low + high) / 2.0
            idx = int((mid - global_min) / width)
            idx = max(0, min(buckets - 1, idx))
            counts[idx] += count

    merged = []
    for i in range(buckets):
        merged.append({
            "low": global_min + i * width,
            "high": global_min + (i + 1) * width,
            "count": counts[i],
        })

    return _stats_summary(per_cdr_summary, merged, n_total, global_min, global_max)


def _decimal_or_none(part: dict | None):
    if not part:
        return None
    return part.get("valueDecimal")


def _parse_histogram_part(part: dict) -> list[tuple[float, float, int]]:
    """cdr.pdhc histogram parts come back as:
        {"name": "histogram", "part": [{"name":"bucket_0", "valueString":"[low,high):count"}, ...]}
    """
    out = []
    for sub in part.get("part") or []:
        s = sub.get("valueString", "")
        try:
            range_part, count_part = s.rsplit(":", 1)
            low_str, high_str = range_part.strip("[)").split(",", 1)
            out.append((float(low_str), float(high_str), int(count_part)))
        except (ValueError, IndexError):
            continue
    return out


def _stats_summary(per_cdr_summary, merged, n_total, global_min, global_max):
    """Combine per-CDR mean/sd into population-level mean+sd via the
    parallel-variance algorithm (Chan / Welford form): for each CDR
    contribution k with n_k, mean_k, var_k, the running mean / m2 /
    n update is well-defined.
    """
    n = 0
    mean = 0.0
    m2 = 0.0
    for s in per_cdr_summary:
        n_i = s["n"]
        if n_i <= 0 or s["mean"] is None:
            continue
        mean_i = float(s["mean"])
        var_i = float(s["sd"]) ** 2
        m2_i = var_i * n_i  # population variance: var_i * n_i
        delta = mean_i - mean
        new_n = n + n_i
        if new_n == 0:
            continue
        mean = (n * mean + n_i * mean_i) / new_n
        m2 = m2 + m2_i + (delta ** 2) * n * n_i / new_n
        n = new_n
    sd = (m2 / n) ** 0.5 if n > 0 else 0.0
    return {
        "n": n_total,
        "min": global_min,
        "max": global_max,
        "mean": mean,
        "sd": sd,
        "buckets": merged,
        "per_cdr": per_cdr_summary,
    }


def concat_series(per_cdr: list[FanoutResult]) -> list[dict]:
    """Concatenate per-CDR Bundle entries, tagging each with its source.

    Used by the researcher trend / scatter paths so the UI can colour
    points by region.
    """
    out: list[dict] = []
    for r in per_cdr:
        if not r.ok or not isinstance(r.body, dict):
            continue
        for entry in r.body.get("entry") or []:
            res = entry.get("resource") or {}
            res = dict(res)
            res["_cdr_id"] = r.cdr_id
            res["_region_label"] = r.region_label
            out.append(res)
    return out


# ---------------------------------------------------------------------------
# Largest-triangle-three-buckets downsample (§4.2.c)
# ---------------------------------------------------------------------------

def lttb_downsample(points: list[tuple[float, float]],
                     *,
                     target: int = 2000) -> list[tuple[float, float]]:
    """Downsample a (x, y) series to ``target`` points, preserving
    extrema via Steinarsson's largest-triangle-three-buckets.

    Implementation follows the canonical LTTB sketch:
      - keep the first and last points unchanged
      - divide the middle into target-2 buckets
      - for each bucket, pick the point that forms the largest triangle
        with the previous chosen point and the average of the next bucket

    Time series too small to need downsampling are returned unchanged.
    """
    n = len(points)
    if target >= n or target < 3:
        return list(points)

    bucket_size = (n - 2) / (target - 2)
    sampled = [points[0]]
    a = 0  # index of previously selected point
    for i in range(target - 2):
        # Range of the next bucket (i+1) — used to compute its avg.
        avg_start = int((i + 1) * bucket_size) + 1
        avg_end = int((i + 2) * bucket_size) + 1
        avg_end = min(avg_end, n)
        if avg_end <= avg_start:
            avg_x = points[-1][0]
            avg_y = points[-1][1]
        else:
            avg_x = sum(p[0] for p in points[avg_start:avg_end]) / (avg_end - avg_start)
            avg_y = sum(p[1] for p in points[avg_start:avg_end]) / (avg_end - avg_start)

        # Range of the current bucket (i)
        cur_start = int(i * bucket_size) + 1
        cur_end = int((i + 1) * bucket_size) + 1
        cur_end = min(cur_end, n)
        max_area = -1.0
        max_idx = cur_start
        a_x, a_y = points[a]
        for j in range(cur_start, cur_end):
            jx, jy = points[j]
            area = abs(
                (a_x - avg_x) * (jy - a_y) - (a_x - jx) * (avg_y - a_y)
            ) / 2.0
            if area > max_area:
                max_area = area
                max_idx = j
        sampled.append(points[max_idx])
        a = max_idx
    sampled.append(points[-1])
    return sampled


# ---------------------------------------------------------------------------
# AGP — hourly bands from a 14d/90d CGM window (§4.2.b, §4.8)
# ---------------------------------------------------------------------------

def agp_hourly_bands(cgm_points: list[tuple[float, float]]) -> dict:
    """Compute the ambulatory glucose profile from a list of
    ``(timestamp_unix_seconds, glucose_value)`` points.

    Returns:
      - ``bands[0..23]`` — per-hour 5/25/50/75/95 percentiles
      - summary: TIR (70-180) %, TBR (<70) %, TAR (>180) %, mean, CV, hypo events

    Uses pure-python (statistics module) to keep runtime light — no
    numpy / pandas dependency at import time.
    """
    import statistics
    from collections import defaultdict

    by_hour: dict[int, list[float]] = defaultdict(list)
    for ts, val in cgm_points:
        # Map timestamp → hour-of-day.
        hour = int((ts // 3600) % 24)
        by_hour[hour].append(float(val))

    bands = []
    for h in range(24):
        vals = sorted(by_hour.get(h, []))
        if not vals:
            bands.append({"hour": h, "p5": None, "p25": None,
                          "p50": None, "p75": None, "p95": None})
            continue
        bands.append({
            "hour": h,
            "p5": _percentile(vals, 5),
            "p25": _percentile(vals, 25),
            "p50": _percentile(vals, 50),
            "p75": _percentile(vals, 75),
            "p95": _percentile(vals, 95),
        })

    all_vals = [v for vs in by_hour.values() for v in vs]
    if not all_vals:
        return {"bands": bands, "summary": {
            "n": 0, "tir": None, "tbr": None, "tar": None,
            "mean": None, "cv": None, "hypo_events": 0,
        }}

    # TIR/TBR/TAR thresholds in mmol/L (Sweden / IFCC convention).
    # Equivalents in mg/dL would be 70/180.
    n = len(all_vals)
    tir = sum(1 for v in all_vals if 3.9 <= v <= 10.0) / n * 100
    tbr = sum(1 for v in all_vals if v < 3.9) / n * 100
    tar = sum(1 for v in all_vals if v > 10.0) / n * 100
    mean = statistics.fmean(all_vals)
    sd = statistics.pstdev(all_vals)
    cv = (sd / mean * 100) if mean else 0.0
    hypo_events = _count_hypo_events([v for _, v in cgm_points])
    return {
        "bands": bands,
        "summary": {
            "n": n, "tir": tir, "tbr": tbr, "tar": tar,
            "mean": mean, "cv": cv, "hypo_events": hypo_events,
        },
    }


def _percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return float("nan")
    if len(sorted_values) == 1:
        return sorted_values[0]
    import math
    idx = (p / 100.0) * (len(sorted_values) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (idx - lo)


def _count_hypo_events(values: list[float], *, threshold: float = 3.9,
                       gap: int = 6) -> int:
    """Same gap-based event counting as sim.pdhc CGM engine."""
    n = 0
    in_event = False
    above_streak = 0
    for v in values:
        if v < threshold:
            if not in_event:
                n += 1
                in_event = True
            above_streak = 0
        else:
            if in_event:
                above_streak += 1
                if above_streak >= gap:
                    in_event = False
                    above_streak = 0
    return n
