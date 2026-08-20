"""Clinical patient picker (#465 / #462 D3).

Lists the patients a signed-in clinician may choose, scoped to their
organisation affiliation (Rule 24 / ``auth.scope_org_guids``), read from
CDR1 (production) under a care-delivery basis. Picking a patient links to
the existing per-patient view.

The heavy lifting (org scoping, care-delivery vs analysis-consent) happens
on CDR1's side via the headers ``Cdr1Client`` sends; narrowing to
"patients with data" + ordering by data volume arrives with CDR1's
per-org patient-index summary (#468). Until then this shows the org's
patient set from CDR1's ``GET /Patient`` search.
"""
from __future__ import annotations

from flask import Blueprint, g, render_template, session

from app.auth import org_guids_for
from app.services.audit import audit_read
from app.services.cdr1_client import build_client

bp = Blueprint("picker", __name__)


@bp.get("/select")
@audit_read
def select():
    user = g.current_user
    is_admin = bool(getattr(user, "is_admin", False))
    orgs = org_guids_for(user)  # [] for admin = no restriction

    # #575 (#212 re-home): an admin's directory listing sees every patient
    # (org-scope override) — break-glass. Require a session attestation reason
    # before listing; render the attest form until it's provided.
    reason = (session.get("admin_read_reason") or "").strip() if is_admin else None
    if is_admin and not reason:
        g._audit_event_type = "admin_override_required"
        g._audit_n_rows = 0
        return render_template(
            "select.html", patients=[],
            cdr1_configured=bool(build_client().base_url),
            attest_required=True,
        )
    if is_admin:
        g._audit_event_type = "admin_override"
        g._audit_admin_justification = reason

    client = build_client()
    patients = client.list_org_patients(orgs, is_admin=is_admin, reason=reason)
    patients.sort(key=lambda p: (p.get("name") or "￿", p.get("guid") or ""))

    # No CDR1 configured (local dev / not yet wired) — flag it so the page
    # doesn't look falsely empty.
    cdr1_configured = bool(client.base_url)

    g._audit_n_rows = len(patients)
    return render_template(
        "select.html",
        patients=patients,
        cdr1_configured=cdr1_configured,
    )
