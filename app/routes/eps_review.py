"""The queue "EPS att granska" (#1044, epic #1039).

Patients answer the EPS form in the patient app. Their answers are stored in
cdr1 as patient-generated ORIGINALS awaiting review. Here a professional sees
the originals for their care units and records one of three decisions per
answer: approve, amend and approve, or reject with a reason. cdr1 holds the
record and decides who may endorse; this module shows what cdr1 lists and
forwards the decision with the professional's own bearer.

MDR (the dashboard is IIa for its limit indication): this view DISPLAYS data
and RECORDS a human decision. It computes nothing from the answers, does not
order or highlight them by clinical meaning (items appear in the order cdr1
returned them, grouped by patient in order of first appearance), and never
proposes a decision: no default action, no pre-selected button.

  GET  /eps-granskning                       the queue
  GET  /eps-granskning/antal                 {"count": N} for the nav link
  POST /eps-granskning/<review_guid>/bekrafta   the confirmation step
  POST /eps-granskning/<review_guid>/beslut     send the decision to cdr1
  POST /eps-granskning/kort/<card_version>/bekrafta   (#1049) a card version
  POST /eps-granskning/kort/<card_version>/beslut     (#1049)

v2 (#1049, contract s7.4/s7.5): the patient sends a whole SECTION of the
patient card at a time, as a `card_version`. Those are listed first, per
patient and section, each version's proposed rows beside the currently
endorsed rows with added / removed / changed marked (`card_diff`, a display
diff). One decision covers the whole version: approve (optionally amending
single values) or reject with a reason. Rows without `card_version` are the v1
queue below it, unchanged.

The view argument is `review_guid`, NOT `guid`: `audit_read` reads a view
argument called `guid` as the PATIENT guid.

Gate: these are clinical paths (`app.auth._is_clinical_path`), so the request
loader applies the care-delivery gate before any of this runs. Both POSTs
require the session's CSRF token (`app/services/csrf.py`).

Refusal is never an empty queue: every `ReviewError` renders a named panel
with a non-200 status, and the empty-queue text is rendered only when cdr1
answered 200 with an empty `items` list.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from flask import (
    Blueprint, abort, flash, g, jsonify, redirect, render_template, request,
    session, url_for,
)

from app.auth import org_guids_for
from app.services import card_diff
from app.services import cdr1_client
from app.services import cdr_review_client as rc
from app.services.audit import audit_read
from app.services.csrf import csrf_token, csrf_valid

bp = Blueprint("eps_review", __name__, url_prefix="/eps-granskning")

#: Contract s3.2: linkIds are prefixed by section. Display names only.
SECTION_LABELS = {
    "allergy": "Allergier",
    "medication": "Läkemedel",
    "history": "Tidigare sjukdomar",
    "vaccination": "Vaccinationer",
    "contact": "Kontaktpersoner",
}

ACTIONS = ("approve", "amend", "reject")
ACTION_LABELS = {
    "approve": "Godkänn",
    "amend": "Ändra och godkänn",
    "reject": "Avvisa",
}
_MAX_TEXT = 2000


# --------------------------------------------------------------------------
# display helpers (presentation only)
# --------------------------------------------------------------------------

def _when(raw) -> str:
    """An ISO timestamp as `YYYY-MM-DD HH:MM` Swedish time; the raw string if
    it cannot be parsed, so nothing is shown as a time it is not."""
    if not raw:
        return "—"
    s = str(raw)
    try:
        dt = datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)
    except ValueError:
        return s
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("Europe/Stockholm")).strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001 — no tzdata in the image: say UTC
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M") + " UTC"


def _kind(value) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if value is None or isinstance(value, str):
        return "text"
    return "structured"


def _display(value) -> str:
    """The answer as entered. No interpretation."""
    if value is None:
        return "(inget svar)"
    if isinstance(value, bool):
        return "Ja" if value else "Nej"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _view(item: dict) -> dict:
    value = item.get("value")
    kind = _kind(value)
    section = item.get("section") or ""
    return {
        "guid": str(item.get("guid") or ""),
        "patient_guid": str(item.get("patient_guid") or ""),
        "section": SECTION_LABELS.get(section, section or "Övrigt"),
        "label": item.get("label") or item.get("link_id") or "",
        "value": value,
        "value_text": _display(value),
        "unit": item.get("unit") or "",
        "kind": kind,
        "amendable": kind != "structured",
        # cdr1 says per item whether THIS caller may decide (an admin may
        # list what they may not decide). Absent = not stated: the actions
        # are offered and cdr1 answers the attempt.
        "can_decide": item.get("can_decide") is not False,
        "submitted_at": _when(item.get("submitted_at")),
        "recorded_at": _when(item.get("recorded_at")),
    }


def _patient_names() -> dict:
    """guid -> name for the caller's care units, best effort, so a group can
    be headed by a name instead of a GUID. Any failure leaves GUIDs. Not done
    for an admin: an admin's patient listing is a break-glass read (as in
    the picker), and this view must not make one implicitly."""
    try:
        user = g.current_user
        if getattr(user, "is_admin", False):
            return {}
        orgs = org_guids_for(user)
        if not orgs:
            return {}
        rows = cdr1_client.build_client().list_org_patients(orgs)
        return {p["guid"]: p.get("name") for p in rows if p.get("name")}
    except Exception:  # noqa: BLE001
        return {}


def _groups(items: list[dict]) -> list[dict]:
    """Per patient, in the order cdr1 returned the items. No sorting."""
    names = _patient_names() if items else {}
    out: dict[str, dict] = {}
    for raw in items:
        v = _view(raw)
        grp = out.get(v["patient_guid"])
        if grp is None:
            grp = out[v["patient_guid"]] = {
                "patient_guid": v["patient_guid"],
                "name": names.get(v["patient_guid"]) or "",
                "items": [],
            }
        grp["items"].append(v)
    return list(out.values())


#: The marks of the display diff (`card_diff`), in Swedish. Text, not colour,
#: carries the mark; the CSS border only repeats it.
MARK_LABELS = {
    card_diff.SAME: "Oförändrad",
    card_diff.CHANGED: "Ändrad",
    card_diff.ADDED: "Ny",
    card_diff.REMOVED: "Borttagen",
}


def _cell(row: dict) -> dict:
    """One card row as shown. `display` is what the patient saw when it is
    given (a coded answer: the value is its code); else the value as entered.

    A CODED row is not amendable here: its value is a code from the form's
    answer list, which the dashboard does not hold, and a free-text
    replacement would no longer be one of those codes."""
    value = row.get("value")
    display = row.get("display")
    coded = isinstance(display, str) and display.strip() != "" and display != _display(value)
    kind = _kind(value)
    return {
        "guid": str(row.get("guid") or ""),
        "link_id": str(row.get("link_id") or ""),
        "label": row.get("label") or row.get("link_id") or "",
        "text": display if coded else _display(value),
        "value": value,
        "kind": kind,
        "coded": coded,
        "amendable": bool(row.get("guid")) and not coded and kind != "structured",
    }


def _card_view(item: dict) -> dict:
    section = item.get("section") or ""
    endorsed = item.get("endorsed")
    blocks = card_diff.diff(item.get("rows") or [],
                            (endorsed or {}).get("rows"), _cell)
    proposed = [_cell(r) for r in item.get("rows") or []]
    return {
        "card_version": str(item.get("card_version") or ""),
        "patient_guid": str(item.get("patient_guid") or ""),
        "section_code": section,
        "section": SECTION_LABELS.get(section, section or "Övrigt"),
        "submitted_at": _when(item.get("submitted_at")),
        "has_endorsed": endorsed is not None,
        "endorsed_at": _when((endorsed or {}).get("reviewed_at")),
        "blocks": blocks,
        "counts": card_diff.counts(blocks),
        "proposed": proposed,
        "amendable": [c for c in proposed if c["amendable"]],
        "can_decide": item.get("can_decide") is not False,
    }


def _card_groups(items: list[dict]) -> list[dict]:
    """Per patient, then per section, in the order cdr1 returned the versions."""
    names = _patient_names() if items else {}
    out: dict[str, dict] = {}
    for raw in items:
        v = _card_view(raw)
        grp = out.get(v["patient_guid"])
        if grp is None:
            grp = out[v["patient_guid"]] = {
                "patient_guid": v["patient_guid"],
                "name": names.get(v["patient_guid"]) or "",
                "sections": {},
            }
        grp["sections"].setdefault(v["section_code"], {
            "section": v["section"], "cards": []})["cards"].append(v)
    return [dict(g_, sections=list(g_["sections"].values())) for g_ in out.values()]


# --------------------------------------------------------------------------
# problems, in plain Swedish
# --------------------------------------------------------------------------

def _list_problem(exc: rc.ReviewError) -> tuple[int, dict]:
    """(HTTP status, panel) for a failed read of the queue."""
    tail = " Kön kunde inte läsas. Det betyder inte att den är tom."
    if isinstance(exc, rc.ReviewForbidden):
        return 403, {"code": "forbidden", "title": "Du har inte behörighet till kön", "text": (
            "CDR avvisade begäran (403). För att granska EPS-svar behöver du "
            "vara affilierad till en vårdenhet där patienten är listad, som "
            "läkare eller sjuksköterska." + tail)}
    if isinstance(exc, rc.ReviewNotConfigured):
        return 503, {"code": "not_configured", "title": "CDR är inte konfigurerat", "text": (
            "CDR1_BASE_URL är inte satt i den här miljön." + tail)}
    if isinstance(exc, rc.ReviewNoBearer):
        return 503, {"code": "no_bearer", "title": "Ingen SSO-inloggning", "text": (
            "Granskning görs i ditt eget namn och kräver en SSO-inloggning. "
            "Den här sessionen har ingen (AUTH_MODE=off)." + tail)}
    if isinstance(exc, rc.ReviewApiMissing):
        return 502, {"code": "api_missing", "title": "CDR saknar granskningsfunktionen", "text": (
            "Den CDR som dashboard är kopplad till har ännu inte funktionen "
            "för EPS-granskning." + tail)}
    if getattr(exc, "status_code", None) == 401:
        return 502, {"code": "cdr_unauthenticated", "title": "CDR godtog inte inloggningen", "text": (
            "CDR svarade 401 trots att du är inloggad här. Logga ut och in "
            "igen; kvarstår felet, kontakta förvaltningen." + tail)}
    return 502, {"code": "unreachable", "title": "CDR svarar inte", "text": (
        "CDR kunde inte nås eller gav ett oväntat svar. Försök igen om en "
        "stund." + tail)}


#: cdr1's refusal codes for the content of a decision, in Swedish.
_INVALID_TEXT = {
    "wrong_type": "Det ändrade värdet har fel typ, till exempel text där ett "
                  "tal eller ett datum väntas.",
    "out_of_range": "Texten eller värdet är för långt eller för stort.",
    "required": "Ett värde krävs.",
    "no_value": "Patientens svar har inget värde som kan ändras. Godkänn "
                "eller avvisa det.",
    "reason_required": "Ett skäl krävs för att avvisa.",
}


def _admin_attest_needed() -> bool:
    """An SU admin lists EVERY unit's originals. Elsewhere in the dashboard
    that is a break-glass read that needs the session's attestation reason
    (the picker, the charts); the same holds here. Sets the audit fields."""
    if not getattr(g.current_user, "is_admin", False):
        return False
    reason = (session.get("admin_read_reason") or "").strip()
    if not reason:
        return True
    g._audit_admin_justification = reason
    return False


def _audit(event: str, *, n_rows: int = 0, patient_guid: str | None = None,
           **payload) -> None:
    """Shape the DashboardAudit row `audit_read` writes. Identifiers and
    outcomes only: never the answer, the amended value, the note or the
    reason. Those live in cdr1, which is their record."""
    g._audit_event_type = event
    g._audit_n_rows = n_rows
    if patient_guid:
        try:  # the column is a UUID; a non-UUID would lose the whole row
            g._audit_patient_guid = str(uuid.UUID(str(patient_guid)))
        except ValueError:
            pass
    g._audit_payload_snapshot = {k: v for k, v in payload.items() if v is not None}


def _card_problem(exc: rc.ReviewError) -> tuple[int, dict]:
    """(HTTP status, panel) for a failed read of the CARD list. A cdr1 without
    the s7.4 routes is said by name: the per-row queue may still be there."""
    if isinstance(exc, rc.ReviewApiMissing):
        return 502, {"code": "card_api_missing",
                     "title": "CDR saknar granskning av patientkort", "text": (
                         "Den CDR som dashboard är kopplad till har ännu inte "
                         "funktionen för patientkort (version 2). Kortversioner "
                         "kunde därför inte läsas. Det betyder inte att det "
                         "inte finns några.")}
    st, problem = _list_problem(exc)
    return st, dict(problem, text=problem["text"].replace(
        "Kön kunde inte läsas.", "Patientkorten kunde inte läsas."))


def _load_queue() -> dict:
    """Both lists from cdr1, each with its own outcome. Never raises."""
    client = rc.build_review_client()
    out: dict = {"cards": None, "card_exc": None, "v1": None, "v1_exc": None}
    try:
        out["cards"] = client.list_cards()
    except rc.ReviewError as exc:
        out["card_exc"] = exc
    try:
        out["v1"] = client.list_pending()
    except rc.ReviewError as exc:
        out["v1_exc"] = exc
    if out["v1"] is not None:
        # A row that belongs to a card version is decided with its version,
        # never alone. cdr1 may list it on the per-row route too (s7.4 does
        # not say); it is shown once, in the card.
        in_cards = {str(r.get("guid")) for c in (out["cards"] or {}).get("items", [])
                    for r in c.get("rows", [])}
        items = out["v1"]["items"]
        kept = [i for i in items
                if not i.get("card_version") and str(i.get("guid")) not in in_cards]
        dropped = len(items) - len(kept)
        out["v1"] = dict(out["v1"], items=kept,
                         total=max(out["v1"]["total"] - dropped, len(kept)))
    return out


def _render_queue(*, status: int = 200, form_error: str | None = None,
                  audit_event: str = "eps_review_queue", **audit_payload):
    """The queue page. Used for the plain GET and to re-show the queue with a
    form error. A failed read renders a problem panel INSTEAD of that list;
    the other list is still shown, and the page answers the failure's status."""
    if _admin_attest_needed():
        _audit(audit_event, outcome="admin_attest_required", **audit_payload)
        return render_template("eps_review.html", attest_required=True,
                               problem=None, groups=[], total=None,
                               card_problem=None, card_groups=[],
                               form_error=None, eps_nav_count=""), 428
    q = _load_queue()
    problem = card_problem = None
    statuses = []
    if q["v1_exc"] is not None:
        st, problem = _list_problem(q["v1_exc"])
        statuses.append(st)
    if q["card_exc"] is not None:
        st, card_problem = _card_problem(q["card_exc"])
        statuses.append(st)
    groups = _groups(q["v1"]["items"]) if q["v1"] else []
    cards = q["cards"]["items"] if q["cards"] else []
    card_groups = _card_groups(cards)
    n_v1 = len(q["v1"]["items"]) if q["v1"] else 0

    if statuses:
        nav = "" if 403 in statuses else "?"
        outcome = ",".join(p["code"] for p in (problem, card_problem) if p)
        _audit(audit_event, n_rows=n_v1 + len(cards), outcome=outcome,
               cdr_status=(q["v1_exc"] or q["card_exc"]).status_code,
               n_cards=len(cards) if q["cards"] else None,
               **{k: v for k, v in audit_payload.items() if k != "outcome"})
        page_status = status if status != 200 else statuses[0]
    else:
        nav = q["v1"]["total"] + len(cards)
        _audit(audit_event, n_rows=n_v1 + len(cards), n_cards=len(cards),
               outcome=audit_payload.pop("outcome", "listed"), **audit_payload)
        page_status = status
    return render_template(
        "eps_review.html", problem=problem, groups=groups,
        total=q["v1"]["total"] if q["v1"] else None,
        shown=n_v1, truncated=bool(q["v1"] and q["v1"]["truncated"]),
        card_problem=card_problem, card_groups=card_groups,
        n_cards=len(cards),
        cards_truncated=bool(q["cards"] and q["cards"]["truncated"]),
        form_error=form_error, eps_nav_count=nav,
        csrf_token=csrf_token(), action_labels=ACTION_LABELS,
        mark_labels=MARK_LABELS), page_status


