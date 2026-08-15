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
- drop CDR1 clinical points (``filter_blocked_points``) whose ``org_guid``
  matches an active block's ``source_scope_id`` (v1 scope is clinic, which
  lives in the same identifier domain as ``org_guid``).
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


class IpsUnreachable(Exception):
    """ips.pdhc could not answer a consent question on this attempt.

    Raised (never swallowed) by the analysis-filter path so research
    reads FAIL CLOSED: without ips's verdict the dashboard must not
    return patient data for secondary-use purposes (#415/#422). The
    block-fetch path keeps its historical fail-open-with-banner
    behaviour — blocks compose with org scoping, consent does not."""


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
        from app.services.session_headers import outbound_session_headers
        h = {"Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self.service_key:
            h["X-Service-Key"] = self.service_key
        h.update(outbound_session_headers())
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

    def analysis_filter(
        self,
        patient_guids: list,
        purpose: str,
        research_project_guids: list | None = None,
    ) -> dict:
        """POST /api/v1/patients/analysis-filter (#415/#422) — ips is the
        single legal source of truth for ehds_opt_out /
        consented_research_projects / quality_registry_opt_out (D1 #404).

        Returns the verdict dict ``{"allowed": [...], "excluded":
        [{"patient_guid", "reason"}, ...]}``. Raises ``IpsUnreachable`` on
        any network error or non-200 so secondary-use reads fail closed.
        Never cached: consent revocation must bite immediately."""
        if not patient_guids:
            return {"allowed": [], "excluded": []}
        if not self.base_url:
            raise IpsUnreachable("IPS_BASE_URL not configured")
        url = f"{self.base_url}/api/v1/patients/analysis-filter"
        try:
            r = requests.post(
                url,
                json={
                    "patient_guids": list(patient_guids),
                    "purpose": purpose,
                    "research_project_guids": list(research_project_guids or []),
                },
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise IpsUnreachable(f"analysis-filter network error: {e}") from e
        if r.status_code != 200:
            raise IpsUnreachable(f"analysis-filter returned {r.status_code}")
        body = r.json() or {}
        return {
            "allowed": list(body.get("allowed") or []),
            "excluded": list(body.get("excluded") or []),
        }


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


# ---------------------------------------------------------------------------
# CDR1 dict-point variant (#471.4, DPO-approved #472)
# ---------------------------------------------------------------------------
# The CDR1 clinical read (routes/charts.py) yields dict points
# ``{code, at, value, unit, org_guid, ...}`` where ``code`` is the row's
# ``code_canonical`` (prod form ``urn:pdhc:concept/<guid>``), NOT a stored
# concept_guid. #472 Q1 approved deriving the concept identity by PARSING that
# URI as the basis for the indispensable-care lift filter. (The legacy
# ObservationCache row-object variant was removed in #471 as dead code;
# ``filter_blocked_points`` below is the sole live spärr filter.)

_CONCEPT_URI_PREFIX = "urn:pdhc:concept/"


def concept_guid_from_canonical(code_canonical):
    """Parse the plan.pdhc Concept guid out of a CDR ``code_canonical`` URI.

    Returns None for any value that is not ``urn:pdhc:concept/<guid>`` (a
    termbank URI, a legacy non-guid code, or None) — which, under a block,
    keeps the point HIDDEN (the DPO-approved under-expose fallback, #472 Q3)."""
    if isinstance(code_canonical, str) and code_canonical.startswith(_CONCEPT_URI_PREFIX):
        guid = code_canonical[len(_CONCEPT_URI_PREFIX):].strip()
        return guid or None
    return None


def _point_lift(concept_guid, observed_iso, lifts: list[Block]):
    """Return the lift Block that exposes this (concept, date), else None.
    concept_guid is already parsed and observed_iso is the point's ISO date
    string (CDR points already carry ISO ``at``; lift dates are ISO too)."""
    if not concept_guid or not lifts:
        return None
    for lift in lifts:
        allowed = {str(g) for g in (lift.lift_concept_guids or [])}
        if concept_guid not in allowed:
            continue
        if lift.lift_from_date and observed_iso and observed_iso < lift.lift_from_date:
            continue
        if lift.lift_until_date and observed_iso and observed_iso > lift.lift_until_date:
            continue
        return lift
    return None


def filter_blocked_points(points, blocks: Iterable[Block]):
    """Spärr filter for CDR1 dict points, with indispensable-care lift exposure.

    Returns ``(kept_points, exposures)`` where ``exposures`` is a list of
    ``(point, lift)`` tuples for the Q4 special audit (#472). A point from a
    blocked clinic is EXPOSED only if an active ``indispensable_care`` lift on
    that clinic covers its concept (parsed from ``code``/``code_canonical``)
    AND its date. Everything else from a blocked clinic stays hidden; a point
    with a non-parseable code stays hidden (safe fallback)."""
    blocks = list(blocks)
    blocked = blocked_clinic_ids(blocks)
    if not blocked:
        return list(points), []
    lifts_by_scope: dict[str, list[Block]] = {}
    for b in blocks:
        if (b.source_scope_type == "clinic"
                and b.lift_kind == "indispensable_care"
                and b.lift_concept_guids):
            lifts_by_scope.setdefault(b.source_scope_id, []).append(b)

    kept, exposures = [], []
    for p in points:
        org = p.get("org_guid")
        if org not in blocked:
            kept.append(p)
            continue
        cg = concept_guid_from_canonical(p.get("code") or p.get("code_canonical"))
        lift = _point_lift(cg, p.get("at"), lifts_by_scope.get(str(org), []))
        if lift is not None:
            kept.append(p)
            exposures.append((p, lift))
    return kept, exposures


def has_any_active_block(blocks: Iterable[Block]) -> bool:
    """True iff the patient has at least one active block.

    Drives the PDL § 4 ¶ 3 metadata-only banner: shown even when every
    blocked row was already filtered out by org-scoping (the patient
    must know that *something* exists, but not what).
    """
    return any(b.is_active for b in blocks)
