""""Bjud in till patientappen" (#1050, v2 after operator feedback 2026-11-22).

Operator: "the patient is chosen as I enter into the dashboard - then the
invite - No need to identify the patient - just confirm the contact way and
allow an iMessage to be formed that sends the invite".

  GET  /bjud-in-patient                    -> the patient picker (no patient)
  GET  /bjud-in-patient/<guid>             the page for the OPEN patient
  POST /bjud-in-patient/<guid>/bekrafta    step 1 of 2: confirm unit + mobile
  POST /bjud-in-patient/<guid>/skicka      step 2 of 2: sso invite; the sms:
                                           link and the invite link, ONCE

WHICH PATIENT. The dashboard holds no "current patient" of its own: the
patient is the guid in the URL of the view opened from the picker
(`/patient/<guid>/charts`, `/dashboard/view/<guid>`). Those views call
`remember_opened_patient`, and these routes refuse a guid that was not opened
in this session. A patient guid in a POSTed form is never used: one that
differs from the path is refused (no IDOR via a hidden field). The real
authority is downstream, with the professional's own bearer: eps scopes the
read of the patient to the reader (404 outside it), and sso refuses an
invite unless the inviter is affiliated to the unit AND eps lists the
patient there.

WHICH UNIT. Non-SU: the caller's affiliations (`affiliations[]
.care_unit_guid`, CLAUDE.md s11), narrowed to the units eps lists the
patient at. One such unit -> preselected; several -> a REQUIRED choice with
no default; none -> refused by name ("not listed at your unit"). No
affiliation at all and not SU -> refused before eps is asked. v1 attached a
patient to the unit with eps `register-at-unit`; that needs a typed
personnummer, so v2 does not, and a patient not listed at the unit is
refused rather than attached. An SU chooses among the units eps lists.

THE NAME comes from eps on every step, never from the form. THE MOBILE is
eps's (prefilled, editable) or typed when eps has none; required; Swedish
mobile normalised to +467XXXXXXXX. It is used for this message only: eps's
only telecom write is the generic, unscoped FHIR `PUT /fhir/Patient/<id>`
(a whole-resource replace), so no "save the number" is offered.

THE MESSAGE. The done page carries a button whose href is
`sms:<+46...>&body=<percent-encoded text>` (see `sms_href`), opening
Messages with recipient and text filled in. The page cannot know whether the
message was sent and does not say so.

Audit: `patient_invite_form` / `_confirm` / `_send` with `outcome` and the
patient guid; never the name, number or link. CSRF on both POSTs;
`confirmed=ja` on the second.
"""
from __future__ import annotations

import re
import uuid
from urllib.parse import quote

from flask import (Blueprint, g, make_response, redirect, render_template,
                   request, session, url_for)

from app.services import invite_client as ic
from app.services.audit import audit_read
from app.services.csrf import csrf_token, csrf_valid

bp = Blueprint("invite", __name__, url_prefix="/bjud-in-patient")

#: How many opened patients the session remembers (several tabs are normal).
OPENED_MAX = 20
_OPENED_KEY = "_opened_patients"

#: Named refusals -> what the professional reads. Keys are (step, code).
MESSAGES = {
    ("eps", "patient_not_found"): "Patienten finns inte i patientregistret (eps), eller ligger utanför de vårdenheter du har behörighet till där.",
    ("eps", "not_platform_guid"): "Patientregistret känner patienten under ett annat id. Öppna patienten från patientlistan och försök igen.",
    ("sso", "patient_already_linked"): "Patienten har redan ett konto i patientappen. En ny inbjudan behövs inte: patienten loggar in med sitt konto.",
    ("sso", "not_affiliated"): "Inloggningstjänsten (sso) anser inte att du är affilierad till den valda vårdenheten.",
    ("sso", "patient_not_at_unit"): "Inloggningstjänsten kunde inte se patienten på vårdenheten.",
    ("sso", "patient_not_found"): "Inloggningstjänsten hittade inte patienten i patientregistret.",
    ("sso", "not_found"): "Vårdenheten finns inte i inloggningstjänsten (sso).",
    ("sso", "forbidden"): "Bara vårdpersonal kan bjuda in patienter.",
    ("sso", "must_change_password"): "Byt ditt lösenord i inloggningstjänsten innan du bjuder in patienter.",
    ("sso", "invalid_request"): "Inloggningstjänsten tog inte emot uppgifterna (namnet saknas eller är för långt i patientregistret).",
}
UNAVAILABLE = {
    "eps": "Patientregistret (eps) svarade inte. Ingenting har gjorts. Försök igen om en stund.",
    "sso": "Inloggningstjänsten (sso) svarade inte. Ingen inbjudan har skapats. Försök igen om en stund.",
}


