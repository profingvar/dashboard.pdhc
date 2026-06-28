"""Gateway-facing observations search — the analyse-layer endpoint
gateway.pdhc's ``/api/v1/observations`` proxy now points to.

Phase 5 of the cdr1 SSOT cutover (ticket #291,
plans/cdr1_analyse_split_plan.md §5). Used to live on cdr1 (added in
#282). Moved here because federated cohort-search across CDR1–6 is the
analyse layer's job; cdr1 only knows about itself.

Endpoint
========
``GET /api/v1/observations?service_request=<sr_guid>[&service_request=...][&patient=<patient_guid>]``

- ``service_request`` (repeatable, required) — gateway has already
  resolved the org → contracts → SRs mapping and sends us the
  pre-computed SR guid list.
- ``patient`` (optional) — additional filter.

Auth: ``X-Source-Service: gateway.pdhc`` + ``X-Service-Key``.
Validated by ``app/auth.py``'s ``_service_key_outcome`` via the
``KNOWN_SERVICES`` map.

Behaviour
---------
- Fan out a ``GET /api/v1/fhir/Observation?patient=<p>&_count=10000``
  to every registered CDR in parallel (registry built from
  ``CDR_ENDPOINTS``).
- Each CDR replies with its own FHIR searchset Bundle of all
  matching-patient Observations (or all Observations if no patient
  filter — bounded by ``_count``).
- Locally filter each Bundle's entries by ``basedOn[*].identifier.value
  ∈ sr_guid_set`` — exactly the filter cdr1's removed endpoint did
  in SQL/Python. Same client-side helper.
- Merge filtered entries into one searchset Bundle. Order is the
  registry order so the consumer sees a stable layout.
"""
from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, request, g

from app.analyse.federation import CdrRegistry, fanout


bp = Blueprint("observations_search", __name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_bundle() -> dict:
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "timestamp": _now_iso(),
        "total": 0,
        "entry": [],
    }


def _resource_belongs_to_sr(resource_json: dict, sr_set: set[str]) -> bool:
    """True if any ``basedOn[*].identifier.value`` matches sr_set.

    Falls back to scanning ``basedOn[*].reference`` for ``/<guid>``
    suffix in case the identifier wasn't carried. Same shape cdr1's
    deleted ``observations_search.py`` used so the merged Bundle is
    byte-identical given identical inputs.
    """
    if not resource_json:
        return False
    for ref in resource_json.get("basedOn", []) or []:
        ident = (ref.get("identifier") or {}).get("value")
        if ident and ident in sr_set:
            return True
        ref_url = ref.get("reference") or ""
        if ref_url:
            tail = ref_url.rsplit("/", 1)[-1]
            if tail in sr_set:
                return True
    return False


@bp.get("/api/v1/observations")
def search_observations():
    # Service-key auth is enforced by the global request loader in
    # app.auth. We just check the caller landed in the service-key
    # path — anything else (SSO session, dev mode) is a misroute.
    blob = getattr(g, "access_blob", None) or {}
    if not blob.get("service_source"):
        return jsonify({"error": "service-key auth required"}), 401
    if blob.get("service_source") not in {"gateway.pdhc", "monitor.pdhc"}:
        return jsonify({"error": "source service not allowed for this endpoint"}), 403

    sr_guids = [s.strip() for s in request.args.getlist("service_request")
                if s.strip()]
    if not sr_guids:
        return jsonify({
            "error": "service_request query parameter is required "
                     "(repeatable)",
        }), 400

    patient = (request.args.get("patient") or "").strip() or None
    sr_set = set(sr_guids)

    registry = CdrRegistry.from_config(current_app.config)
    if not registry.all:
        return jsonify(_empty_bundle()), 200

    params: dict = {"_count": 10000}
    if patient:
        params["patient"] = patient

    correlation = request.headers.get("X-Correlation-Id") or "observations.search"
    extra_headers = {"X-Request-Id": correlation}

    response = fanout(
        registry,
        method="GET",
        path="/api/v1/fhir/Observation",
        params=params,
        extra_headers=extra_headers,
    )

    matched: list[dict] = []
    for r in response.results:
        if not r.ok or not r.body:
            continue
        for entry in (r.body.get("entry") or []):
            resource = entry.get("resource") or {}
            if _resource_belongs_to_sr(resource, sr_set):
                matched.append({"resource": resource})

    return jsonify({
        "resourceType": "Bundle",
        "type": "searchset",
        "timestamp": _now_iso(),
        "total": len(matched),
        "entry": matched,
    }), 200
