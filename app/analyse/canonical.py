"""Federated canonical-table query — analyse-layer aggregator.

Phase 6 of the cdr1 SSOT cutover (ticket #292,
plans/cdr1_analyse_split_plan.md §5). Used to live on cdr1 at
``GET /api/v1/canonical/<table_name>``. Moved here so a single
analyse-layer query reaches the patient's rows wherever they live
across CDR1–6.

Endpoint
========
``GET /api/v1/canonical/<table_name>?patient_guid=<p>[&metric=<m>&limit=N&offset=N]``

- ``table_name`` (path) — must be one of ``health_observations`` or
  ``activities``. Same allowlist cdr1 enforced.
- ``patient_guid`` (required) — patient to scope the query to.
- ``metric`` (optional) — filter on the row's metric/activity_type.
- ``limit`` / ``offset`` — pagination, capped at 500/request.

Behaviour
---------
Fan out to all registered CDRs with the same query. Concatenate
the returned ``rows`` arrays, tagging each row with its source
``_cdr_id`` so consumers can attribute rows to regions. Total
across CDRs is reported as the sum of per-CDR totals.
"""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request, g

from app.analyse.federation import CdrRegistry, fanout


bp = Blueprint("analyse_canonical", __name__)

_ALLOWED_TABLES = {"health_observations", "activities"}


@bp.get("/api/v1/canonical/<table_name>")
def query_table(table_name):
    blob = getattr(g, "access_blob", None) or {}
    if not blob.get("service_source"):
        return jsonify({"error": "service-key auth required"}), 401
    if blob.get("service_source") not in {"gateway.pdhc", "monitor.pdhc"}:
        return jsonify({"error": "source service not allowed for this endpoint"}), 403

    if table_name not in _ALLOWED_TABLES:
        return jsonify({"error": f"unknown table: {table_name}"}), 404

    patient = (request.args.get("patient_guid") or "").strip()
    if not patient:
        return jsonify({"error": "patient_guid required"}), 400

    limit = min(int(request.args.get("limit", 100)), 500)
    offset = int(request.args.get("offset", 0))
    metric = (request.args.get("metric") or "").strip() or None

    registry = CdrRegistry.from_config(current_app.config)
    if not registry.all:
        return jsonify({
            "table": table_name,
            "patient_guid": patient,
            "total": 0,
            "rows": [],
            "cdrs_total": 0,
            "cdrs_responded": 0,
        }), 200

    params = {"patient_guid": patient, "limit": limit, "offset": offset}
    if metric:
        params["metric"] = metric

    response = fanout(
        registry,
        method="GET",
        path=f"/api/v1/canonical/{table_name}",
        params=params,
    )

    merged_rows: list[dict] = []
    responded = 0
    for r in response.results:
        if not r.ok or not r.body:
            continue
        responded += 1
        rows = r.body.get("rows") or []
        for row in rows:
            tagged = dict(row)
            tagged["_cdr_id"] = r.cdr_id
            merged_rows.append(tagged)

    return jsonify({
        "table": table_name,
        "patient_guid": patient,
        "total": len(merged_rows),
        "rows": merged_rows,
        "cdrs_total": len(registry.all),
        "cdrs_responded": responded,
    }), 200
