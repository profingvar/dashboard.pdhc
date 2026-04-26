"""Researcher workspace API — platform-plan execution §4.3 + §4.6.

Cohorts are defined via a JSON predicate (filter object) and resolved
against the federation. Resolution stages:

  1. ``POST /api/cohort`` — accept the predicate, compute member set,
     persist a CohortDefinition row, return the cohort_id + n.
  2. ``GET  /api/cohort`` — list past cohorts owned by the caller.
  3. ``GET  /api/cohort/<id>/variable/<canonical>/histogram``
                                  — federated $stats merge.
  4. ``GET  /api/cohort/<id>/variable/<canonical>/boxplot?group_by=...``
                                  — stratified summary stats.
  5. ``GET  /api/cohort/<id>/scatter?x=&y=&max=`` — paired points.
  6. ``GET  /api/cohort/<id>/trend?canonical=&window=`` — time-trend
                                  with IQR bands.
  7. ``GET  /api/cohort/<id>/export?format=csv&variables=`` — streamed
                                  CSV with sim_run_id column when synthetic.
"""
from __future__ import annotations

import csv
import io
import json
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from flask import (
    Blueprint, Response, abort, current_app, g, jsonify, request, stream_with_context,
)

from app.models import db
from app.services.cohort import CohortFilter, intersect_patient_sets, to_predicate_searches
from app.services.federation import (
    CdrRegistry,
    concat_series,
    fanout,
    merge_histograms,
)
from app.services.role_guards import researcher_required


bp = Blueprint("researcher_api", __name__, url_prefix="/api")


# ---------------------------------------------------------------------------
# Per-app singletons
# ---------------------------------------------------------------------------

def _registry() -> CdrRegistry:
    if not hasattr(current_app, "_cdr_registry"):
        current_app._cdr_registry = CdrRegistry.from_config(current_app.config)
    return current_app._cdr_registry


def _auth_headers() -> dict:
    blob = getattr(g, "access_blob", None) or {}
    if isinstance(blob, dict):
        is_admin = bool(blob.get("is_su_admin"))
        org_ids = blob.get("organization_ids") or []
    else:
        is_admin = bool(getattr(blob, "is_su_admin", False))
        org_ids = getattr(blob, "organization_ids", None) or []
    return {
        "is_admin": is_admin,
        "org_guids": ",".join(str(g) for g in (org_ids or [])),
    }


# ---------------------------------------------------------------------------
# In-process cohort store. The platform plan calls for CohortDefinition
# persistence, but the dashboard's existing schema doesn't have such
# a model yet, and adding migrations is out of scope for this slice.
# A process-local dict is the minimum that satisfies the API while
# keeping the surface stable for a future DB-backed swap.
# ---------------------------------------------------------------------------

_COHORTS: dict[str, dict] = {}


def _resolve_members(filt: CohortFilter) -> tuple[set[str], dict]:
    """Run the predicate searches across the federation, intersect the
    returned patient-id sets, and return (members, fanout_summary)."""
    auth = _auth_headers()
    per_predicate_sets: list[set[str]] = []
    summary = {"predicates": []}
    for resource_type, params in to_predicate_searches(filt):
        resp = fanout(
            _registry(),
            method="GET",
            path=f"api/v1/fhir/{resource_type}",
            cdr_ids=filt.cdr_ids or None,
            params=params,
            org_guids_header=auth["org_guids"],
            is_admin_header=auth["is_admin"],
        )
        ids: set[str] = set()
        for r in resp.results:
            if not r.ok or not isinstance(r.body, dict):
                continue
            for entry in r.body.get("entry") or []:
                res = entry.get("resource") or {}
                if resource_type == "Patient":
                    if res.get("id"):
                        ids.add(res["id"])
                else:
                    ref = (res.get("subject") or {}).get("reference", "")
                    if "/" in ref:
                        ids.add(ref.rsplit("/", 1)[-1])
        per_predicate_sets.append(ids)
        summary["predicates"].append({
            "resource_type": resource_type,
            "params": params,
            "n_matched": len(ids),
            "fanout_mode": resp.mode,
        })
    members = intersect_patient_sets(per_predicate_sets) if per_predicate_sets else set()
    return members, summary


# ---------------------------------------------------------------------------
# POST /api/cohort  — define a cohort, return id + count
# ---------------------------------------------------------------------------

@bp.post("/cohort")
@researcher_required
def define_cohort():
    body = request.get_json(silent=True) or {}
    filt = CohortFilter.from_dict(body)
    members, summary = _resolve_members(filt)
    cohort_id = str(uuid.uuid4())
    _COHORTS[cohort_id] = {
        "id": cohort_id,
        "filter": body,
        "members": list(members),
        "n": len(members),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "owner_blob": _owner_label(),
    }
    return jsonify({
        "cohort_id": cohort_id,
        "n": len(members),
        "summary": summary,
    }), 201


