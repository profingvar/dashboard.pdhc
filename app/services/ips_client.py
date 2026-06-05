"""Client for ips.pdhc — fetch active spärr (PatientBlock) entries.

Ticket #205 / spärr Phase 2. Implements:

- ``IpsClient.fetch_active_blocks(patient_guid)`` — raw HTTP GET against
  ips.pdhc's ``/api/v1/patients/<guid>/blocks?active=true``.
- A small in-process TTL cache (30 s by default — legal-confirmed
  2026-06-04 as the acceptable staleness window).
- ``get_active_blocks(patient_guid, ...)`` — convenience that consults
  the cache first.
- ``invalidate(patient_guid)`` — entry point for the webhook subscriber
  in IPS Renov 6 / #202 to evict on-demand. Until #202 lands the cache
  is bounded by the 30 s TTL alone.

The block list lets the dashboard:
- drop ``ObservationCache`` rows whose ``org_guid`` matches an active
  block's ``source_scope_id`` (v1 scope is clinic, which lives in the
  same identifier domain as ``org_guid``).
- render the PDL Ch 4 § 4 ¶ 3 banner ("uppgift om att det finns
  spärrade uppgifter…") when the patient has any active block, even
  if every blocked row was already filtered by org membership.

No global state beyond the module-level cache: the cache is keyed on
patient guid alone (not user) because a block is patient-scoped — every
caller's filter is identical for a given patient.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Iterable

import requests
from flask import current_app


DEFAULT_TTL_SECONDS = 30  # legal 2026-06-04
DEFAULT_TIMEOUT = 4.0


@dataclass(frozen=True)
class Block:
    """Subset of PatientBlock we care about for filtering + banner."""
    guid: str
    patient_guid: str
    source_scope_type: str  # 'clinic' | 'caregiver'
    source_scope_id: str
    is_active: bool
    lift_kind: str | None       # 'consent' | 'indispensable_care' | None
    lift_concept_guids: list | None
    lift_from_date: str | None  # ISO-8601 or None
    lift_until_date: str | None

    @classmethod
    def from_dict(cls, d: dict) -> "Block":
        return cls(
            guid=str(d.get("guid")),
            patient_guid=str(d.get("patient_guid")),
            source_scope_type=d.get("source_scope_type") or "clinic",
            source_scope_id=str(d.get("source_scope_id")),
            is_active=bool(d.get("is_active")),
            lift_kind=d.get("lift_kind"),
            lift_concept_guids=d.get("lift_concept_guids"),
            lift_from_date=d.get("lift_from_date"),
            lift_until_date=d.get("lift_until_date"),
        )


class IpsClient:
    """Thin wrapper around ips.pdhc's /api/v1/patients/<g>/blocks endpoint.

    Auth: forwards the caller's SSO bearer token. ips.pdhc validates it
    against sso.pdhc on every request. Service-key callers (sim,
    monitor) can pass ``service_key`` instead — ips.pdhc accepts either.
    """

    def __init__(
        self,
        token: str | None = None,
        service_key: str | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.token = token
        self.service_key = service_key
        self.base_url = (
            base_url or os.environ.get("IPS_BASE_URL", "")
        ).rstrip("/")
        self.timeout = timeout

    def _headers(self) -> dict:
        h = {"Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self.service_key:
            h["X-Service-Key"] = self.service_key
        return h

    def fetch_active_blocks(self, patient_guid: str) -> list[Block]:
        if not self.base_url:
            return []
        url = f"{self.base_url}/api/v1/patients/{patient_guid}/blocks"
        try:
            r = requests.get(
                url, params={"active": "true"},
                headers=self._headers(), timeout=self.timeout,
            )
        except requests.RequestException:
            current_app.logger.warning(
                "ips block fetch failed for %s (network error) — "
                "falling back to empty list",
                patient_guid[:12] if patient_guid else "?",
            )
            return []
        if r.status_code == 404:
            return []
        if r.status_code >= 400:
            current_app.logger.warning(
                "ips block fetch failed for %s — status %s",
                patient_guid[:12] if patient_guid else "?", r.status_code,
            )
            return []
        payload = r.json() or {}
        raw = payload.get("blocks") or payload.get("entry") or []
        return [Block.from_dict(b) for b in raw if isinstance(b, dict)]


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class _BlockCache:
    """Per-process TTL cache of {patient_guid: (expires_at, [Block])}.

    Bounded by TTL; no eviction policy beyond expiry. Patient population
    in the dashboard is small (per-org), so the working set comfortably
    fits in memory. If that ever stops being true, swap for an LRU.

    Thread-safe — gunicorn workers each have their own copy; the cache
    is not shared across workers (each warms independently).
    """

    def __init__(self, ttl: float = DEFAULT_TTL_SECONDS):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._data: dict[str, tuple[float, list[Block]]] = {}

    def get(self, patient_guid: str) -> list[Block] | None:
        with self._lock:
            entry = self._data.get(patient_guid)
        if not entry:
            return None
        expires_at, blocks = entry
        if time.monotonic() >= expires_at:
            return None
        return blocks

    def put(self, patient_guid: str, blocks: list[Block]) -> None:
        with self._lock:
            self._data[patient_guid] = (time.monotonic() + self.ttl, blocks)

    def invalidate(self, patient_guid: str | None = None) -> None:
        with self._lock:
            if patient_guid is None:
                self._data.clear()
            else:
                self._data.pop(patient_guid, None)


_cache = _BlockCache()


def invalidate(patient_guid: str | None = None) -> None:
    """Webhook entry point — clear the cache for a patient (or all).

    Called from the IPS Renov 6 / #202 webhook subscriber once that
    ships. Until then the cache is bounded by the 30 s TTL alone.
    """
    _cache.invalidate(patient_guid)


def get_active_blocks(
    patient_guid: str,
    *,
    client: IpsClient | None = None,
    use_cache: bool = True,
) -> list[Block]:
    """Return the patient's active blocks. Caches results for 30 s."""
    if not patient_guid:
        return []
    if use_cache:
        cached = _cache.get(patient_guid)
        if cached is not None:
            return cached
    client = client or _default_client()
    blocks = [b for b in client.fetch_active_blocks(patient_guid) if b.is_active]
    if use_cache:
        _cache.put(patient_guid, blocks)
    return blocks


