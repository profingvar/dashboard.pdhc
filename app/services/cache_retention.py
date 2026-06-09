"""ObservationCache retention + admin scrub (ticket #213).

PDL Ch 4 §§ 3-4 require the dashboard to be able to act on data after
the fact: block events, deletion requests, audit windows. The cache
must therefore (a) shed stale rows on a schedule and (b) accept
targeted admin scrubs.

This module is the pure-DB layer:

  - ``sweep_expired_observations(ttl_hours)`` drops rows whose
    ``fetched_at`` is older than ``NOW() - ttl_hours``. Called from the
    ``flask cache-sweep`` CLI (run hourly from cron on the macmini).
    Refreshes overwrite the same source guids and reset ``fetched_at``
    via ``default=_now`` in the model, so a row's ``fetched_at`` is the
    last time the org was refreshed for that row.

  - ``scrub_observations(patient_guid, org_guid)`` removes rows matching
    EITHER or BOTH filters. At least one filter must be supplied; the
    caller (admin route) is responsible for that check + for writing
    the audit row.

Both return the integer row count deleted. Neither writes audit rows
itself — audit shaping happens at the call site (CLI logs to stderr,
admin route writes a ``DashboardAudit`` row).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from app.models import ObservationCache, db


def sweep_expired_observations(ttl_hours: int) -> int:
    """Delete observation_cache rows older than ttl_hours by fetched_at.

    Returns the number of rows removed. Caller is responsible for the
    commit -> on failure it rolls back and re-raises."""
    if ttl_hours is None or int(ttl_hours) <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=int(ttl_hours))
    try:
        n = (
            ObservationCache.query
            .filter(ObservationCache.fetched_at < cutoff)
            .delete(synchronize_session=False)
        )
        db.session.commit()
        return int(n or 0)
    except Exception:
        db.session.rollback()
        raise


def scrub_observations(
    *,
    patient_guid: Optional[str] = None,
    org_guid: Optional[str] = None,
) -> int:
    """Immediately delete rows matching the supplied filters.

    At least one of ``patient_guid`` / ``org_guid`` is required — a
    no-filter scrub would wipe the whole table, which the admin route
    must not be able to trigger. Raises ``ValueError`` if both are
    empty.
    """
    if not patient_guid and not org_guid:
        raise ValueError(
            "scrub_observations requires at least one of "
            "patient_guid or org_guid",
        )
    q = ObservationCache.query
    if patient_guid:
        q = q.filter(ObservationCache.patient_guid == patient_guid)
    if org_guid:
        q = q.filter(ObservationCache.org_guid == org_guid)
    try:
        n = q.delete(synchronize_session=False)
        db.session.commit()
        return int(n or 0)
    except Exception:
        db.session.rollback()
        raise