# --------------------------------------------------------------------------
# the patient in context
# --------------------------------------------------------------------------

def _canonical(guid) -> str | None:
    try:
        return str(uuid.UUID(str(guid)))
    except (ValueError, TypeError):
        return None


def remember_opened_patient(guid) -> None:
    """Called by the patient views: this patient is open in this session."""
    c = _canonical(guid)
    if not c:
        return
    try:
        opened = [x for x in (session.get(_OPENED_KEY) or []) if x != c]
        session[_OPENED_KEY] = ([c] + opened)[:OPENED_MAX]
    except RuntimeError:  # no request context
        pass


def _was_opened(guid: str) -> bool:
    return guid in (session.get(_OPENED_KEY) or [])


# --------------------------------------------------------------------------
# who may invite for which unit (display; sso checks again)
# --------------------------------------------------------------------------

def _blob() -> dict:
    b = getattr(g, "access_blob", None)
    return b if isinstance(b, dict) else {}


def _is_su() -> bool:
    return bool(_blob().get("is_su_admin"))


def affiliated_units() -> list[dict]:
    """[{guid, name}] from the blob's affiliations, de-duplicated, in the
    blob's order. Never `organization_ids` (memberships)."""
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


def unit_choices(listed: list[dict]) -> tuple[list[dict], list[dict]]:
    """(eligible, not_listed). Eligible = the units the caller may invite
    from AND eps lists the patient at. An SU: every listed unit."""
    if _is_su():
        return list(listed), []
    listed_guids = {u["guid"] for u in listed}
    mine = affiliated_units()
    return ([u for u in mine if u["guid"] in listed_guids],
            [u for u in mine if u["guid"] not in listed_guids])


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


def eps_mobile(phones: list[str]) -> tuple[str, str | None]:
    """(prefill, normalised) from eps's numbers: the first that normalises
    to a Swedish mobile; else the first raw one (to be corrected); else ''."""
    for p in phones:
        try:
            n = normalise_mobile(p)
        except ValueError:
            continue
        if n:
            return n, n
    return (phones[0] if phones else ""), None


# --------------------------------------------------------------------------
# the message
# --------------------------------------------------------------------------

def message_text(given_name: str, unit_name: str, invite_url: str) -> str:
    return (f"Hej {given_name}! {unit_name} bjuder in dig till Patientappen. "
            f"Öppna länken för att skapa ditt konto: {invite_url} "
            "Länken gäller en gång.")


def sms_href(mobile: str, text: str) -> str:
    """`sms:<+46...>&body=<text>`, the body percent-encoded in full.

    THE FORM. RFC 5724 writes the body as `?body=`; Apple's Messages on iOS
    (since iOS 8) reads `sms:<number>&body=<text>`, and the same form is what
    current guides use for macOS Messages. The operator asked for the iOS form,
    so this is it. `quote(..., safe="")` encodes everything that is not
    unreserved: a space is `%20` (never `+`, which Messages would show), and
    the `&`, `?`, `=`, `/`, `#` inside the invite link are encoded so they
    cannot end the body or start a new parameter. Unverified on a real
    iPhone/iPad/Mac: see progress.md."""
    return f"sms:{mobile}&body={quote(text, safe='')}"


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------

def _audit(event: str, patient_guid: str | None = None, **payload) -> None:
    """The DashboardAudit row: identifiers and outcomes, never the names,
    the mobile or the link."""
    g._audit_event_type = event
    if patient_guid:
        g._audit_patient_guid = patient_guid
    g._audit_payload_snapshot = {k: v for k, v in payload.items() if v is not None}


def _message(title: str, text: str, status: int, guid: str | None = None):
    return render_template("invite_message.html", title=title, text=text,
                           patient_guid=guid), status


def _error_text(exc: ic.InviteError) -> str:
    if (exc.step, exc.code) in MESSAGES:
        return MESSAGES[(exc.step, exc.code)]
    if exc.code == "not_configured":
        return ("Patientregistret (eps) är inte konfigurerat i dashboard."
                if exc.step == "eps" else
                "Inloggningstjänsten (sso) är inte konfigurerad i dashboard.")
    if exc.code in ("no_bearer", "unauthenticated"):
        return "Din inloggning gick inte att använda. Logga ut och in igen."
    if exc.code in ("eps_unavailable", "eps_not_configured") and exc.step == "sso":
        return "Inloggningstjänsten kunde inte fråga patientregistret. Ingen inbjudan har skapats."
    return UNAVAILABLE[exc.step]