def _csrf_refused(event: str, **ids):
    _audit(event, outcome="csrf_refused", **ids)
    return render_template("eps_review_message.html", title="Begäran avvisades", text=(
        "Säkerhetskontrollen (CSRF) saknades eller stämde inte, så inget "
        "beslut har registrerats. Öppna kön igen och försök på nytt.")), 403


# --------------------------------------------------------------------------
# the decision form
# --------------------------------------------------------------------------

class _FormError(Exception):
    pass


def _coerce_amended(raw: str, original):
    """The amended value in the ORIGINAL's JSON type. Raises _FormError."""
    raw = (raw or "").strip()
    kind = _kind(original)
    if kind == "structured":
        raise _FormError("Det här svaret är sammansatt och kan inte ändras "
                         "här. Godkänn det som det är eller avvisa det.")
    if not raw:
        raise _FormError("Ange det ändrade värdet.")
    if len(raw) > _MAX_TEXT:
        raise _FormError(f"Värdet får vara högst {_MAX_TEXT} tecken.")
    if kind == "bool":
        if raw.lower() not in ("ja", "nej"):
            raise _FormError("Välj Ja eller Nej.")
        new = raw.lower() == "ja"
    elif kind == "number":
        txt = raw.replace(",", ".").replace(" ", "")
        try:
            new = float(txt) if "." in txt or "e" in txt.lower() else int(txt)
        except ValueError:
            raise _FormError("Värdet måste vara ett tal.") from None
    else:
        new = raw
    if new == original:
        raise _FormError("Värdet är oförändrat. Välj Godkänn om svaret ska "
                         "godkännas som det är.")
    return new


