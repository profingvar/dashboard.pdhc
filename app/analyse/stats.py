"""Federated row-count stats — analyse-layer aggregator.

Phase 6 of the cdr1 SSOT cutover (ticket #292,
plans/cdr1_analyse_split_plan.md §5). Used to live on cdr1 at
``GET /api/v1/stats``. Moved here because the dashboard needs
row counts across every registered CDR, not just one.

Endpoint
========
``GET /api/v1/stats``

Behaviour
---------
Fan out a ``GET /api/v1/stats`` to every registered CDR in parallel.
Each CDR no longer exposes ``/api/v1/stats`` (the route was removed
in this same phase) — *but* during the transition deploy the route
may still exist on some CDRs. To survive the transition we sum
counts only from CDRs that return 200; degraded CDRs are recorded
as ``mode='degraded'`` with their stub fields zeroed.

Output preserves the same flat shape consumers expected from
cdr1's old ``/api/v1/stats``: a dict of count-per-table keys. Adds
two metadata fields ``cdrs_total`` and ``cdrs_responded`` so the
operator dashboard can tell how complete the picture is.
"""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify, g

from app.analyse.federation import CdrRegistry, fanout


bp = Blueprint("analyse_stats", __name__)

_COUNT_KEYS = (
    "ingest_raw",
    "fhir_resources",
    "openehr_compositions",
    "health_observations",
    "dedupe_registry",
    "patients",
)


@bp.get("/api/v1/stats")
def federated_stats():
    blob = getattr(g, "access_blob", None) or {}
    if not blob.get("service_source"):
        return jsonify({"error": "service-key auth required"}), 401
    if blob.get("service_source") not in {"gateway.pdhc", "monitor.pdhc"}:
        return jsonify({"error": "source service not allowed for this endpoint"}), 403

    registry = CdrRegistry.from_config(current_app.config)
    totals = {k: 0 for k in _COUNT_KEYS}
    if not registry.all:
        return jsonify({
            **totals,
            "cdrs_total": 0,
            "cdrs_responded": 0,
            "mode": "empty",
        }), 200

    response = fanout(
        registry,
        method="GET",
        path="/api/v1/stats",
        params=None,
    )

    responded = 0
    for r in response.results:
        if not r.ok or not r.body:
            continue
        responded += 1
        for k in _COUNT_KEYS:
            v = r.body.get(k)
            if isinstance(v, (int, float)):
                totals[k] += int(v)

    mode = "complete" if responded == len(registry.all) else (
        "degraded" if responded > 0 else "error"
    )

    return jsonify({
        **totals,
        "cdrs_total": len(registry.all),
        "cdrs_responded": responded,
        "mode": mode,
    }), 200