def _status_for(exc: ic.InviteError) -> int:
    if exc.code in ("unavailable", "not_configured"):
        return 503
    if exc.code == "patient_not_found" and exc.step == "eps":
        return 404
    if exc.code == "patient_already_linked":
        return 409
    return exc.status if exc.status in (400, 403, 404, 409) else 502


class _Refusal(Exception):
    def __init__(self, response, outcome: str):
        self.response, self.outcome = response, outcome


def _context(guid: str, event: str) -> dict:
    """Everything every step needs, re-established on every step: the
    patient (from eps), the eligible units. Raises _Refusal with the page."""
    canon = _canonical(guid)
    if canon is None or canon != guid.lower():
        raise _Refusal(_message("Okänd patient", "Patientens id är inte giltigt.", 404),
                       "bad_guid")
    guid = canon
    if not affiliated_units() and not _is_su():
        raise _Refusal(_message("Du kan inte bjuda in patienter", (
            "Du har ingen affiliering till någon vårdenhet och kan därför inte bjuda "
            "in patienter. Be en administratör om en affiliering till din vårdenhet."),
            403), "no_unit")
    # A guid in the FORM never chooses the patient; one that disagrees with
    # the path is refused, not ignored, so the attempt is visible.
    for key in ("patient_guid", "ips_patient_guid"):
        sent = request.form.get(key) if request.method == "POST" else None
        if sent is not None and _canonical(sent) != guid:
            raise _Refusal(_message("Fel patient", (
                "Formuläret gällde en annan patient än den som är öppen. "
                "Ingenting har gjorts."), 400, guid), "patient_mismatch")
    if not _was_opened(guid):
        raise _Refusal(_message("Öppna patienten först", (
            "Inbjudan görs för den patient du har öppen i dashboard. Välj "
            "patienten i patientlistan och välj sedan Bjud in till patientappen."),
            403), "not_in_context")
    try:
        person = ic.patient_details(guid)
        listed = ic.patient_units(guid)
    except ic.InviteError as exc:
        raise _Refusal(_message("Det gick inte att bjuda in", _error_text(exc),
                                _status_for(exc), guid), f"eps_{exc.code}")
    if not person["given_name"] or not person["family_name"]:
        raise _Refusal(_message("Det gick inte att bjuda in", (
            "Patientregistret (eps) har inget fullständigt namn för patienten. "
            "Inbjudan visar patientens namn, så namnet behöver finnas där först."),
            409, guid), "eps_no_name")
    eligible, not_listed = unit_choices(listed)
    if not eligible:
        names = ", ".join(u["name"] for u in not_listed) or "någon vårdenhet"
        raise _Refusal(_message("Patienten är inte listad vid din vårdenhet", (
            f"Patienten är inte listad vid {names} i patientregistret (eps). "
            "En inbjudan görs från en vårdenhet där patienten är listad. "
            "Ingenting har gjorts."), 409, guid), "not_listed")
    prefill, eps_norm = eps_mobile(person["phones"])
    return {"patient_guid": guid, "given_name": person["given_name"],
            "family_name": person["family_name"], "eligible": eligible,
            "not_listed": not_listed, "eps_mobile": eps_norm,
            "mobile_prefill": prefill, "has_eps_mobile": bool(person["phones"])}


def _read_choices(ctx: dict) -> tuple[dict, list[str]]:
    """The two things the professional confirms: unit and mobile."""
    errors: list[str] = []
    chosen = (request.form.get("care_unit_guid") or "").strip().lower()
    eligible = {u["guid"]: u for u in ctx["eligible"]}
    if not chosen and len(eligible) == 1:
        chosen = next(iter(eligible))
    unit = eligible.get(chosen)
    if not chosen:
        errors.append("Välj vårdenhet.")
    elif unit is None:
        if chosen in {u["guid"] for u in ctx["not_listed"]}:
            errors.append("Patienten är inte listad vid den valda vårdenheten i "
                          "patientregistret. Välj en vårdenhet där patienten är listad.")
        else:
            errors.append("Du är inte affilierad till den valda vårdenheten.")
    raw = (request.form.get("mobile") or "").strip()
    mobile = None
    try:
        mobile = normalise_mobile(raw)
        if mobile is None:
            errors.append("Ange patientens mobilnummer (07…).")
    except ValueError:
        errors.append("Ange ett svenskt mobilnummer (07… eller +467…).")
    return {"care_unit_guid": unit["guid"] if unit else chosen,
            "care_unit_name": unit["name"] if unit else "",
            "mobile_raw": raw, "mobile": mobile,
            "mobile_differs": bool(mobile) and mobile != ctx["eps_mobile"]}, errors


