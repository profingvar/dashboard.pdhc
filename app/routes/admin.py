"""Admin endpoints + CLI (ticket #213).

  POST /admin/cache/scrub
    Body: {"patient_guid": "...?", "org_guid": "...?", "reason": "...?"}
    Auth: SU admin only.
    Behaviour: immediately removes matching observation_cache rows;
               writes a DashboardAudit row with event_type='cache_scrub'
               and a payload_snapshot carrying the filter + count.

  flask cache-sweep [--ttl-hours N]
    Drops rows whose fetched_at is older than the TTL (default from
    OBSERVATION_CACHE_TTL_HOURS config, 48h). Intended to run hourly
    from cron on the macmini -> see docs/technical.md.
"""
from __future__ import annotations

import click
from flask import Blueprint, current_app, g, jsonify, request

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
