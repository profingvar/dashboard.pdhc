""""Bjud in patient" (#1050): a patient joins the patient app ONLY by an
invitation from their care unit, made here.

  GET  /bjud-in-patient            the form
  POST /bjud-in-patient/bekrafta   step 1 of 2: show what will be done
  POST /bjud-in-patient/skicka     step 2 of 2: eps, then sso; the link ONCE

Which unit. The choice is the caller's affiliations
(`affiliations[].care_unit_guid` in the SSO blob, CLAUDE.md s11): exactly one
-> preselected; several -> a REQUIRED choice with no default (the browser
form has no preselected option and the server refuses a POST without one);
none -> refused with a clear message, unless SU admin. An SU may also name a
care-unit GUID. eps and sso each check the unit again with the same bearer;
this page decides nothing on their behalf.

The two calls (`services/invite_client.py`), both with the professional's
own bearer:
  1. eps `register-at-unit`: find or create the patient and ensure an ACTIVE
     assignment at the unit;
  2. sso `patient-links/invites`: the one-time link, returned ONCE.
Nothing is half-done without being said: if sso fails after eps answered,
the page says the patient IS registered at the unit and the invitation can
be made again (step 1 is idempotent: eps finds the same patient).

The personnummer is never written to the audit row, the log or the session.
It travels in the confirmation form's hidden field (the professional's own
page) and in the eps request body. The invite link is shown once, with
`Cache-Control: no-store`, and is not stored by the dashboard.

Gate: `app.auth._is_invite_path` admits an SU admin or ANY professional, so a
professional with no care unit reaches this page and is told why they cannot
invite, instead of a bare 403. CSRF on both POSTs (`services/csrf.py`).
"""
from __future__ import annotations

import re
import uuid

from flask import Blueprint, g, make_response, render_template, request

from app.services import invite_client as ic
from app.services.audit import audit_read
from app.services.csrf import csrf_token, csrf_valid

bp = Blueprint("invite", __name__, url_prefix="/bjud-in-patient")

NAME_MAX = 100

#: Named refusals -> what the professional reads. Keys are (step, code).
MESSAGES = {
    ("eps", "invalid_personnummer"): "Personnumret är inte ett giltigt personnummer (10 eller 12 siffror med ett riktigt datum).",
    ("eps", "real_personnummer"): "Piloten tar bara emot syntetiska testpersonnummer. Det här numret har en giltig kontrollsiffra och kan tillhöra en verklig person.",
    ("eps", "not_affiliated"): "Du är inte affilierad till den valda vårdenheten.",
    ("eps", "not_professional"): "Bara vårdpersonal kan bjuda in patienter.",
    ("eps", "unknown_unit"): "Vårdenheten finns inte som aktiv enhet i patientregistret (eps).",
    ("eps", "name_mismatch"): "Det finns redan en patient med det personnumret, under ett annat namn. Kontrollera namn och personnummer. Ingenting har registrerats.",
    ("eps", "ambiguous"): "Flera patienter i patientregistret har det personnumret. Kontakta en administratör. Ingenting har registrerats.",
    ("eps", "archived"): "Patienten är arkiverad i patientregistret. Kontakta en administratör. Ingenting har registrerats.",
    ("eps", "validation"): "Någon uppgift saknas eller är för lång.",
    ("sso", "patient_already_linked"): "Patienten har redan ett konto i patientappen. En ny inbjudan behövs inte: patienten loggar in med sitt konto.",
    ("sso", "not_affiliated"): "Inloggningstjänsten (sso) anser inte att du är affilierad till den valda vårdenheten.",
    ("sso", "patient_not_at_unit"): "Inloggningstjänsten kunde inte se patienten på vårdenheten.",
    ("sso", "patient_not_found"): "Inloggningstjänsten hittade inte patienten i patientregistret.",
    ("sso", "not_found"): "Vårdenheten finns inte i inloggningstjänsten (sso).",
    ("sso", "forbidden"): "Bara vårdpersonal kan bjuda in patienter.",
    ("sso", "must_change_password"): "Byt ditt lösenord i inloggningstjänsten innan du bjuder in patienter.",
}
UNAVAILABLE = {
    "eps": "Patientregistret (eps) svarade inte. Ingenting har registrerats. Försök igen om en stund.",
    "sso": "Inloggningstjänsten (sso) svarade inte.",
}


# --------------------------------------------------------------------------
# who may invite for which unit (display; eps and sso check again)
# --------------------------------------------------------------------------

def _blob() -> dict:
    b = getattr(g, "access_blob", None)
    return b if isinstance(b, dict) else {}


def _is_su() -> bool:
    return bool(_blob().get("is_su_admin"))