def _read_form(original_lookup) -> dict:
    """Validate the posted decision. `original_lookup()` returns the item's
    original value and is only called for an amendment. Raises _FormError."""
    action = (request.form.get("action") or "").strip()
    if action not in ACTIONS:
        raise _FormError("Välj Godkänn, Ändra och godkänn eller Avvisa.")
    note = (request.form.get("note") or "").strip()
    reason = (request.form.get("reason") or "").strip()
    if len(note) > _MAX_TEXT or len(reason) > _MAX_TEXT:
        raise _FormError(f"Texten får vara högst {_MAX_TEXT} tecken.")
    out = {"action": action, "note": "", "reason": "", "raw_value": ""}
    if action == "reject":
        if not reason:
            raise _FormError("Ett skäl krävs för att avvisa. Skälet visas "
                             "för patienten.")
        out["reason"] = reason
        return out
    out["note"] = note
    if action == "amend":
        out["raw_value"] = (request.form.get("value") or "").strip()
        out["value"] = _coerce_amended(out["raw_value"], original_lookup())
    return out


def _find_pending(review_guid: str) -> dict | None:
    """The item as cdr1 lists it NOW, or None if it is not pending in the
    caller's scope. Raises ReviewError."""
    for it in rc.build_review_client().list_pending()["items"]:
        if str(it.get("guid")) == review_guid:
            # #1049: a row of a card version is decided with its version.
            return None if it.get("card_version") else it
    return None


