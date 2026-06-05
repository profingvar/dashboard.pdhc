"""Internal JSON API + FHIR CapabilityStatement."""
from __future__ import annotations

from flask import Blueprint, jsonify, request, abort, g
from app.models import ObservationCache
from app.auth import scope_to_user_orgs
from app.services.audit import audit_read
from app.services.ips_client import (
    get_active_blocks,
    filter_blocked_rows,
    has_any_active_block,
)

bp = Blueprint("api", __name__, url_prefix="/api/v1")


@bp.get("/series")
@audit_read
def series():
    patient = request.args.get("patient")
    concept = request.args.get("concept")
    if not (patient and concept):
        abort(400)
    q = ObservationCache.query.filter_by(patient_guid=patient, concept_guid=concept)
    q = scope_to_user_orgs(q, ObservationCache.org_guid)
    rows = q.order_by(ObservationCache.observed_at.asc()).all()
    # Spärr Phase 2 (ticket #205, PDL Ch 4 § 4).
    blocks = get_active_blocks(patient)
    rows = filter_blocked_rows(rows, blocks)
    bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(rows),
        "entry": [
            {"resource": {
                "resourceType": "Observation",
                "id": r.source_obs_guid,
                "status": "final",
                "subject": {"reference": f"Patient/{r.patient_guid}"},
                "code": {"coding": [{"code": r.concept_guid, "display": r.concept_name}]},
                "valueQuantity": {"value": r.value, "unit": r.unit},
                "effectiveDateTime": r.observed_at.isoformat(),
            }} for r in rows
        ],
    }
    if has_any_active_block(blocks):
        # PDL Ch 4 § 4 ¶ 3 metadata-only signal — the caller needs to know
        # that some sources are spärrade even if the filter dropped no
        # row this caller could see.
        bundle["meta"] = {
            "tag": [{
                "system": "urn:pdhc:pdl:sparr",
                "code": "blocked-sources-present",
                "display": (
                    "uppgift om att det finns spärrade uppgifter "
                    "hos vårdenheten"
                ),
            }]
        }
    return jsonify(bundle)


def register_metadata(app):
    @app.get("/metadata")
    def metadata():
        return jsonify({
            "resourceType": "CapabilityStatement",
            "status": "active",
            "kind": "instance",
            "fhirVersion": "5.0.0",
            "format": ["json"],
            "rest": [{
                "mode": "server",
                "resource": [{
                    "type": "Observation",
                    "interaction": [{"code": "search-type"}],
                    "searchParam": [
                        {"name": "patient", "type": "token"},
                        {"name": "concept", "type": "token"},
                    ],
                }],
                "operation": [
                    {"name": "healthz", "definition": "/healthz"},
                    {"name": "series", "definition": "/api/v1/series"},
                    {"name": "refresh", "definition": "/refresh"},
                ],
            }],
        })