def unit_choices() -> list[dict]:
    """[{guid, name}] from the blob's active affiliations, de-duplicated,
    in the blob's order. Never `organization_ids` (memberships)."""
    out, seen = [], set()
    for a in _blob().get("affiliations") or []:
        if not isinstance(a, dict):
            continue
        guid = str(a.get("care_unit_guid") or "").strip().lower()
        if not guid or guid in seen:
            continue
        seen.add(guid)
        out.append({"guid": guid, "name": a.get("care_unit_name") or guid})
    return out


def _unit_from_form(units: list[dict]) -> tuple[str | None, str | None]:
    """(unit guid, error). Several units and no choice is an error: there is
    no default. An SU may name any GUID."""
    chosen = (request.form.get("care_unit_guid") or "").strip().lower()
    if _is_su() and (request.form.get("care_unit_guid_su") or "").strip():
        chosen = request.form["care_unit_guid_su"].strip().lower()
    if not chosen and len(units) == 1:
        chosen = units[0]["guid"]
    if not chosen:
        return None, "Välj vårdenhet."
    if chosen in {u["guid"] for u in units}:
        return chosen, None
    if _is_su():
        try:
            return str(uuid.UUID(chosen)), None
        except ValueError:
            return None, "Vårdenhetens GUID är inte ett GUID."
    return None, "Du är inte affilierad till den valda vårdenheten."


def _unit_name(guid: str, units: list[dict]) -> str:
    return next((u["name"] for u in units if u["guid"] == guid), guid)


# --------------------------------------------------------------------------
# the form
# --------------------------------------------------------------------------

def normalise_mobile(value: str | None) -> str | None:
    """'' -> None; a Swedish mobile -> '+467XXXXXXXX'; else ValueError.
    Per-repo copy of sso's rule (patient_enrolment.normalise_mobile): there
    is deliberately no shared library across the PDHC services."""
    v = re.sub(r"[\s\-().]", "", value or "")
    if not v:
        return None
    if v.startswith("+46"):
        rest = v[3:]
    elif v.startswith("0046"):
        rest = v[4:]
    elif v.startswith("46") and len(v) == 11:
        rest = v[2:]
    elif v.startswith("0"):
        rest = v[1:]
    else:
        raise ValueError("not a Swedish mobile number")
    if not re.fullmatch(r"7\d{8}", rest):
        raise ValueError("not a Swedish mobile number")
    return "+46" + rest


def _read_form(units: list[dict]) -> tuple[dict, list[str]]:
    f = request.form
    form = {k: (f.get(k) or "").strip() for k in
            ("given_name", "family_name", "personnummer", "mobile")}
    errors = []
    if not form["given_name"]:
        errors.append("Ange förnamn.")
    if not form["family_name"]:
        errors.append("Ange efternamn.")
    if len(form["given_name"]) > NAME_MAX or len(form["family_name"]) > NAME_MAX:
        errors.append(f"Namnet får vara högst {NAME_MAX} tecken.")
    if not form["personnummer"]:
        errors.append("Ange personnummer.")
    elif not re.fullmatch(r"\d{6}(\d{2})?[-+]?\d{4}", form["personnummer"]):
        errors.append("Personnumret ska ha 10 eller 12 siffror (ÅÅÅÅMMDD-XXXX).")
    try:
        form["mobile_normalised"] = normalise_mobile(form["mobile"])
    except ValueError:
        errors.append("Ange ett svenskt mobilnummer (07…), eller lämna fältet tomt.")
        form["mobile_normalised"] = None
    unit, err = _unit_from_form(units)
    if err:
        errors.append(err)
    form["care_unit_guid"] = unit or ""
    form["care_unit_name"] = _unit_name(unit, units) if unit else ""
    return form, errors


def _audit(event: str, patient_guid: str | None = None, **payload) -> None:
    """The DashboardAudit row: identifiers and outcomes, never the number,
    the names, the mobile or the link."""
    g._audit_event_type = event
    if patient_guid:
        try:
            g._audit_patient_guid = str(uuid.UUID(str(patient_guid)))
        except ValueError:
            pass
    g._audit_payload_snapshot = {k: v for k, v in payload.items() if v is not None}


def _page(*, status: int = 200, form: dict | None = None, errors=None,
          refused: str | None = None):
    units = unit_choices()
    return render_template(
        "invite.html", units=units, is_su=_is_su(), form=form or {},
        errors=errors or [], refused=refused, csrf_token=csrf_token()), status