def _gone():
    flash("Posten finns inte längre i din kö. Någon kan redan ha beslutat "
          "om den.", "warning")
    return redirect(url_for("eps_review.queue"), code=303)


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

@bp.get("")
@audit_read
def queue():
    return _render_queue()


@bp.get("/antal")
def count():
    """The number for the navigation link. Never 0 unless cdr1 said 0: a
    failed read answers with its own status and `count: null`."""
    q = _load_queue()
    exc = q["v1_exc"] or q["card_exc"]
    if exc is not None:
        # Either list unknown -> the number is unknown. A count that left
        # out the card versions would be a smaller queue than the real one.
        st, problem = (_list_problem(exc) if q["v1_exc"] is not None
                       else _card_problem(exc))
        resp = jsonify(count=None, error=problem["code"])
        resp.headers["Cache-Control"] = "no-store"
        return resp, st
    resp = jsonify(count=q["v1"]["total"] + len(q["cards"]["items"]))
    resp.headers["Cache-Control"] = "no-store"
    return resp, 200


@bp.post("/<review_guid>/bekrafta")
@audit_read
def confirm(review_guid):
    """Step 1 of 2: show exactly what will be sent. Sends nothing to cdr1."""
    if not csrf_valid():
        return _csrf_refused("eps_review_confirm", review_guid=review_guid)
    if _admin_attest_needed():
        return _render_queue(audit_event="eps_review_confirm", review_guid=review_guid)
    try:
        item = _find_pending(review_guid)
    except rc.ReviewError:
        return _render_queue(audit_event="eps_review_confirm",
                             review_guid=review_guid)
    if item is None:
        _audit("eps_review_confirm", review_guid=review_guid, outcome="not_pending")
        return _gone()
    try:
        form = _read_form(lambda: item.get("value"))
    except _FormError as exc:
        return _render_queue(status=400, form_error=str(exc),
                             audit_event="eps_review_confirm",
                             review_guid=review_guid, outcome="form_invalid")
    _audit("eps_review_confirm", n_rows=1, patient_guid=item.get("patient_guid"),
           review_guid=review_guid, action=form["action"], outcome="shown")
    amended_text = _display(form["value"]) if form["action"] == "amend" else None
    return render_template(
        "eps_review_confirm.html", item=_view(item), form=form,
        amended_text=amended_text, action_label=ACTION_LABELS[form["action"]],
        csrf_token=csrf_token())


