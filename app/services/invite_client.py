"""The two calls behind "Bjud in patient" (#1050): eps, then sso.

    1. eps.pdhc  POST {IPS_BASE_URL}/api/v1/patients/register-at-unit
                 {"given_name", "family_name", "personnummer",
                  "care_unit_guid", "mobile"?}
                 -> 201/200 {"patient_guid", "created", "assignment"}
    2. sso.pdhc  POST {SSO_BASE_URL}/api/patient-links/invites
                 {"ips_patient_guid", "care_unit_guid", "given_name",
                  "family_name"} -> 201 {"invite_url", "expires_at", ...}

AUTH IS THE PROFESSIONAL'S OWN SSO BEARER on both, and nothing else (the
cdr_review_client rule): eps decides from that person's affiliations, sso
requires that person to be affiliated to the unit, and both record that
person as the one who registered and invited. No service key is sent.

EVERY FAILURE IS A NAMED EXCEPTION carrying which step failed and the
neighbour's own `error` code, so the page can say exactly what happened —
in particular "the patient IS registered at the unit, only the invite
failed" (step 2 after step 1), which must never read as "nothing happened".
The personnummer is in the eps request body only; it is never logged here.
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


def register_at_unit(*, given_name: str, family_name: str, personnummer: str,
                     care_unit_guid: str, mobile: str | None) -> dict:
    """Step 1. Returns {"patient_guid", "created", "assignment"}."""
    base = (current_app.config.get("IPS_BASE_URL") or "").strip().rstrip("/")
    if not base:
        raise InviteError("eps", "not_configured")
    body = {"given_name": given_name, "family_name": family_name,
            "personnummer": personnummer, "care_unit_guid": care_unit_guid}
    if mobile:
        body["mobile"] = mobile
    status, data = _post("eps", f"{base}/api/v1/patients/register-at-unit",
                         body, ok=(200, 201))
    try:
        guid = str(uuid.UUID(str(data.get("patient_guid") or "")))
    except ValueError:
        raise InviteError("eps", "unavailable", status)
    return {"patient_guid": guid, "created": bool(data.get("created")),
            "assignment": data.get("assignment")}


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