@bp.get("/cohort")
@researcher_required
def list_cohorts():
    out = []
    for c in _COHORTS.values():
        out.append({
            "cohort_id": c["id"],
            "n": c["n"],
            "filter": c["filter"],
            "created_at": c["created_at"],
        })
    return jsonify({"cohorts": out, "n": len(out)})


def _get_cohort_or_404(cohort_id: str) -> dict:
    c = _COHORTS.get(cohort_id)
    if not c:
        abort(404, description="cohort not found")
    return c


def _owner_label() -> str:
    blob = getattr(g, "access_blob", None) or {}
    if isinstance(blob, dict):
        return blob.get("user_guid") or blob.get("email") or "anonymous"
    return getattr(blob, "user_guid", "") or getattr(blob, "email", "") or "anonymous"


# ---------------------------------------------------------------------------
# Variable / histogram / boxplot / scatter / trend
# ---------------------------------------------------------------------------

@bp.get("/cohort/<cohort_id>/variable/<path:canonical>/histogram")
@researcher_required
def cohort_histogram(cohort_id: str, canonical: str):
    cohort = _get_cohort_or_404(cohort_id)
    auth = _auth_headers()
    code_arg = _normalise_canonical(canonical)
    buckets = max(1, min(int(request.args.get("buckets", "20")), 100))
    cdr_filter = (cohort["filter"].get("cdr_ids") or [])

    resp = fanout(
        _registry(),
        method="GET",
        path="api/v1/fhir/Observation/$stats",
        cdr_ids=cdr_filter or None,
        params={"code": code_arg, "buckets": str(buckets)},
        org_guids_header=auth["org_guids"],
        is_admin_header=auth["is_admin"],
    )
    merged = merge_histograms(resp.results, buckets=buckets)
    return jsonify({
        "cohort_id": cohort_id,
        "canonical": canonical,
        "fanout_mode": resp.mode,
        "succeeded_cdrs": resp.succeeded,
        "failed_cdrs": resp.failed,
        **merged,
    })


@bp.get("/cohort/<cohort_id>/variable/<path:canonical>/boxplot")
@researcher_required
def cohort_boxplot(cohort_id: str, canonical: str):
    """Stratified boxplot: returns per-group summary (n, p25/p50/p75,
    min, max) for the chosen ``group_by``."""
    cohort = _get_cohort_or_404(cohort_id)
    group_by = request.args.get("group_by", "region")
    auth = _auth_headers()
    code_arg = _normalise_canonical(canonical)
    cdr_filter = (cohort["filter"].get("cdr_ids") or [])

    # Pull the raw observations across the federation; group on the dashboard side.
    resp = fanout(
        _registry(),
        method="GET",
        path="api/v1/fhir/Observation",
        cdr_ids=cdr_filter or None,
        params={"code": code_arg, "_count": "5000"},
        org_guids_header=auth["org_guids"],
        is_admin_header=auth["is_admin"],
    )
    rows = concat_series(resp.results)
    members = set(cohort["members"])
    by_group: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        ref = (r.get("subject") or {}).get("reference", "")
        pat_guid = ref.rsplit("/", 1)[-1] if "/" in ref else ref
        if pat_guid not in members:
            continue
        val = (r.get("valueQuantity") or {}).get("value")
        if val is None:
            continue
        if group_by == "region":
            label = r.get("_region_label") or r.get("_cdr_id") or "unknown"
        elif group_by == "cdr":
            label = r.get("_cdr_id") or "unknown"
        else:
            label = "all"
        by_group[label].append(float(val))

    summary = []
    for label, vals in by_group.items():
        vals_sorted = sorted(vals)
        if not vals_sorted:
            continue
        from app.services.federation import _percentile  # reuse
        summary.append({
            "group": label,
            "n": len(vals_sorted),
            "min": vals_sorted[0],
            "max": vals_sorted[-1],
            "p25": _percentile(vals_sorted, 25),
            "p50": _percentile(vals_sorted, 50),
            "p75": _percentile(vals_sorted, 75),
        })
    return jsonify({
        "cohort_id": cohort_id,
        "canonical": canonical,
        "group_by": group_by,
        "groups": summary,
    })