@bp.post("/<review_guid>/beslut")
@audit_read
def decide(review_guid):
    """Step 2 of 2: send the confirmed decision to cdr1 in the operator's name."""
    if not csrf_valid():
        return _csrf_refused("eps_review_decision", review_guid=review_guid)
    if request.form.get("confirmed") != "ja":
        _audit("eps_review_decision", review_guid=review_guid, outcome="unconfirmed")
        return render_template("eps_review_message.html", title="Beslutet är inte bekräftat", text=(
            "Ett beslut skickas först när det har bekräftats. Inget har "
            "registrerats. Öppna kön och välj åtgärd igen.")), 400
    if _admin_attest_needed():
        return _render_queue(audit_event="eps_review_decision", review_guid=review_guid)
    try:
        item = _find_pending(review_guid)
    except rc.ReviewError:
        return _render_queue(audit_event="eps_review_decision",
                             review_guid=review_guid)
    if item is None:
        _audit("eps_review_decision", review_guid=review_guid, outcome="not_pending")
        return _gone()
    try:
        form = _read_form(lambda: item.get("value"))
    except _FormError as exc:
        return _render_queue(status=400, form_error=str(exc),
                             audit_event="eps_review_decision",
                             review_guid=review_guid, outcome="form_invalid")

    action = form["action"]
    event = f"eps_review_{action}"          # eps_review_approve|amend|reject
    pg = item.get("patient_guid")
    client = rc.build_review_client()
    try:
        if action == "reject":
            body = client.reject(review_guid, form["reason"])
        elif action == "amend":
            body = client.approve(review_guid, value=form["value"],
                                  note=form["note"] or None)
        else:
            body = client.approve(review_guid, note=form["note"] or None)
    except rc.ReviewAlreadyDecided as exc:
        _audit(event, patient_guid=pg, review_guid=review_guid,
               outcome="already_decided", cdr_status=exc.status_code)
        flash("Någon annan har redan beslutat om den här posten. Ditt beslut "
              "registrerades inte, och posten har lämnat kön.", "warning")
        return redirect(url_for("eps_review.queue"), code=303)
    except rc.ReviewNotFound as exc:
        _audit(event, patient_guid=pg, review_guid=review_guid,
               outcome="not_found", cdr_status=exc.status_code)
        flash("Posten finns inte inom dina vårdenheter. Inget beslut "
              "registrerades.", "warning")
        return redirect(url_for("eps_review.queue"), code=303)
    except rc.ReviewForbidden as exc:
        _audit(event, patient_guid=pg, review_guid=review_guid,
               outcome="forbidden", cdr_status=exc.status_code)
        return render_template("eps_review_message.html", title="Du får inte besluta om den här posten", text=(
            "CDR avvisade beslutet (403), så inget har registrerats. Beslut "
            "kräver att du är läkare eller sjuksköterska med affiliering "
            "till den vårdenhet där patienten är listad. Att vara "
            "administratör räcker inte. CDR svarar på samma sätt om posten "
            "inte finns.")), 403
    except rc.ReviewInvalid as exc:
        _audit(event, patient_guid=pg, review_guid=review_guid,
               outcome="invalid", cdr_status=exc.status_code,
               cdr_error=exc.cdr_code or exc.cdr_error)
        code = exc.cdr_code or exc.cdr_error
        why = _INVALID_TEXT.get(code) or (
            "Kontrollera värdet eller skälet" + (f" (CDR: {code})" if code else "") + ".")
        return render_template("eps_review_message.html", title="CDR godtog inte beslutet", text=(
            "CDR avvisade innehållet i beslutet, så inget har registrerats. "
            + why)), 422
    except rc.ReviewError as exc:
        _audit(event, patient_guid=pg, review_guid=review_guid,
               outcome="unknown", cdr_status=exc.status_code)
        return render_template("eps_review_message.html", title="Beslutet kunde inte bekräftas", text=(
            "CDR svarade inte som väntat. Det är därför OKÄNT om beslutet "
            "registrerades. Öppna kön igen: finns posten kvar har inget "
            "registrerats, och du kan försöka på nytt.")), 502

    _audit(event, n_rows=1, patient_guid=pg, review_guid=review_guid,
           outcome="sent",
           endorsement_guid=body.get("endorsement_guid"),
           amended=(action == "amend") if action != "reject" else None,
           note_given=bool(form["note"]) if action != "reject" else None)
    flash({"approve": "Svaret är godkänt. En ny post i vårdens namn har skapats.",
           "amend": "Svaret är ändrat och godkänt. En ny post i vårdens namn "
                    "har skapats; patientens original är oförändrat.",
           "reject": "Svaret är avvisat. Patienten ser skälet i appen."}[action],
          "success")
    return redirect(url_for("eps_review.queue"), code=303)


