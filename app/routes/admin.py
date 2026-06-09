"""Admin endpoints + CLI (tickets #213, #215).

  POST /admin/cache/scrub  (#213)
    Body: {"patient_guid": "...?", "org_guid": "...?", "reason": "...?"}
    Auth: SU admin only.
    Behaviour: immediately removes matching observation_cache rows;
               writes a DashboardAudit row with event_type='cache_scrub'
               and a payload_snapshot carrying the filter + count.

  GET  /admin/audit             (#215)
  GET  /admin/audit/export.csv  (#215)
    SU-only operator view of dashboard_audit. Filterable by date range,
    user_guid, patient_guid, route, event_type. Surfaces every row
    shape shipped in this PDL series: 'read', 'admin_override_required',
    'admin_override', 'cache_scrub', 'research_export'. Operationalises
    PDL Ch 4 §3 ('kontroller').

  flask cache-sweep [--ttl-hours N]  (#213)
    Drops rows whose fetched_at is older than the TTL (default from
    OBSERVATION_CACHE_TTL_HOURS config, 48h). Intended to run hourly
    from cron on the macmini -> see docs/technical.md.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Optional

import click
from flask import (
    Blueprint, Response, current_app, g, jsonify, render_template, request,
)

from app.models import DashboardAudit, db
from app.services.audit import audit_read
from app.services.cache_retention import (
    scrub_observations,
    sweep_expired_observations,
)


bp = Blueprint("admin", __name__, url_prefix="/admin")


def _is_su_admin() -> bool:
    user = getattr(g, "current_user", None)
    return bool(getattr(user, "is_admin", False)) or bool(
        getattr(user, "is_su", False),
    )


@bp.post("/cache/scrub")
@audit_read
def cache_scrub():
    """Admin-triggered immediate scrub of observation_cache rows.

    Audit row shape (uses #212's event_type column):
      event_type        = 'cache_scrub'
      patient_guid      = the patient filter when supplied, else NULL
      route             = 'POST /admin/cache/scrub'
      n_rows_returned   = number of cache rows DELETED
      payload_snapshot  = {
          patient_guid, org_guid, reason, deleted_count
      }
      admin_justification = reason (so /admin/audit surfaces the why
                            alongside off-org-read justifications).
    """
    if not _is_su_admin():
        return jsonify({"error": "forbidden"}), 403

    payload = request.get_json(silent=True) or {}
    patient_guid = (payload.get("patient_guid") or "").strip() or None
    org_guid = (payload.get("org_guid") or "").strip() or None
    reason = (payload.get("reason") or "").strip() or None

    if not patient_guid and not org_guid:
        return (
            jsonify({
                "error":
                    "at least one of patient_guid / org_guid is required",
            }),
            400,
        )

    try:
        deleted = scrub_observations(
            patient_guid=patient_guid, org_guid=org_guid,
        )
    except Exception as exc:  # noqa: BLE001
        current_app.logger.error("cache scrub failed: %s", exc)
        return jsonify({"error": "scrub failed"}), 500

    g._audit_event_type = "cache_scrub"
    g._audit_admin_justification = reason
    g._audit_n_rows = deleted
    g._audit_patient_guid = patient_guid
    g._audit_payload_snapshot = {
        "patient_guid": patient_guid,
        "org_guid": org_guid,
        "reason": reason,
        "deleted_count": deleted,
    }

    return jsonify({
        "deleted_count": deleted,
        "patient_guid": patient_guid,
        "org_guid": org_guid,
    })


def register_cache_sweep_cli(app):
    @app.cli.command("cache-sweep")
    @click.option(
        "--ttl-hours", type=int, default=None,
        help="TTL in hours; defaults to OBSERVATION_CACHE_TTL_HOURS",
    )
    @click.option(
        "--dry-run", is_flag=True, default=False,
        help="Report the count that would be removed but do nothing.",
    )
    def cache_sweep(ttl_hours, dry_run):
        """Drop observation_cache rows older than the TTL (PDL Ch 4)."""
        ttl = ttl_hours or app.config.get(
            "OBSERVATION_CACHE_TTL_HOURS", 48,
        )
        if dry_run:
            from datetime import datetime, timedelta, timezone
            from app.models import ObservationCache
            cutoff = datetime.now(timezone.utc) - timedelta(hours=int(ttl))
            n = (
                ObservationCache.query
                .filter(ObservationCache.fetched_at < cutoff)
                .count()
            )
            click.echo(f"[dry-run] ttl_hours={ttl} would_delete={n}")
            return
        n = sweep_expired_observations(int(ttl))
        click.echo(f"cache-sweep ttl_hours={ttl} deleted={n}")


# ---------------------------------------------------------------------------
# /admin/audit — operator view (ticket #215)
# ---------------------------------------------------------------------------

_PAGE_SIZE = 100
_CSV_HARD_CAP = 50_000  # one PDL kontroll session, not a year-end dump


def _parse_dt(raw: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 datetime (with or without trailing Z). Returns
    None if blank/unparseable so the filter degrades to no-op rather
    than 400-ing the whole view."""
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _build_audit_query(args):
    """Build the SQLAlchemy query for /admin/audit from query args.

    Filters are AND-ed. A missing/blank filter is skipped. Returns the
    query with a default order (timestamp DESC) so both the HTML and
    CSV paths get consistent ordering.
    """
    q = DashboardAudit.query

    dt_from = _parse_dt(args.get("from"))
    dt_to = _parse_dt(args.get("to"))
    if dt_from is not None:
        q = q.filter(DashboardAudit.timestamp >= dt_from)
    if dt_to is not None:
        q = q.filter(DashboardAudit.timestamp <= dt_to)

    user_guid = (args.get("user_guid") or "").strip()
    if user_guid:
        q = q.filter(DashboardAudit.user_guid == user_guid)

    patient_guid = (args.get("patient_guid") or "").strip()
    if patient_guid:
        q = q.filter(DashboardAudit.patient_guid == patient_guid)

    route = (args.get("route") or "").strip()
    if route:
        # Exact match — operators paste the rule literal from a prior row.
        q = q.filter(DashboardAudit.route == route)

    event_type = (args.get("event_type") or "").strip()
    if event_type:
        q = q.filter(DashboardAudit.event_type == event_type)

    return q.order_by(DashboardAudit.timestamp.desc())


def _serialise_row(row) -> dict:
    """Project a DashboardAudit row to the JSON-safe shape both the
    template and the CSV consume."""
    return {
        "guid": str(row.guid),
        "timestamp": (
            row.timestamp.isoformat() if row.timestamp else None
        ),
        "user_guid": row.user_guid,
        "user_org_guids": row.user_org_guids,
        "route": row.route,
        "event_type": row.event_type,
        "patient_guid": (
            str(row.patient_guid) if row.patient_guid else None
        ),
        "n_rows_returned": row.n_rows_returned,
        "response_status": row.response_status,
        "session_id": row.session_id,
        "admin_justification": row.admin_justification,
        "payload_snapshot": row.payload_snapshot,
    }


@bp.get("/audit")
@audit_read
def audit_view():
    """SU-only audit browse. Filters via query args; paginates 100/page."""
    if not _is_su_admin():
        return jsonify({"error": "forbidden"}), 403

    try:
        page = max(1, int(request.args.get("page", "1")))
    except (TypeError, ValueError):
        page = 1

    q = _build_audit_query(request.args)
    total = q.count()
    rows = (
        q.limit(_PAGE_SIZE)
         .offset((page - 1) * _PAGE_SIZE)
         .all()
    )
    serialised = [_serialise_row(r) for r in rows]
    g._audit_n_rows = len(serialised)
    # The view IS a kontroll action — flag it with its own event_type
    # so /admin/audit calls themselves are easy to find on a future
    # audit-of-the-audit pass.
    g._audit_event_type = "admin_audit_view"

    return render_template(
        "admin_audit.html",
        rows=serialised,
        total=total,
        page=page,
        page_size=_PAGE_SIZE,
        filters={
            "from": request.args.get("from", ""),
            "to": request.args.get("to", ""),
            "user_guid": request.args.get("user_guid", ""),
            "patient_guid": request.args.get("patient_guid", ""),
            "route": request.args.get("route", ""),
            "event_type": request.args.get("event_type", ""),
        },
    )


_CSV_COLUMNS = [
    "guid", "timestamp", "user_guid", "user_org_guids",
    "route", "event_type", "patient_guid", "n_rows_returned",
    "response_status", "session_id", "admin_justification",
    "payload_snapshot",
]


@bp.get("/audit/export.csv")
@audit_read
def audit_export_csv():
    """SU-only filtered CSV export. Capped at _CSV_HARD_CAP rows so a
    misclick can't take 5 minutes to produce."""
    if not _is_su_admin():
        return jsonify({"error": "forbidden"}), 403

    q = _build_audit_query(request.args)
    rows = q.limit(_CSV_HARD_CAP).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_CSV_COLUMNS)
    for r in rows:
        s = _serialise_row(r)
        writer.writerow([
            s["guid"], s["timestamp"], s["user_guid"],
            _json_dump(s["user_org_guids"]),
            s["route"], s["event_type"], s["patient_guid"],
            s["n_rows_returned"], s["response_status"],
            s["session_id"], s["admin_justification"],
            _json_dump(s["payload_snapshot"]),
        ])

    g._audit_n_rows = len(rows)
    g._audit_event_type = "admin_audit_export"
    g._audit_payload_snapshot = {
        "filters": {
            k: request.args.get(k, "") for k in
            ("from", "to", "user_guid", "patient_guid", "route",
             "event_type")
        },
        "row_count": len(rows),
    }

    out = buf.getvalue()
    headers = {
        "Content-Disposition":
            "attachment; filename=dashboard_audit.csv",
        "Cache-Control": "no-store",
    }
    return Response(out, mimetype="text/csv", headers=headers)


def _json_dump(value) -> str:
    """JSON-encode a list/dict cell for the CSV. Returns '' for None
    so the column stays empty rather than ``"null"``."""
    if value is None:
        return ""
    import json
    return json.dumps(value, sort_keys=True, ensure_ascii=False)
