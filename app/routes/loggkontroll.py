"""Loggkontroll (#1045): who has read or changed the OPEN patient's data.

Patientdatalagen 4 kap. 3 §: "systematisk och återkommande kontroll" of who
has accessed a patient's records.

  GET /loggkontroll                         -> the patient picker
  GET /loggkontroll/<guid>                  the merged log of the OPEN patient
        ?fran=YYYY-MM-DD&till=YYYY-MM-DD    Swedish calendar days, inclusive
        &visa=alla|avvikande|stickprov
        &antal=<n>&urval=<seed>             the random sample
  GET /loggkontroll/<guid>/csv              the same rows as CSV (`visa` applies)

WHICH PATIENT: the invite's rule (routes/invite.py). The patient is the one
opened in this session (`remember_opened_patient`); a guid that was not opened
is refused. The services asked enforce their own gate with the person's own
bearer as well.

WHO MAY REVIEW — AN OPERATOR DECISION, the safest default implemented:

  * an SU admin, always;
  * a professional ONLY when (a) the operator has named the roles that review
    logs in LOGGKONTROLL_ROLES (comma-separated affiliation roles, e.g.
    "loggranskare" or "doctor"), AND (b) the person has an affiliation with
    such a role at a care unit where eps lists the patient.

  LOGGKONTROLL_ROLES unset = SU only. A log of who looked at a patient is
  also a log of what colleagues did; under PDL the care provider decides who
  carries out the check, so the default is not "every professional at the
  unit". The services behind it (cdr1, eps, request) admit SU or a
  professional affiliated to the patient's unit, so this page is the
  narrower door.

THE PAGE NEVER SHOWS A SILENT GAP. A service that could not be asked is
listed by name ("Kunde inte hämtas från ...") above the table, and in the CSV
as a row of its own.

Audit: one DashboardAudit row per view (`loggkontroll`) and per export
(`loggkontroll_export`), with the period, the view, the sample seed, the
row count and which services failed; refusals with their outcome. Never a
name.
"""
from __future__ import annotations

import csv
import io
import secrets
from datetime import date, datetime, timedelta

from flask import (Blueprint, current_app, g, make_response, redirect,
                   render_template, request, url_for)

from app.routes.invite import _canonical, _was_opened
from app.services import access_log as al
from app.services import invite_client as ic
from app.services.audit import audit_read

bp = Blueprint("loggkontroll", __name__, url_prefix="/loggkontroll")

DEFAULT_DAYS = 30
MAX_DAYS = 366
DEFAULT_SAMPLE = 20
VIEWS = ("alla", "avvikande", "stickprov")

#: CSV cells that a spreadsheet would read as a formula.
_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def _blob() -> dict:
    b = getattr(g, "access_blob", None)
    return b if isinstance(b, dict) else {}


def review_roles() -> set[str]:
    raw = str(current_app.config.get("LOGGKONTROLL_ROLES") or "")
    return {r.strip().lower() for r in raw.split(",") if r.strip()}


def may_review(blob: dict, patient_units: set[str] | None) -> tuple[bool, str]:
    """(allowed, outcome). See the module docstring."""
    if blob.get("is_su_admin"):
        return True, "su_admin"
    if blob.get("user_type") != "professional":
        return False, "not_professional"
    roles = review_roles()
    if not roles:
        return False, "su_only"
    if patient_units is None:
        return False, "eps_unavailable"
    for a in blob.get("affiliations") or []:
        if not isinstance(a, dict):
            continue
        unit = str(a.get("care_unit_guid") or "").strip().lower()
        if unit in patient_units and str(a.get("role") or "").lower() in roles:
            return True, "unit_role"
    return False, "not_at_unit"