# --------------------------------------------------------------------------
# card versions (#1049, contract s7.4/s7.5)
# --------------------------------------------------------------------------

CARD_ACTIONS = ("approve", "reject")
CARD_ACTION_LABELS = {"approve": "Godkänn versionen", "reject": "Avvisa versionen"}


def _card_version_arg(card_version: str) -> str:
    """The path segment as a canonical UUID (s7.3 mints a uuid), or 404.
    Nothing that is not a uuid is put into a cdr1 URL."""
    try:
        return str(uuid.UUID(card_version))
    except ValueError:
        abort(404)


def _find_card(card_version: str) -> dict | None:
    """The version as cdr1 lists it NOW among the pending ones, or None.
    Raises ReviewError."""
    for it in rc.build_review_client().list_cards()["items"]:
        if str(it.get("card_version")).lower() == card_version:
            return it
    return None


def _card_gone():
    flash("Den versionen av patientkortet finns inte längre i din kö. Någon "
          "kan redan ha beslutat om den, eller så har patienten skickat en "
          "nyare version som ersatt den. Inget beslut registrerades.", "warning")
    return redirect(url_for("eps_review.queue"), code=303)


def _read_card_form(view: dict) -> dict:
    """Validate a posted card decision against the version as cdr1 lists it.
    Amendments are read ONLY for the version's own amendable rows, from the
    fields `amend-<row guid>`; an empty field means "not amended". Raises
    _FormError."""
    action = (request.form.get("action") or "").strip()
    if action not in CARD_ACTIONS:
        raise _FormError("Välj Godkänn versionen eller Avvisa versionen.")
    note = (request.form.get("note") or "").strip()
    reason = (request.form.get("reason") or "").strip()
    if len(note) > _MAX_TEXT or len(reason) > _MAX_TEXT:
        raise _FormError(f"Texten får vara högst {_MAX_TEXT} tecken.")
    out = {"action": action, "note": "", "reason": "", "amend": {}, "raw": {}}
    if action == "reject":
        if not reason:
            raise _FormError("Ett skäl krävs för att avvisa. Skälet visas "
                             "för patienten.")
        out["reason"] = reason
        return out
    out["note"] = note
    for c in view["proposed"]:
        raw = (request.form.get(f"amend-{c['guid']}") or "").strip()
        if not raw:
            continue
        if not c["amendable"]:
            raise _FormError(f"{c['label']}: svaret kan inte ändras här. "
                             "Godkänn versionen som den är eller avvisa den.")
        try:
            out["amend"][c["guid"]] = _coerce_amended(raw, c["value"])
        except _FormError as exc:
            raise _FormError(f"{c['label']}: {exc}") from None
        out["raw"][c["guid"]] = raw
    return out


