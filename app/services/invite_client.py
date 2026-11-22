"""The calls behind "Bjud in till patientappen" (#1050, v2 2026-11-22).

The patient is the one the professional has OPEN in the dashboard; nothing
about them is typed. So eps is only READ, and sso is asked for the invite:

    1. eps.pdhc  GET  {IPS_BASE_URL}/api/v1/patients/<guid>/state
                 -> the name and phone numbers (scoped by eps)
    2. eps.pdhc  GET  {IPS_BASE_URL}/api/v1/patients/<guid>/clinics
                 -> the units the patient is actively listed at
    3. sso.pdhc  POST {SSO_BASE_URL}/api/patient-links/invites
                 {"ips_patient_guid", "care_unit_guid", "given_name",
                  "family_name"} -> 201 {"invite_url", "expires_at", ...}

AUTH IS THE PROFESSIONAL'S OWN SSO BEARER on both, and nothing else (the
cdr_review_client rule): eps scopes the reads to that person, sso requires
that person to be affiliated to the unit (and asks eps itself that the
patient is listed there), and sso records that person as the inviter. No
service key is sent.

v1 called eps `register-at-unit` first, with a typed name and personnummer.
v2 does not call it: there is no personnummer in this flow, and a patient
who is not listed at the unit is refused by name instead of being attached.

EVERY FAILURE IS A NAMED EXCEPTION carrying which step failed and the
neighbour's own `error` code, so the page can say exactly what happened —
in particular "eps could not say" (503) versus "outside your scope"
(404), which must never read as "nothing happened".
No personnummer passes through this module.
"""
from __future__ import annotations

import uuid

import requests
from flask import current_app, session

DEFAULT_TIMEOUT = 8.0


class InviteError(Exception):
    """`step` is "eps" or "sso"; `status` the HTTP status when there was one;
    `code` the neighbour's `error` code (or ours: not_configured,
    no_bearer, unavailable)."""

    def __init__(self, step: str, code: str, status: int | None = None):
        super().__init__(f"{step}: {code} ({status})")
        self.step, self.code, self.status = step, code, status


def _bearer() -> str | None:
    try:
        return session.get("sso_token")
    except RuntimeError:
        return None


def _headers(token: str) -> dict:
    h = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    try:
        from app.services.session_headers import outbound_session_headers
        h.update(outbound_session_headers())
    except Exception:  # noqa: BLE001 — best effort, as in every other client
        pass
    return h


def _post(step: str, url: str, body: dict, ok: tuple[int, ...]) -> tuple[int, dict]:
    token = _bearer()
    if not token:
        raise InviteError(step, "no_bearer")
    try:
        r = requests.post(url, headers=_headers(token), json=body,
                          timeout=DEFAULT_TIMEOUT, allow_redirects=False)
    except requests.RequestException:
        raise InviteError(step, "unavailable")
    try:
        data = r.json()
    except ValueError:
        data = None
    if r.status_code in ok and isinstance(data, dict):
        return r.status_code, data
    code = data.get("error") if isinstance(data, dict) else None
    if not isinstance(code, str) or not code:
        # 401, an HTML page from another vhost, an OperationOutcome: none of
        # these is the neighbour saying something about THIS patient.
        code = "unauthenticated" if r.status_code == 401 else "unavailable"
    raise InviteError(step, code, r.status_code)


def _get(step: str, url: str) -> tuple[int, dict | list, dict]:
    """GET with the professional's bearer. Returns (status, body, headers)
    on 200; a named InviteError otherwise (404 -> `patient_not_found`)."""
    token = _bearer()
    if not token:
        raise InviteError(step, "no_bearer")
    try:
        r = requests.get(url, headers=_headers(token), timeout=DEFAULT_TIMEOUT,
                         allow_redirects=False)
    except requests.RequestException:
        raise InviteError(step, "unavailable")
    try:
        data = r.json()
    except ValueError:
        data = None
    if r.status_code == 200 and isinstance(data, (dict, list)):
        return r.status_code, data, dict(getattr(r, "headers", None) or {})
    if r.status_code == 404:
        # eps answers the SAME 404 for "no such patient" and "outside your
        # scope" (patient_routes /state), deliberately. So do we.
        raise InviteError(step, "patient_not_found", 404)
    raise InviteError(step, "unauthenticated" if r.status_code == 401
                      else "unavailable", r.status_code)