def shows_link(blob: dict | None) -> bool:
    """Whether the patient views offer "Loggkontroll": an SU, or a
    professional holding a review role somewhere. The page still checks the
    patient's unit (may_review)."""
    blob = blob if isinstance(blob, dict) else {}
    if blob.get("is_su_admin"):
        return True
    roles = review_roles()
    return bool(roles) and blob.get("user_type") == "professional" and any(
        isinstance(a, dict) and str(a.get("role") or "").lower() in roles
        for a in blob.get("affiliations") or [])


REFUSALS = {
    "not_professional": "Loggkontroll görs av vårdpersonal med uppdrag att granska loggar.",
    "su_only": ("Loggkontroll görs i dag av systemadministratör (SU). Verksamheten har "
                "ännu inte bestämt vilka roller vid vårdenheten som får granska loggar."),
    "eps_unavailable": ("Patientregistret (eps) svarade inte, så det gick inte att se om "
                        "patienten hör till din vårdenhet. Försök igen om en stund."),
    "not_at_unit": ("Du kan granska loggen för patienter vid den vårdenhet där du har "
                    "uppdraget att granska loggar. Patienten är inte listad där."),
    "not_in_context": ("Loggkontrollen görs för den patient du har öppen. Välj patienten "
                       "i patientlistan och välj sedan Loggkontroll."),
    "bad_guid": "Patientens id är inte giltigt.",
}


def _audit(event: str, guid: str | None, **payload) -> None:
    g._audit_event_type = event
    if guid:
        g._audit_patient_guid = guid
    g._audit_payload_snapshot = {k: v for k, v in payload.items() if v is not None}


def _refuse(event: str, guid: str | None, outcome: str, status: int):
    _audit(event, guid, outcome=outcome)
    return render_template("loggkontroll_message.html", title="Loggkontroll",
                           text=REFUSALS[outcome], patient_guid=guid
                           if outcome not in ("bad_guid", "not_in_context") else None), status


def _day(name: str, default: date) -> tuple[date | None, str | None]:
    raw = (request.args.get(name) or "").strip()
    if not raw:
        return default, None
    try:
        return date.fromisoformat(raw), None
    except ValueError:
        return None, f"Datumet {raw!r} är inte på formen ÅÅÅÅ-MM-DD."


def _params() -> tuple[dict, list[str]]:
    today = datetime.now(al.STOCKHOLM).date()
    errors = []
    d_from, e1 = _day("fran", today - timedelta(days=DEFAULT_DAYS - 1))
    d_to, e2 = _day("till", today)
    errors += [e for e in (e1, e2) if e]
    if d_from and d_to:
        if d_from > d_to:
            errors.append("Från-datumet ligger efter till-datumet.")
        elif (d_to - d_from).days + 1 > MAX_DAYS:
            errors.append(f"Välj en period på högst {MAX_DAYS} dagar.")
    view = request.args.get("visa") or "alla"
    if view not in VIEWS:
        view = "alla"
    try:
        n = max(1, min(500, int(request.args.get("antal") or DEFAULT_SAMPLE)))
    except ValueError:
        n = DEFAULT_SAMPLE
    try:
        seed = int(request.args.get("urval") or 0)
    except ValueError:
        seed = 0
    if view == "stickprov" and seed <= 0:
        seed = secrets.randbelow(900_000) + 100_000
    return {"fran": d_from, "till": d_to, "visa": view, "antal": n, "urval": seed}, errors


def _patient_units(guid: str) -> tuple[set[str] | None, list[dict]]:
    try:
        units = ic.patient_units(guid)
    except ic.InviteError as exc:
        if exc.code == "patient_not_found":
            # eps answers the same 404 for "no such patient" and "outside
            # your units": either way, listed at none of the reader's units.
            return set(), []
        return None, []
    return {u["guid"] for u in units}, units