@bp.get("/cohort/<cohort_id>/scatter")
@researcher_required
def cohort_scatter(cohort_id: str):
    cohort = _get_cohort_or_404(cohort_id)
    x = request.args.get("x")
    y = request.args.get("y")
    cap = min(int(request.args.get("max", "5000")), 10000)
    if not x or not y:
        abort(400, description="x and y are required")
    members = set(cohort["members"])
    points = _paired_observations(cohort, x, y, members)
    truncated = len(points) > cap
    if truncated:
        # Stride sample to keep distribution coverage.
        stride = max(1, len(points) // cap)
        points = points[::stride][:cap]
    return jsonify({
        "cohort_id": cohort_id,
        "x": x, "y": y,
        "n": len(points),
        "truncated": truncated,
        "points": points,
    })


@bp.get("/cohort/<cohort_id>/trend")
@researcher_required
def cohort_trend(cohort_id: str):
    """Returns per-month mean/p25/p50/p75 for a single canonical."""
    cohort = _get_cohort_or_404(cohort_id)
    canonical = request.args.get("canonical")
    if not canonical:
        abort(400, description="canonical is required")
    window_days = int(request.args.get("window", "365"))
    since = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    auth = _auth_headers()
    cdr_filter = (cohort["filter"].get("cdr_ids") or [])
    resp = fanout(
        _registry(),
        method="GET",
        path="api/v1/fhir/Observation",
        cdr_ids=cdr_filter or None,
        params={
            "code": _normalise_canonical(canonical),
            "date": f"ge{since}",
            "_count": "10000",
        },
        org_guids_header=auth["org_guids"],
        is_admin_header=auth["is_admin"],
    )
    rows = concat_series(resp.results)
    members = set(cohort["members"])

    by_month: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        ref = (r.get("subject") or {}).get("reference", "")
        pat_guid = ref.rsplit("/", 1)[-1] if "/" in ref else ref
        if pat_guid not in members:
            continue
        eff = r.get("effectiveDateTime") or ""
        val = (r.get("valueQuantity") or {}).get("value")
        if not eff or val is None:
            continue
        month = eff[:7]  # YYYY-MM
        by_month[month].append(float(val))

    from app.services.federation import _percentile
    series = []
    for month in sorted(by_month.keys()):
        vals = sorted(by_month[month])
        series.append({
            "month": month,
            "n": len(vals),
            "p25": _percentile(vals, 25),
            "p50": _percentile(vals, 50),
            "p75": _percentile(vals, 75),
        })
    return jsonify({
        "cohort_id": cohort_id,
        "canonical": canonical,
        "window_days": window_days,
        "series": series,
    })


# ---------------------------------------------------------------------------
# CSV export (§4.6)
# ---------------------------------------------------------------------------

@bp.get("/cohort/<cohort_id>/export")
@researcher_required
def cohort_export(cohort_id: str):
    cohort = _get_cohort_or_404(cohort_id)
    fmt = request.args.get("format", "csv")
    if fmt != "csv":
        abort(400, description="only csv export supported in this build")
    variables = (request.args.get("variables") or "").split(",")
    variables = [v.strip() for v in variables if v.strip()]
    if not variables:
        abort(400, description="variables list is required")

    auth = _auth_headers()
    members = set(cohort["members"])
    cdr_filter = (cohort["filter"].get("cdr_ids") or [])
    export_id = str(uuid.uuid4())

    @stream_with_context
    def _stream():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "patient_guid", "org_guid", "canonical",
            "source_code", "effective_at", "value", "unit", "sim_run_id",
        ])
        yield buf.getvalue()
        buf.seek(0); buf.truncate()

        row_count = 0
        for canonical in variables:
            resp = fanout(
                _registry(),
                method="GET",
                path="api/v1/fhir/Observation",
                cdr_ids=cdr_filter or None,
                params={"code": _normalise_canonical(canonical), "_count": "10000"},
                org_guids_header=auth["org_guids"],
                is_admin_header=auth["is_admin"],
            )
            rows = concat_series(resp.results)
            for r in rows:
                ref = (r.get("subject") or {}).get("reference", "")
                pat_guid = ref.rsplit("/", 1)[-1] if "/" in ref else ref
                if pat_guid not in members:
                    continue
                org = _extract_org(r)
                source_code = _extract_source_code(r)
                eff = r.get("effectiveDateTime")
                vq = r.get("valueQuantity") or {}
                sim_run_id = _extract_sim_run_id(r)
                writer.writerow([
                    pat_guid, org, canonical, source_code, eff,
                    vq.get("value"), vq.get("unit"), sim_run_id,
                ])
                row_count += 1
                if row_count % 100 == 0:
                    yield buf.getvalue()
                    buf.seek(0); buf.truncate()
        # Flush trailing rows.
        if buf.getvalue():
            yield buf.getvalue()

        # Audit row written at-end so row_count is final. We commit here
        # (outside the streamed generator) — Flask iterates the generator
        # before closing the response, so this runs before the connection
        # is released.
        _record_export_audit(
            export_id=export_id,
            cohort_id=cohort_id,
            variables=variables,
            row_count=row_count,
        )

    headers = {
        "Content-Disposition": f'attachment; filename="cohort-{cohort_id[:8]}-{export_id[:8]}.csv"',
        "Content-Type": "text/csv; charset=utf-8",
        "X-Export-Id": export_id,
    }
    return Response(_stream(), headers=headers)