def _eps_base() -> str:
    base = (current_app.config.get("IPS_BASE_URL") or "").strip().rstrip("/")
    if not base:
        raise InviteError("eps", "not_configured")
    return base


def patient_details(patient_guid: str) -> dict:
    """The patient's current name and phone numbers from eps.

    `GET /api/v1/patients/<guid>/state` (#901c): the patient's facts as at
    now, scoped through eps's `patient_export.exportable` -- the same "may
    this reader see this patient" the euips-summary on the patient card uses.
    Returns {"given_name", "family_name", "phones": [raw values, mobile
    first]}. Names are "" when eps holds none."""
    _, data, _ = _get("eps", f"{_eps_base()}/api/v1/patients/{patient_guid}/state")
    if not isinstance(data, dict) or not isinstance(data.get("facts"), list):
        raise InviteError("eps", "unavailable", 200)
    given = family = ""
    mobiles, others = [], []
    for f in data["facts"]:
        if not isinstance(f, dict):
            continue
        v = f.get("value") if isinstance(f.get("value"), dict) else {}
        kind = f.get("fact_type")
        if kind == "name.given" and not given:
            raw = v.get("value")
            given = " ".join(str(x) for x in raw) if isinstance(raw, list) else str(raw or "")
        elif kind == "name.family" and not family:
            family = str(v.get("value") or "")
        elif kind == "telecom" and v.get("system") in ("phone", "sms") and v.get("value"):
            (mobiles if v.get("use") == "mobile" else others).append(str(v["value"]))
    return {"given_name": given.strip(), "family_name": family.strip(),
            "phones": mobiles + others}


def patient_units(patient_guid: str) -> list[dict]:
    """The care units the patient is ACTIVELY listed at in eps:
    `GET /api/v1/patients/<guid>/clinics` -> [{"guid", "name"}], unit guids
    lower-case. This is the list sso's own `_eps_check` reads before it
    creates an invite, so the page and sso agree on "listed at the unit".

    eps also resolves a FHIR resource_id here; it names the platform guid in
    `X-PDHC-Patient-Guid`. An invite is stored under the guid we send, so a
    guid that is not the platform guid is refused rather than used."""
    _, data, headers = _get("eps", f"{_eps_base()}/api/v1/patients/{patient_guid}/clinics")
    platform = {k.lower(): v for k, v in headers.items()}.get("x-pdhc-patient-guid")
    if platform and str(platform).strip().lower() != patient_guid:
        raise InviteError("eps", "not_platform_guid", 200)
    if not isinstance(data, list):
        raise InviteError("eps", "unavailable", 200)
    out, seen = [], set()
    for c in data:
        if not isinstance(c, dict):
            continue
        g = str(c.get("care_unit_guid") or c.get("organisation_guid") or "").strip().lower()
        if g and g not in seen:
            seen.add(g)
            out.append({"guid": g, "name": c.get("name") or g})
    return out


def create_invite(*, patient_guid: str, care_unit_guid: str, given_name: str,
                  family_name: str) -> dict:
    """Step 2. Returns sso's 201 body; `invite_url` is in it ONCE."""
    base = (current_app.config.get("SSO_BASE_URL") or "").strip().rstrip("/")
    if not base:
        raise InviteError("sso", "not_configured")
    status, data = _post("sso", f"{base}/api/patient-links/invites",
                         {"ips_patient_guid": patient_guid,
                          "care_unit_guid": care_unit_guid,
                          "given_name": given_name, "family_name": family_name},
                         ok=(201,))
    url = data.get("invite_url")
    if not isinstance(url, str) or "/patient-invite/" not in url:
        raise InviteError("sso", "unavailable", status)
    return data