def _form_page(ctx: dict, *, choice: dict | None = None, errors=None, status=200):
    return render_template(
        "invite.html", ctx=ctx, choice=choice or {}, errors=errors or [],
        csrf_token=csrf_token()), status


def _csrf_refused(event: str, guid: str | None):
    _audit(event, _canonical(guid), outcome="csrf_refused")
    return _message("Begäran avvisades", (
        "Säkerhetskontrollen (CSRF) saknades eller stämde inte, så ingenting har "
        "gjorts. Öppna sidan igen och försök på nytt."), 403)


@bp.get("")
def no_patient():
    """v1's standalone form is gone: a patient is always chosen first."""
    return redirect(url_for("picker.select"))


@bp.get("/<guid>")
@audit_read
def form(guid: str):
    try:
        ctx = _context(guid, "patient_invite_form")
    except _Refusal as r:
        _audit("patient_invite_form", _canonical(guid), outcome=r.outcome)
        return r.response
    _audit("patient_invite_form", ctx["patient_guid"], outcome="shown",
           eps_mobile=bool(ctx["has_eps_mobile"]))
    return _form_page(ctx)


@bp.post("/<guid>/bekrafta")
@audit_read
def confirm(guid: str):
    """Step 1 of 2. Reads eps; writes nothing."""
    if not csrf_valid():
        return _csrf_refused("patient_invite_confirm", guid)
    try:
        ctx = _context(guid, "patient_invite_confirm")
    except _Refusal as r:
        _audit("patient_invite_confirm", _canonical(guid), outcome=r.outcome)
        return r.response
    choice, errors = _read_choices(ctx)
    if errors:
        _audit("patient_invite_confirm", ctx["patient_guid"], outcome="form_invalid")
        return _form_page(ctx, choice=choice, errors=errors, status=400)
    _audit("patient_invite_confirm", ctx["patient_guid"], outcome="shown",
           care_unit_guid=choice["care_unit_guid"],
           mobile_differs=choice["mobile_differs"])
    return render_template("invite_confirm.html", ctx=ctx, choice=choice,
                           csrf_token=csrf_token())


@bp.post("/<guid>/skicka")
@audit_read
def send(guid: str):
    """Step 2 of 2: the sso invite, with eps's name. Shown ONCE."""
    if not csrf_valid():
        return _csrf_refused("patient_invite_send", guid)
    if request.form.get("confirmed") != "ja":
        _audit("patient_invite_send", _canonical(guid), outcome="unconfirmed")
        return _message("Inte bekräftat", (
            "Inbjudan skapas först när den har bekräftats. Ingenting har gjorts."), 400)
    try:
        ctx = _context(guid, "patient_invite_send")
    except _Refusal as r:
        _audit("patient_invite_send", _canonical(guid), outcome=r.outcome)
        return r.response
    choice, errors = _read_choices(ctx)
    if errors:
        _audit("patient_invite_send", ctx["patient_guid"], outcome="form_invalid")
        return _form_page(ctx, choice=choice, errors=errors, status=400)
    unit = choice["care_unit_guid"]
    try:
        invite = ic.create_invite(patient_guid=ctx["patient_guid"], care_unit_guid=unit,
                                  given_name=ctx["given_name"],
                                  family_name=ctx["family_name"])
    except ic.InviteError as exc:
        _audit("patient_invite_send", ctx["patient_guid"], outcome=f"sso_{exc.code}",
               care_unit_guid=unit, upstream_status=exc.status)
        return _form_page(ctx, choice=choice, errors=[_error_text(exc)],
                          status=_status_for(exc))

    text = message_text(ctx["given_name"], choice["care_unit_name"], invite["invite_url"])
    _audit("patient_invite_send", ctx["patient_guid"], outcome="invited",
           care_unit_guid=unit, invite_guid=invite.get("invite_guid"),
           mobile_differs=choice["mobile_differs"])
    resp = make_response(render_template(
        "invite_done.html", ctx=ctx, choice=choice, invite_url=invite["invite_url"],
        text=text, sms=sms_href(choice["mobile"], text),
        expires_at=invite.get("expires_at")))
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Pragma"] = "no-cache"
    return resp
