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

def x1_tuple(blob, route_rule: str) -> dict:
    """X1 (#407) — the extended access-log tuple fields that don't have
    dedicated columns: person_guid, role_guid (from the ACTIVE
    affiliation), purpose and access_basis (closed enums,
    plans/pdhc_data_shapes.md §5).

    Purpose is route-classed: researcher cohort surface → research,
    admin surface → administration, everything patient-facing → care
    (the dashboard's clinical views are care follow-up, not secondary
    use). access_basis: su_admin for admins, research_consent for
    research reads (the #415 ips consent join is what admits them),
    same_unit otherwise (dashboard reads are Zone-1 scoped)."""
    if not isinstance(blob, dict):
        blob = {}
    affs = blob.get("affiliations") or []
    active = None
    active_guid = blob.get("active_affiliation_guid")
    if active_guid:
        active = next((a for a in affs
                       if a.get("affiliation_guid") == active_guid), None)
    if active is None and len(affs) == 1:
        active = affs[0]

    if route_rule.startswith(("GET /api/cohort", "POST /api/cohort")) \
            or "/api/cohort" in route_rule:
        purpose = "research"
    elif "/admin" in route_rule:
        purpose = "administration"
    else:
        purpose = "care"

    if blob.get("is_su_admin"):
        basis = "su_admin"
    elif purpose == "research":
        basis = "research_consent"
    else:
        basis = "same_unit"

    return {
        "person_guid": blob.get("user_guid"),
        "role_guid": (active or {}).get("role_guid"),
        "purpose": purpose,
        "access_basis": basis,
    }


def _write_audit_row(*, status: int, n_rows: int | None) -> None:
    """Insert a single DashboardAudit row. Never raises — a failed
    audit write must not break the response to the caller."""
    try:
        blob = getattr(g, "access_blob", None) or {}
        user_guid = _user_guid(blob)
        org_ids = _org_ids(blob)
        session_id = _session_id(blob)
        # Ticket #213: routes whose patient guid isn't on the URL
        # (e.g. POST /admin/cache/scrub takes it in the JSON body) can
        # override the auto-derived value via g._audit_patient_guid.
        patient_guid = (
            getattr(g, "_audit_patient_guid", None)
            or _patient_guid_from_request()
        )
        route_rule = _route_rule()
        # Allow the route to override n_rows (streamed responses, custom
        # aggregations) by setting g._audit_n_rows.
        override = getattr(g, "_audit_n_rows", None)
        if override is not None:
            n_rows = int(override)
        # Ticket #212: routes signal admin-override status via
        # g._audit_event_type ('admin_override' | 'admin_override_required'
        # | 'cache_scrub' for #213, ...) and g._audit_admin_justification.
        # Untouched by every other route so the column stays at the
        # 'read' default.
        event_type = getattr(g, "_audit_event_type", None) or "read"
        admin_justification = getattr(
            g, "_audit_admin_justification", None,
        )
        # Ticket #213: routes can attach a per-event JSONB blob via
        # g._audit_payload_snapshot (e.g. the cache-scrub filter +
        # deleted_count). Defaults to None so the column stays NULL on
        # the @audit_read default path.
        payload_snapshot = getattr(g, "_audit_payload_snapshot", None)

        # X1 (#407): merge the extended tuple into the JSONB detail —
        # role_guid from the active affiliation + purpose/access_basis
        # closed enums. Routes may pre-set keys via payload_snapshot;
        # explicit values win over the derived defaults.
        payload_snapshot = {**x1_tuple(blob, route_rule),
                            **(payload_snapshot or {})}

        row = DashboardAudit(
            user_guid=user_guid,
            user_org_guids=list(org_ids or []),
            route=route_rule,
            patient_guid=patient_guid,
            n_rows_returned=n_rows,
            response_status=int(status),
            session_id=session_id,
            event_type=event_type,
            admin_justification=admin_justification,
            payload_snapshot=payload_snapshot,
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
        # M0/X1: attribute the read to care units — affiliations[] first,
        # legacy organization_ids fallback (same dual-read as app.auth).
        from app.auth import scope_org_guids
        return scope_org_guids(blob)
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
