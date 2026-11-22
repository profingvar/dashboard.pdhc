"""Clinical patient picker (#465 / #462 D3; names and personnummer #1050).

The picker's patient set is the UNION of two reads:

1. eps (ips.pdhc), the patient register — the roster of each care unit the
   caller is affiliated to (`affiliations[].care_unit_guid`, Rule 24; never
   `organization_ids`), read with the professional's OWN SSO bearer
   (`services/eps_roster.py`). This gives the name and the personnummer, and
   it lists a patient who has no data yet — who must be openable, since the
   invite (#1050) starts from the open patient.
2. cdr1's `GET /api/v1/clinical/patients` (#468), unchanged: patients WITH
   observations in the caller's scope, for the data count and last observed.
   cdr1 holds no demographics by design, so its names are not used when eps
   has the patient.

A cdr1 row eps does not list for the caller's units keeps its guid and says
"ej i patientregistret" — but only when every roster was read; when eps did
not answer for every unit (or for an SU) it says "namn okänt" instead, since
absence from an unread roster proves nothing.

eps unreachable -> the cdr1 list is shown with a banner, never a silently
nameless or empty list.

SU (#575 break-glass, unchanged): the page asks for an attested reason
first, then lists cdr1's whole set (admin override) plus the eps rosters of
the SU's OWN affiliated units, if any. An SU's picker does NOT read every
eps clinic: that would be the whole register on one page, unbounded. A cdr1
row outside those rosters reads "namn okänt" with the guid. SU's names for a patient come from opening it.

Personnummer: shown in the page only (the caller is an authorised
professional or SU, and eps itself gates the roster). Never logged, never in
the audit row, never in a URL. The response is `no-store`.
"""
from __future__ import annotations

from flask import Blueprint, g, make_response, render_template, session

from app.auth import org_guids_for
from app.services import eps_roster
from app.services.audit import audit_read
from app.services.cdr1_client import build_client

bp = Blueprint("picker", __name__)


def _merge(cdr_rows: list[dict], roster: eps_roster.Roster) -> list[dict]:
    """One row per patient guid: eps identity + cdr1 data counts."""
    out: dict[str, dict] = {}
    for p in cdr_rows:
        guid = str(p.get("guid") or "").strip()
        if not guid:
            continue
        key = guid.lower()
        eps = roster.patients.get(key)
        out[key] = {
            "guid": guid,
            "in_eps": eps is not None,
            "name": eps["name"] if eps else "",
            "personnummer": eps["personnummer"] if eps else "",
            "units": eps["units"] if eps else [],
            "observation_count": p.get("observation_count"),
            "last_observed_at": p.get("last_observed_at"),
            "has_data": True,
        }
    for key, eps in roster.patients.items():
        if key in out:
            continue
        out[key] = {
            "guid": eps["guid"], "in_eps": True, "name": eps["name"],
            "personnummer": eps["personnummer"], "units": eps["units"],
            "observation_count": None, "last_observed_at": None,
            "has_data": False,
        }
    rows = list(out.values())
    # Most recent data first (#578); patients without data after, by name.
    rows.sort(key=lambda r: (r["name"].lower() or "￿", r["guid"]))
    rows.sort(key=lambda r: r["last_observed_at"] or "", reverse=True)
    return rows


def _render(**ctx):
    resp = make_response(render_template("select.html", **ctx))
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Pragma"] = "no-cache"
    return resp


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
        return _render(patients=[], cdr1_configured=bool(build_client().base_url),
                       attest_required=True)
    if is_admin:
        g._audit_event_type = "admin_override"
        g._audit_admin_justification = reason

    units = eps_roster.caller_units(getattr(g, "access_blob", None))
    roster = eps_roster.read_roster(units)

    client = build_client()
    cdr_rows = client.list_org_patients(orgs, is_admin=is_admin, reason=reason)
    patients = _merge(cdr_rows, roster)

    g._audit_n_rows = len(patients)
    # Counts and states only: never a name or a personnummer.
    g._audit_payload_snapshot = {
        "eps_roster": roster.status, "eps_units_asked": roster.units_asked,
        "eps_units_failed": roster.units_failed,
        "eps_units_refused": roster.units_refused,
        "eps_patients": len(roster.patients), "cdr1_patients": len(cdr_rows),
    }
    # "ej i patientregistret" is a CLAIM about eps. Make it only when every
    # unit's roster was read and the caller is not an SU (whose cdr1 list
    # spans units whose rosters are not read); otherwise the name is unknown.
    roster_complete = (not is_admin and roster.status == "ok"
                       and not (roster.units_failed or roster.units_refused
                                or roster.truncated))
    return _render(
        patients=patients,
        absent_label="ej i patientregistret" if roster_complete else "namn okänt",
        cdr1_configured=bool(client.base_url),
        roster=roster,
        n_units=len(units),
        is_admin=is_admin,
        max_units=eps_roster.MAX_UNITS,
    )