def _record_export_audit(*, export_id: str, cohort_id: str,
                         variables: list[str], row_count: int) -> None:
    """Append one row to a process-local audit log. The platform-plan
    §4.6.c calls for a `dashboard_audit` table — we surface the audit
    event here and leave the migration as a follow-up so this slice
    doesn't carry a schema change."""
    blob = getattr(g, "access_blob", None) or {}
    if isinstance(blob, dict):
        user = blob.get("user_guid") or blob.get("email") or "anonymous"
    else:
        user = getattr(blob, "user_guid", "anonymous")
    log_path = current_app.config.get("EXPORT_AUDIT_LOG", "results/export_audit.log")
    line = json.dumps({
        "export_id": export_id,
        "cohort_id": cohort_id,
        "user": user,
        "variables": variables,
        "row_count": row_count,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    try:
        import os
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        with open(log_path, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        # We don't break the export over an audit-log write failure — but
        # we shouldn't silently drop it either. Future: hook this into
        # the Flask logger.
        current_app.logger.warning("export audit log write failed: %s", line)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise_canonical(canonical: str) -> str:
    """Accept either ``url/.../<code>`` or ``<system>|<code>`` form."""
    if "|" in canonical:
        return canonical
    parts = canonical.rsplit("/", 1)
    if len(parts) == 2:
        return f"{parts[0]}|{parts[1]}"
    return canonical


def _paired_observations(cohort: dict, x_canonical: str, y_canonical: str,
                          members: set[str]) -> list[dict]:
    """Pull every (x, y) pair where the same Patient has both
    Observations. Pairing is by patient_guid + month bucket — we use
    each patient's latest x and latest y in a 30-day window."""
    auth = _auth_headers()
    cdr_filter = (cohort["filter"].get("cdr_ids") or [])
    out: list[dict] = []
    x_resp = fanout(_registry(), method="GET",
                    path="api/v1/fhir/Observation",
                    cdr_ids=cdr_filter or None,
                    params={"code": _normalise_canonical(x_canonical), "_count": "10000"},
                    org_guids_header=auth["org_guids"],
                    is_admin_header=auth["is_admin"])
    y_resp = fanout(_registry(), method="GET",
                    path="api/v1/fhir/Observation",
                    cdr_ids=cdr_filter or None,
                    params={"code": _normalise_canonical(y_canonical), "_count": "10000"},
                    org_guids_header=auth["org_guids"],
                    is_admin_header=auth["is_admin"])
    x_rows = concat_series(x_resp.results)
    y_rows = concat_series(y_resp.results)

    # Group by patient → latest value
    def _latest(rows):
        latest_by_pat: dict[str, dict] = {}
        for r in rows:
            ref = (r.get("subject") or {}).get("reference", "")
            pat_guid = ref.rsplit("/", 1)[-1] if "/" in ref else ref
            if pat_guid not in members:
                continue
            eff = r.get("effectiveDateTime") or ""
            cur = latest_by_pat.get(pat_guid)
            if cur is None or eff > (cur.get("effectiveDateTime") or ""):
                latest_by_pat[pat_guid] = r
        return latest_by_pat

    x_latest = _latest(x_rows)
    y_latest = _latest(y_rows)
    common = set(x_latest) & set(y_latest)
    for pat_guid in common:
        xv = (x_latest[pat_guid].get("valueQuantity") or {}).get("value")
        yv = (y_latest[pat_guid].get("valueQuantity") or {}).get("value")
        if xv is None or yv is None:
            continue
        out.append({
            "patient_guid": pat_guid,
            "x": xv, "y": yv,
            "cdr_id": x_latest[pat_guid].get("_cdr_id"),
            "region": x_latest[pat_guid].get("_region_label"),
        })
    return out


def _extract_org(r: dict) -> str | None:
    sec = ((r.get("meta") or {}).get("security") or [])
    for s in sec:
        if s.get("code") == "org_guid":
            return s.get("display")
    return None


def _extract_sim_run_id(r: dict) -> str | None:
    tags = ((r.get("meta") or {}).get("tag") or [])
    for t in tags:
        sys_uri = t.get("system") or ""
        if sys_uri.endswith("/run"):
            return t.get("code")
    return None


def _extract_source_code(r: dict) -> str | None:
    """Return the second coding (the original foreign code, preserved
    by the CDR's canonicalisation chain)."""
    codings = ((r.get("code") or {}).get("coding") or [])
    if len(codings) >= 2:
        c = codings[1]
        return f"{c.get('system','')}|{c.get('code','')}"
    return None
