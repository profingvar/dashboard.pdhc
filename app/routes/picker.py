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

from flask import Blueprint, g, render_template

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

    client = build_client()
    patients = client.list_org_patients(orgs, is_admin=is_admin)
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