def _no_unit_refusal():
    """None, or the page for a professional who may invite for no unit."""
    if unit_choices() or _is_su():
        return None
    return _page(status=403, refused=(
        "Du har ingen affiliering till någon vårdenhet och kan därför inte bjuda "
        "in patienter. Be en administratör om en affiliering till din vårdenhet."))


def _csrf_refused(event: str):
    _audit(event, outcome="csrf_refused")
    return render_template("invite_message.html", title="Begäran avvisades", text=(
        "Säkerhetskontrollen (CSRF) saknades eller stämde inte, så ingenting har "
        "gjorts. Öppna sidan igen och försök på nytt.")), 403


@bp.get("")
@audit_read
def form():
    _audit("patient_invite_form")
    refusal = _no_unit_refusal()
    if refusal:
        _audit("patient_invite_form", outcome="no_unit")
        return refusal
    return _page()


@bp.post("/bekrafta")
@audit_read
def confirm():
    """Step 1 of 2. Calls nothing."""
    if not csrf_valid():
        return _csrf_refused("patient_invite_confirm")
    refusal = _no_unit_refusal()
    if refusal:
        return refusal
    form, errors = _read_form(unit_choices())
    if errors:
        _audit("patient_invite_confirm", outcome="form_invalid")
        return _page(status=400, form=form, errors=errors)
    _audit("patient_invite_confirm", outcome="shown",
           care_unit_guid=form["care_unit_guid"])
    return render_template("invite_confirm.html", form=form, csrf_token=csrf_token())


def _message(exc: ic.InviteError) -> str:
    if (exc.step, exc.code) in MESSAGES:
        return MESSAGES[(exc.step, exc.code)]
    if exc.code == "not_configured":
        return ("Patientregistret (eps) är inte konfigurerat i dashboard."
                if exc.step == "eps" else
                "Inloggningstjänsten (sso) är inte konfigurerad i dashboard.")
    if exc.code in ("no_bearer", "unauthenticated"):
        return "Din inloggning gick inte att använda. Logga ut och in igen."
    if exc.code in ("eps_unavailable", "eps_not_configured") and exc.step == "sso":
        return "Inloggningstjänsten kunde inte fråga patientregistret."
    return UNAVAILABLE[exc.step]


@bp.post("/skicka")
@audit_read
def send():
    """Step 2 of 2: eps, then sso. The link is shown ONCE."""
    if not csrf_valid():
        return _csrf_refused("patient_invite_send")
    if request.form.get("confirmed") != "ja":
        _audit("patient_invite_send", outcome="unconfirmed")
        return render_template("invite_message.html", title="Inte bekräftat", text=(
            "Inbjudan skickas först när den har bekräftats. Ingenting har gjorts.")), 400
    refusal = _no_unit_refusal()
    if refusal:
        return refusal
    form, errors = _read_form(unit_choices())
    if errors:
        _audit("patient_invite_send", outcome="form_invalid")
        return _page(status=400, form=form, errors=errors)
    unit = form["care_unit_guid"]

    try:
        patient = ic.register_at_unit(
            given_name=form["given_name"], family_name=form["family_name"],
            personnummer=form["personnummer"], care_unit_guid=unit,
            mobile=form["mobile_normalised"])
    except ic.InviteError as exc:
        _audit("patient_invite_send", outcome=f"eps_{exc.code}",
               care_unit_guid=unit, upstream_status=exc.status)
        status = 503 if exc.code in ("unavailable", "not_configured") else \
            (exc.status if exc.status in (403, 409, 422) else 502)
        return _page(status=status, form=form, errors=[_message(exc)])

    try:
        invite = ic.create_invite(patient_guid=patient["patient_guid"],
                                  care_unit_guid=unit,
                                  given_name=form["given_name"],
                                  family_name=form["family_name"])
    except ic.InviteError as exc:
        _audit("patient_invite_send", patient_guid=patient["patient_guid"],
               outcome=f"sso_{exc.code}", care_unit_guid=unit,
               eps_created=patient["created"], upstream_status=exc.status)
        already = exc.code == "patient_already_linked"
        status = 409 if already else (exc.status if exc.status in (403, 404, 409) else 502)
        return render_template(
            "invite_half.html", form=form, patient=patient, already=already,
            text=_message(exc), csrf_token=csrf_token()), status

    _audit("patient_invite_send", patient_guid=patient["patient_guid"],
           outcome="invited", care_unit_guid=unit, eps_created=patient["created"],
           eps_assignment=patient["assignment"], invite_guid=invite.get("invite_guid"))
    resp = make_response(render_template(
        "invite_done.html", form=form, patient=patient,
        invite_url=invite["invite_url"], expires_at=invite.get("expires_at")))
    resp.headers["Cache-Control"] = "no-store"
    return resp