def _default_client() -> IpsClient:
    """Build a client using the request-context SSO token (if any)
    plus the dashboard service key as fallback for service-to-service
    paths (sim refresh, monitor probes)."""
    from flask import session  # local import — module is import-time safe
    token = None
    try:
        token = session.get("sso_token")
    except RuntimeError:  # no request context
        token = None
    return IpsClient(
        token=token,
        service_key=current_app.config.get("DASHBOARD_PDHC_SERVICE_KEY") or None,
        base_url=current_app.config.get("IPS_BASE_URL") or None,
    )


# ---------------------------------------------------------------------------
# Filter helpers
# ---------------------------------------------------------------------------


def blocked_clinic_ids(blocks: Iterable[Block]) -> set[str]:
    """Return the set of clinic GUIDs whose data must be hidden.

    Caregiver-scope blocks are out of scope for v1 (IPS Renov 8 / #204);
    they are intentionally ignored here. The caregiver case will need
    a separate lookup table to resolve clinic→caregiver membership,
    which doesn't exist yet.
    """
    return {
        b.source_scope_id
        for b in blocks
        if b.is_active and b.source_scope_type == "clinic"
    }


def filter_blocked_rows(rows, blocks: Iterable[Block]):
    """Drop ObservationCache rows whose org_guid matches an active block.

    Rows are kept iff:
    - their ``org_guid`` is NOT in any active block's source_scope_id,
      OR
    - they satisfy at least one block's *lift filter* — the
      ``lift_concept_guids`` mechanical filter applied to the row's
      ``concept_guid`` + observation date. (Legal-confirmed 2026-06-04:
      indispensable-care lifts MUST be mechanically filtered.)
    """
    blocked = blocked_clinic_ids(blocks)
    if not blocked:
        return list(rows)
    # Pre-index lifts by source_scope_id for O(1) lookup.
    lifts_by_scope: dict[str, list[Block]] = {}
    for b in blocks:
        if b.source_scope_type != "clinic":
            continue
        if b.lift_kind == "indispensable_care" and b.lift_concept_guids:
            lifts_by_scope.setdefault(b.source_scope_id, []).append(b)

    out = []
    for r in rows:
        org = getattr(r, "org_guid", None)
        if org not in blocked:
            out.append(r)
            continue
        # The block is active for this scope; check lifts.
        if _row_passes_any_lift(r, lifts_by_scope.get(str(org), [])):
            out.append(r)
    return out


def _row_passes_any_lift(row, lifts: list[Block]) -> bool:
    """True iff at least one lift exposes this row."""
    if not lifts:
        return False
    concept = str(getattr(row, "concept_guid", "") or "")
    observed = getattr(row, "observed_at", None)
    observed_iso = observed.isoformat() if observed is not None else None
    for lift in lifts:
        allowed = {str(g) for g in (lift.lift_concept_guids or [])}
        if concept not in allowed:
            continue
        if lift.lift_from_date and observed_iso and observed_iso < lift.lift_from_date:
            continue
        if lift.lift_until_date and observed_iso and observed_iso > lift.lift_until_date:
            continue
        return True
    return False


def has_any_active_block(blocks: Iterable[Block]) -> bool:
    """True iff the patient has at least one active block.

    Drives the PDL § 4 ¶ 3 metadata-only banner: shown even when every
    blocked row was already filtered out by org-scoping (the patient
    must know that *something* exists, but not what).
    """
    return any(b.is_active for b in blocks)