def _card_audit(event: str, card_version: str, view: dict | None = None, **kw):
    """Identifiers, counts and the outcome only: never a value, the note or
    the reason (cdr1 is their record)."""
    _audit(event, n_rows=len(view["proposed"]) if view else 0,
           patient_guid=view["patient_guid"] if view else None,
           card_version=card_version,
           section=view["section_code"] if view else None, **kw)


def _card_lookup(event: str, card_version: str):
    """(view, None) for a pending, decidable version; (None, response) else."""
    if _admin_attest_needed():
        return None, _render_queue(audit_event=event, card_version=card_version)
    try:
        item = _find_card(card_version)
    except rc.ReviewError:
        return None, _render_queue(audit_event=event, card_version=card_version)
    if item is None:
        _card_audit(event, card_version, outcome="not_pending")
        return None, _card_gone()
    view = _card_view(item)
    if not view["can_decide"]:
        _card_audit(event, card_version, view, outcome="cannot_decide")
        return None, (render_template("eps_review_message.html", title="Du får inte besluta om den här versionen", text=(
            "CDR anger att du kan se versionen men inte besluta om den. Beslut "
            "kräver att du är läkare eller sjuksköterska med affiliering till "
            "patientens vårdenhet. Inget har registrerats.")), 403)
    return view, None


@bp.post("/kort/<card_version>/bekrafta")
@audit_read
def card_confirm(card_version):
    """Step 1 of 2 for a card version. Sends nothing to cdr1."""
    card_version = _card_version_arg(card_version)
    if not csrf_valid():
        return _csrf_refused("eps_card_confirm", card_version=card_version)
    view, resp = _card_lookup("eps_card_confirm", card_version)
    if resp is not None:
        return resp
    try:
        form = _read_card_form(view)
    except _FormError as exc:
        return _render_queue(status=400, form_error=str(exc),
                             audit_event="eps_card_confirm",
                             card_version=card_version, outcome="form_invalid")
    _card_audit("eps_card_confirm", card_version, view, action=form["action"],
                n_amended=len(form["amend"]), outcome="shown")
    amended = {g_: _display(v) for g_, v in form["amend"].items()}
    return render_template(
        "eps_review_card_confirm.html", card=view, form=form, amended=amended,
        action_label=CARD_ACTION_LABELS[form["action"]],
        mark_labels=MARK_LABELS, csrf_token=csrf_token())


