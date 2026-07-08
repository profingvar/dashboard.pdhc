"""Client for gateway.pdhc — read observations for an organisation.

The dashboard is read-only (Rule 6: relies on gateway.pdhc). All matching is
GUID-based (Rule 18). Consumed payloads are expected to follow FHIR R5
Observation shape (Rule 15).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable
import os
import requests

from app.models import db, ObservationCache, RefreshLog


@dataclass
class NormalisedObservation:
    source_obs_guid: str
    patient_guid: str
    org_guid: str
    concept_guid: str
    concept_name: str
    value: float | None
    unit: str | None
    observed_at: datetime
    raw: dict


class GatewayClient:
    """Read observations from gateway.pdhc.

    Auth: forwards the *user's* SSO bearer token to gateway, which
    validates it via the same SSO flow gateway uses for its own web
    routes. Gateway then filters observations by the requesting org
    on the underlying contract (resolved via contract.pdhc).
    """

    def __init__(self, token: str, base_url: str | None = None, timeout: float = 15.0):
        if not token:
            raise ValueError("GatewayClient requires an SSO bearer token")
        self.token = token
        self.base_url = (base_url or os.environ.get("GATEWAY_BASE_URL", "")).rstrip("/")
        self.timeout = timeout

    def _headers(self):
        from app.services.session_headers import outbound_session_headers
        return {
            "Accept": "application/fhir+json",
            "Authorization": f"Bearer {self.token}",
            **outbound_session_headers(),
        }

    def fetch_observations(self, org_guid: str) -> list[dict]:
        url = f"{self.base_url}/api/v1/observations"
        r = requests.get(url, params={"organization": org_guid},
                         headers=self._headers(), timeout=self.timeout)
        r.raise_for_status()
        bundle = r.json()
        return [e.get("resource", {}) for e in (bundle.get("entry") or [])]


def normalise(resource: dict, org_guid: str) -> NormalisedObservation | None:
    if resource.get("resourceType") != "Observation":
        return None
    obs_guid = resource.get("id")
    subject = (resource.get("subject") or {}).get("reference", "")
    patient_guid = subject.split("/")[-1] if subject else None
    coding = ((resource.get("code") or {}).get("coding") or [{}])[0]
    concept_guid = coding.get("code", "")
    concept_name = coding.get("display", "") or (resource.get("code") or {}).get("text", "")
    vq = resource.get("valueQuantity") or {}
    value = vq.get("value")
    unit = vq.get("unit")

    # Graph observations have no valueQuantity — value stays None, data is in raw
    if value is None and resource.get("valueString") == "graph":
        unit = "graph"

    eff = resource.get("effectiveDateTime")
    if eff:
        try:
            observed_at = datetime.fromisoformat(eff.replace("Z", "+00:00"))
        except ValueError:
            observed_at = datetime.now(timezone.utc)
    else:
        observed_at = datetime.now(timezone.utc)

    if not (obs_guid and patient_guid and concept_guid):
        return None
    return NormalisedObservation(
        source_obs_guid=obs_guid,
        patient_guid=patient_guid,
        org_guid=org_guid,
        concept_guid=concept_guid,
        concept_name=concept_name,
        value=value,
        unit=unit,
        observed_at=observed_at,
        raw=resource,
    )


def refresh_org(user_guid: str, org_guid: str, client: GatewayClient | None = None) -> RefreshLog:
    """Clear + repopulate ObservationCache for a single org. Logged in RefreshLog."""
    client = client or GatewayClient()
    log = RefreshLog(user_guid=user_guid, org_guid=org_guid, status="running")
    db.session.add(log)
    db.session.commit()
    try:
        resources = client.fetch_observations(org_guid)
        ObservationCache.query.filter_by(org_guid=org_guid).delete()
        count = 0
        for r in resources:
            n = normalise(r, org_guid)
            if not n:
                continue
            db.session.add(ObservationCache(
                source_obs_guid=n.source_obs_guid,
                patient_guid=n.patient_guid,
                org_guid=n.org_guid,
                concept_guid=n.concept_guid,
                concept_name=n.concept_name,
                value=n.value,
                unit=n.unit,
                observed_at=n.observed_at,
                raw=n.raw,
            ))
            count += 1
        log.rows_fetched = count
        log.status = "ok"
        log.finished_at = datetime.now(timezone.utc)
        db.session.commit()
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        log = db.session.get(RefreshLog, log.guid)
        log.status = "error"
        log.error = str(exc)
        log.finished_at = datetime.now(timezone.utc)
        db.session.commit()
        raise
    return log
