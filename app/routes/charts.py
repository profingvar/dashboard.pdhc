"""Clinical per-patient charts view + JSON proxies (#464 D2 + #466 D4).

The patient page is a shell; the browser fetches parameters + series from
these dashboard endpoints, which in turn read CDR1 under the care-delivery
basis (Cdr1Client) and apply spärr on this side (operator #469 Q1). The
browser never talks to CDR1 directly (no service key in the browser).

Routes (all care-delivery gated — /patient/* and /api/v1/patient/* are
clinical paths in app.auth):
  GET /patient/<guid>/charts               — the charting page shell
  GET /api/v1/patient/<guid>/parameters    — sorted concept list (dropdown)
  GET /api/v1/patient/<guid>/series         — points for chosen concepts/window
"""
from __future__ import annotations

from flask import Blueprint, current_app, g, jsonify, render_template, request

from app.auth import org_guids_for
from app.services.audit import audit_read
from app.services.cdr1_client import build_client
from app.services.ips_client import (
    get_active_blocks,
    blocked_clinic_ids,
    has_any_active_block,
    filter_blocked_points,
    concept_guid_from_canonical,
)


def _lift_exposure_audit(exposures):
    """Q4 (#472) audit detail: the lift reference for each exposure, aggregated
    by lift (blocked clinic + window). Reader + patient + role are already on
    the audit row; this adds *which* lift exposed *what*."""
    by_lift: dict = {}
    for p, lift in exposures:
        key = (lift.source_scope_id, lift.lift_from_date, lift.lift_until_date)
        e = by_lift.setdefault(key, {
            "blocked_clinic": lift.source_scope_id,
            "lift_kind": lift.lift_kind,
            "lift_from_date": lift.lift_from_date,
            "lift_until_date": lift.lift_until_date,
            "exposed_concepts": set(),
            "exposed_points": 0,
        })
        cg = concept_guid_from_canonical(p.get("code") or p.get("code_canonical"))
        if cg:
            e["exposed_concepts"].add(cg)
        e["exposed_points"] += 1
    return [{**v, "exposed_concepts": sorted(v["exposed_concepts"])}
            for v in by_lift.values()]

bp = Blueprint("charts", __name__)


def _scope():
    user = g.current_user
    return org_guids_for(user), bool(getattr(user, "is_admin", False))


@bp.get("/patient/<guid>/charts")
@audit_read
def charts_page(guid):
    # Shell only; the data endpoints below are each audited on fetch.
    g._audit_n_rows = 0
    blocks = get_active_blocks(guid)
    return render_template(
        "charts.html",
        patient_guid=guid,
        has_blocked_sources=has_any_active_block(blocks),
        cdr1_configured=bool(build_client().base_url),
    )


@bp.get("/api/v1/patient/<guid>/parameters")
@audit_read
def parameters(guid):
    orgs, is_admin = _scope()
    params = build_client().patient_summary(guid, orgs, is_admin=is_admin)
    g._audit_n_rows = len(params)
    return jsonify(patient_guid=guid, parameters=params)


@bp.get("/api/v1/patient/<guid>/series")
@audit_read
def series(guid):
    orgs, is_admin = _scope()
    codes = [c for c in request.args.getlist("code") if c]
    frm = request.args.get("from")
    to = request.args.get("to")
    pts = build_client().patient_series(
        guid, codes, frm, to, orgs, is_admin=is_admin,
    )
    # Spärr (operator #469 Q1): drop points from clinics the patient has
    # blocked. Two modes:
    #  - default (SPARR_LIFT_ENABLED off): COARSE org-level hide — drop every
    #    point from a blocked clinic (safe, over-hides).
    #  - SPARR_LIFT_ENABLED (#471.4, DPO-approved #472): apply the
    #    indispensable-care LIFT — a blocked point is exposed iff an active
    #    indispensable_care lift on its clinic covers its concept (parsed from
    #    code_canonical) AND its date; non-parseable code stays hidden. Each
    #    read that exposes lifted data is specially audited (Q4).
    blocks = get_active_blocks(guid)
    blocked = blocked_clinic_ids(blocks)
    if blocked:
        if current_app.config.get("SPARR_LIFT_ENABLED"):
            pts, exposures = filter_blocked_points(pts, blocks)
            if exposures:
                g._audit_event_type = "sparr_lift_exposure"
                g._audit_payload_snapshot = {
                    "sparr_lift": _lift_exposure_audit(exposures),
                }
        else:
            pts = [p for p in pts if p.get("org_guid") not in blocked]
    g._audit_n_rows = len(pts)
    return jsonify(
        patient_guid=guid, points=pts,
        has_blocked_sources=has_any_active_block(blocks),
    )
