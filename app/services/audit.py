"""PDL Ch 4 §3 kontroller log — read-side audit decorator.

Ticket #211. Apply ``@audit_read`` to every patient-touching route. Each
call produces exactly one ``DashboardAudit`` row, including 4xx denials.

Routes that touch a single patient have ``<guid>`` (or similar) in the
URL view args — the decorator picks the patient guid up automatically.
Cohort-aggregated routes touch many patients; ``patient_guid`` is NULL
on those rows and ``n_rows_returned`` is the denominator.

To override the auto-derived row count (e.g. when the response is a
streamed CSV), set ``g._audit_n_rows`` inside the route before
returning.

5xx errors that happen BEFORE the route returns (route blew up
mid-DB-call) are NOT logged here — we'd have no audit context, and the
``after_request`` path would risk a cascading commit failure. They
remain in the gunicorn error log. 4xx (``flask.abort``) IS logged.
"""
from __future__ import annotations

from functools import wraps
from typing import Any

from flask import current_app, g, request
from werkzeug.exceptions import HTTPException
from werkzeug.wrappers import Response as WzResponse

from app.models import DashboardAudit, db


# View-arg names that carry a single-patient guid. Keep in sync with the
# URL rules under app/routes/.
_PATIENT_VIEW_ARGS = ("guid", "patient_guid")

# JSON keys whose value is a list of patient-data rows. Used to infer
# n_rows_returned when the route doesn't set ``g._audit_n_rows``.
_LIST_KEYS = ("entry", "members", "points", "events", "series", "groups",
              "cohorts", "patients")


def audit_read(fn):
    """Wrap a Flask view so each call writes one ``DashboardAudit`` row.

    Captures: user_guid, organization_ids snapshot, route rule, patient
    guid (when present in the URL), best-effort row count, and the
    response status. 4xx (HTTPException) is logged; 5xx pre-return is
    not (the route raised before producing audit context).
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            response = fn(*args, **kwargs)
        except HTTPException as exc:
            _write_audit_row(status=exc.code or 500, n_rows=None)
            raise
        status = _resolve_status(response)
        n_rows = _resolve_n_rows(response)
        _write_audit_row(status=status, n_rows=n_rows)
        return response
    return wrapper


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _write_audit_row(*, status: int, n_rows: int | None) -> None:
    """Insert a single DashboardAudit row. Never raises — a failed
    audit write must not break the response to the caller."""
    try:
        blob = getattr(g, "access_blob", None) or {}
        user_guid = _user_guid(blob)
        org_ids = _org_ids(blob)
        session_id = _session_id(blob)
        patient_guid = _patient_guid_from_request()
        route_rule = _route_rule()
        # Allow the route to override n_rows (streamed responses, custom
        # aggregations) by setting g._audit_n_rows.
        override = getattr(g, "_audit_n_rows", None)
        if override is not None:
            n_rows = int(override)

        row = DashboardAudit(
            user_guid=user_guid,
            user_org_guids=list(org_ids or []),
            route=route_rule,
            patient_guid=patient_guid,
            n_rows_returned=n_rows,
            response_status=int(status),
            session_id=session_id,
        )
        db.session.add(row)
        db.session.commit()
    except Exception as exc:  # noqa: BLE001
        try:
            db.session.rollback()
        except Exception:
            pass
        try:
            current_app.logger.warning("dashboard_audit write failed: %s", exc)
        except Exception:
            pass


def _user_guid(blob: Any) -> str | None:
    if isinstance(blob, dict):
        return blob.get("user_guid")
    return getattr(blob, "user_guid", None)


def _org_ids(blob: Any) -> list[str]:
    if isinstance(blob, dict):
        return list(blob.get("organization_ids") or [])
    return list(getattr(blob, "organization_ids", None) or [])


def _session_id(blob: Any) -> str | None:
    """SSO Phase 3 (#191) will populate this. Until then it's None."""
    if isinstance(blob, dict):
        return blob.get("session_id")
    return getattr(blob, "session_id", None)


def _patient_guid_from_request() -> str | None:
    """Pull the patient guid from the URL view args. Returns None for
    cohort-aggregated routes that don't bind a single patient."""
    view_args = request.view_args or {}
    for key in _PATIENT_VIEW_ARGS:
        if key in view_args and view_args[key]:
            return str(view_args[key])
    # Fall back to a ``patient=`` query arg (the /api/v1/series path).
    qv = request.args.get("patient")
    return qv or None


def _route_rule() -> str:
    """Stable label for the route, in ``"<METHOD> <rule>"`` form."""
    rule = (request.url_rule.rule if request.url_rule else request.path) or "?"
    return f"{request.method} {rule}"[:256]


def _resolve_status(response: Any) -> int:
    """Extract HTTP status from a Flask view return value."""
    if isinstance(response, WzResponse):
        return response.status_code
    if isinstance(response, tuple):
        # (body, status) or (body, status, headers)
        if len(response) >= 2 and isinstance(response[1], int):
            return response[1]
    return 200


def _resolve_n_rows(response: Any) -> int | None:
    """Best-effort row count from common JSON shapes used by this app.

    Returns None for streamed responses and shapes we don't recognise —
    routes can always set ``g._audit_n_rows`` to override.
    """
    body = response
    if isinstance(response, WzResponse):
        # Don't pull a streamed body into memory.
        if getattr(response, "is_streamed", False):
            return None
        try:
            body = response.get_json(silent=True)
        except Exception:
            return None
        if body is None:
            return None
    elif isinstance(response, tuple):
        body = response[0]
        if isinstance(body, WzResponse):
            return _resolve_n_rows(body)

    if isinstance(body, dict):
        # Explicit count fields win — they're cheaper and authoritative.
        for k in ("n", "total"):
            v = body.get(k)
            if isinstance(v, int):
                return v
        for key in _LIST_KEYS:
            v = body.get(key)
            if isinstance(v, list):
                return len(v)
    if isinstance(body, list):
        return len(body)
    return None
