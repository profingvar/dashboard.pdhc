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

from flask import Blueprint, g, jsonify, render_template, request

from app.auth import org_guids_for
from app.services.audit import audit_read
from app.services.cdr1_client import build_client
from app.services.ips_client import (
    get_active_blocks,
    blocked_clinic_ids,
    has_any_active_block,
)

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
    # blocked. Coarse org-level filter — lift refinement is deferred because
    # spärr's lift_concept_guids are plan.pdhc Concept guids while CDR
    # points carry code_canonical (a URI); mapping the two is follow-up
    # work. Filtering toward hiding is the safe direction.
    blocks = get_active_blocks(guid)
    blocked = blocked_clinic_ids(blocks)
    if blocked:
        pts = [p for p in pts if p.get("org_guid") not in blocked]
    g._audit_n_rows = len(pts)
    return jsonify(
        patient_guid=guid, points=pts,
        has_blocked_sources=has_any_active_block(blocks),
    )
