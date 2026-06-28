"""Federated openEHR composition search — analyse-layer aggregator.

Phase 6 of the cdr1 SSOT cutover (ticket #292,
plans/cdr1_analyse_split_plan.md §5). Used to live on cdr1 at
``GET /api/v1/openehr/composition`` and
``GET /api/v1/openehr/ehr/<patient>/compositions``. Moved here for
federated patient lookups across CDR1–6. cdr1 still hosts the
storage-style by-GUID lookup at ``/api/v1/openehr/composition/<guid>``.

Endpoints
=========
``GET /api/v1/openehr/composition?patient=<p>[&archetype=<a>]``
``GET /api/v1/openehr/ehr/<patient>/compositions``

Behaviour
---------
Fan out to every registered CDR with the same params. Concatenate
each CDR's ``compositions`` array, tagging entries with ``_cdr_id``.
Stable ordering by registry order then by per-CDR response order.
"""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request, g

from app.analyse.federation import CdrRegistry, fanout


bp = Blueprint("analyse_openehr", __name__)


def _auth_ok(blob: dict) -> tuple[bool, tuple[dict, int] | None]:
    if not blob.get("service_source"):
        return False, ({"error": "service-key auth required"}, 401)
    if blob.get("service_source") not in {"gateway.pdhc", "monitor.pdhc"}:
        return False, ({"error": "source service not allowed for this endpoint"}, 403)
    return True, None


def _federated_search(path: str, params: dict, registry: CdrRegistry):
    response = fanout(
        registry,
        method="GET",
        path=path,
        params=params,
    )
    merged: list[dict] = []
    responded = 0
    for r in response.results:
        if not r.ok or not r.body:
            continue
        responded += 1
        comps = r.body.get("compositions") or []
        for comp in comps:
            tagged = dict(comp) if isinstance(comp, dict) else {"raw": comp}
            tagged["_cdr_id"] = r.cdr_id
            merged.append(tagged)
    return merged, responded


@bp.get("/api/v1/openehr/composition")
def search_compositions():
    blob = getattr(g, "access_blob", None) or {}
    ok, err = _auth_ok(blob)
    if not ok:
        return jsonify(err[0]), err[1]

    patient = (request.args.get("patient") or "").strip()
    if not patient:
        return jsonify({"error": "patient query parameter required"}), 400
    archetype = (request.args.get("archetype") or "").strip() or None

    registry = CdrRegistry.from_config(current_app.config)
    if not registry.all:
        return jsonify({
            "patient": patient,
            "total": 0,
            "compositions": [],
            "cdrs_total": 0,
            "cdrs_responded": 0,
        }), 200

    params = {"patient": patient}
    if archetype:
        params["archetype"] = archetype
    merged, responded = _federated_search(
        "/api/v1/openehr/composition", params, registry)
    return jsonify({
        "patient": patient,
        "total": len(merged),
        "compositions": merged,
        "cdrs_total": len(registry.all),
        "cdrs_responded": responded,
    }), 200


@bp.get("/api/v1/openehr/ehr/<patient>/compositions")
def patient_compositions(patient):
    blob = getattr(g, "access_blob", None) or {}
    ok, err = _auth_ok(blob)
    if not ok:
        return jsonify(err[0]), err[1]

    registry = CdrRegistry.from_config(current_app.config)
    if not registry.all:
        return jsonify({
            "patient": patient,
            "total": 0,
            "compositions": [],
            "cdrs_total": 0,
            "cdrs_responded": 0,
        }), 200

    merged, responded = _federated_search(
        f"/api/v1/openehr/ehr/{patient}/compositions", {}, registry)
    return jsonify({
        "patient": patient,
        "total": len(merged),
        "compositions": merged,
        "cdrs_total": len(registry.all),
        "cdrs_responded": responded,
    }), 200