@bp.post("/kort/<card_version>/beslut")
@audit_read
def card_decide(card_version):
    """Step 2 of 2: send the confirmed decision on a card version to cdr1."""
    card_version = _card_version_arg(card_version)
    if not csrf_valid():
        return _csrf_refused("eps_card_decision", card_version=card_version)
    if request.form.get("confirmed") != "ja":
        _audit("eps_card_decision", card_version=card_version, outcome="unconfirmed")
        return render_template("eps_review_message.html", title="Beslutet är inte bekräftat", text=(
            "Ett beslut skickas först när det har bekräftats. Inget har "
            "registrerats. Öppna kön och välj åtgärd igen.")), 400
    view, resp = _card_lookup("eps_card_decision", card_version)
    if resp is not None:
        return resp
    try:
        form = _read_card_form(view)
    except _FormError as exc:
        return _render_queue(status=400, form_error=str(exc),
                             audit_event="eps_card_decision",
                             card_version=card_version, outcome="form_invalid")

    action = form["action"]
    event = f"eps_card_{action}"            # eps_card_approve|reject
    client = rc.build_review_client()
    try:
        if action == "reject":
            body = client.reject_card(card_version, form["reason"])
        else:
            body = client.approve_card(card_version, amend=form["amend"] or None,
                                       note=form["note"] or None)
    except rc.ReviewSuperseded as exc:      # before its base class
        _card_audit(event, card_version, view, outcome="superseded",
                    cdr_status=exc.status_code)
        flash("Patienten har skickat en nyare version av avsnittet "
              f"{view['section']}. Den version du granskade är ersatt och kan "
              "inte längre beslutas. Ditt beslut registrerades inte. Granska "
              "den nya versionen i kön.", "warning")
        return redirect(url_for("eps_review.queue"), code=303)
    except rc.ReviewAlreadyDecided as exc:
        _card_audit(event, card_version, view, outcome="already_decided",
                    cdr_status=exc.status_code)
        flash("Någon annan har redan beslutat om den här versionen av "
              "patientkortet. Ditt beslut registrerades inte, och versionen "
              "har lämnat kön.", "warning")
        return redirect(url_for("eps_review.queue"), code=303)
    except rc.ReviewNotFound as exc:
        _card_audit(event, card_version, view, outcome="not_found",
                    cdr_status=exc.status_code)
        flash("Versionen finns inte inom dina vårdenheter. Inget beslut "
              "registrerades.", "warning")
        return redirect(url_for("eps_review.queue"), code=303)
    except rc.ReviewForbidden as exc:
        _card_audit(event, card_version, view, outcome="forbidden",
                    cdr_status=exc.status_code)
        return render_template("eps_review_message.html", title="Du får inte besluta om den här versionen", text=(
            "CDR avvisade beslutet (403), så inget har registrerats. Beslut "
            "kräver att du är läkare eller sjuksköterska med affiliering "
            "till den vårdenhet där patienten är listad. Att vara "
            "administratör räcker inte.")), 403
    except rc.ReviewInvalid as exc:
        code = exc.cdr_code or exc.cdr_error
        _card_audit(event, card_version, view, outcome="invalid",
                    cdr_status=exc.status_code, cdr_error=code)
        why = _INVALID_TEXT.get(code) or (
            "Kontrollera värdena eller skälet" + (f" (CDR: {code})" if code else "") + ".")
        return render_template("eps_review_message.html", title="CDR godtog inte beslutet", text=(
            "CDR avvisade innehållet i beslutet, så inget har registrerats. "
            + why)), 422
    except rc.ReviewError as exc:
        _card_audit(event, card_version, view, outcome="unknown",
                    cdr_status=exc.status_code)
        return render_template("eps_review_message.html", title="Beslutet kunde inte bekräftas", text=(
            "CDR svarade inte som väntat. Det är därför OKÄNT om beslutet "
            "registrerades. Öppna kön igen: finns versionen kvar har inget "
            "registrerats, och du kan försöka på nytt.")), 502

    _card_audit(event, card_version, view, outcome="sent",
                n_amended=len(form["amend"]) if action == "approve" else None,
                endorsed=body.get("endorsed") if action == "approve" else None,
                note_given=bool(form["note"]) if action == "approve" else None)
    if action == "approve":
        msg = (f"Versionen av {view['section']} är godkänd. Nya poster i vårdens "
               "namn har skapats; patientens original är oförändrade.")
        if form["amend"]:
            msg += f" {len(form['amend'])} värde(n) ändrades."
    else:
        msg = (f"Versionen av {view['section']} är avvisad. Patienten ser "
               "skälet i appen.")
    flash(msg, "success")
    return redirect(url_for("eps_review.queue"), code=303)