def _prepare(guid: str, event: str):
    """(context, None) or (None, refusal response)."""
    canon = _canonical(guid)
    if canon is None or canon != guid.lower():
        return None, _refuse(event, None, "bad_guid", 404)
    if not _was_opened(canon):
        return None, _refuse(event, canon, "not_in_context", 403)
    units, unit_list = _patient_units(canon)
    ok, outcome = may_review(_blob(), units)
    if not ok:
        return None, _refuse(event, canon, outcome, 403)
    p, errors = _params()
    ctx = {"patient_guid": canon, "units": unit_list, "units_known": units is not None,
           "basis": outcome, "p": p, "errors": errors}
    if errors:
        return ctx, None
    start, end = al.period_utc(p["fran"], p["till"])
    items, results = al.collect(canon, start, end, units)
    unusual = [i for i in items if i["unusual"]]
    if p["visa"] == "avvikande":
        shown = unusual
    elif p["visa"] == "stickprov":
        shown = al.sample(items, p["antal"], p["urval"])
    else:
        shown = items
    ctx.update(entries=items, shown=shown, results=results, n_unusual=len(unusual),
               failed=[r for r in results if not r.ok])
    return ctx, None


def _audit_ok(event: str, ctx: dict) -> None:
    p = ctx["p"]
    _audit(event, ctx["patient_guid"], outcome="shown" if not ctx["errors"] else "form_invalid",
           fran=p["fran"].isoformat() if p["fran"] else None,
           till=p["till"].isoformat() if p["till"] else None,
           visa=p["visa"], urval=p["urval"] if p["visa"] == "stickprov" else None,
           antal=p["antal"] if p["visa"] == "stickprov" else None,
           n_rows=len(ctx.get("shown") or []), basis=ctx["basis"],
           failed=[r.service for r in ctx.get("failed") or []] or None)
    if ctx.get("shown") is not None:
        g._audit_n_rows = len(ctx["shown"])


@bp.get("")
def no_patient():
    return redirect(url_for("picker.select"))


@bp.get("/<guid>")
@audit_read
def page(guid: str):
    ctx, refused = _prepare(guid, "loggkontroll")
    if refused:
        return refused
    _audit_ok("loggkontroll", ctx)
    resp = make_response(render_template("loggkontroll.html", ctx=ctx, flags=al.FLAGS,
                                          views=VIEWS), 400 if ctx["errors"] else 200)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _cell(value) -> str:
    s = "" if value is None else str(value)
    return "'" + s if s.startswith(_FORMULA) else s


@bp.get("/<guid>/csv")
@audit_read
def export_csv(guid: str):
    ctx, refused = _prepare(guid, "loggkontroll_export")
    if refused:
        return refused
    _audit_ok("loggkontroll_export", ctx)
    if ctx["errors"]:
        return render_template("loggkontroll_message.html", title="Loggkontroll",
                               text=" ".join(ctx["errors"]),
                               patient_guid=ctx["patient_guid"]), 400
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    flag_names = [k for k, _ in al.FLAGS]
    w.writerow(["tid (svensk tid)", "tjänst", "användare", "användar-id", "via tjänst",
                "åtgärd", "utfall", "grund", "anrop", "resurs", "session"] + flag_names)
    for r in ctx["failed"]:
        w.writerow([_cell(x) for x in ["", r.service, "", "", "", "KUNDE INTE HÄMTAS",
                                       r.error, "", "", "", ""]] + [""] * len(flag_names))
    for i in ctx["shown"]:
        f = i["flags"]
        w.writerow([_cell(x) for x in [
            i["at_local"], i["service"], i.get("user_label"), i.get("user_guid"),
            i.get("via_service"), i.get("action"), i.get("outcome"), i.get("basis"),
            i.get("route"), i.get("resource_type"), i.get("session_id")]]
            + ["ja" if f[k] is True else ("nej" if f[k] is False else "okänt")
               for k in flag_names])
    p = ctx["p"]
    resp = make_response("﻿" + buf.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="loggkontroll_{ctx["patient_guid"]}_{p["fran"]}_{p["till"]}'
        f'_{p["visa"]}.csv"')
    resp.headers["Cache-Control"] = "no-store"
    return resp
