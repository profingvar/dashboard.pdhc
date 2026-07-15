"""Client for CDR1 (cdr.pdhc.se) — the production CDR (#462 D2/D3, #465).

The redesigned clinical dashboard reads production observations from CDR1
under a CARE-DELIVERY legal basis (vård: vårdrelation + spärr inre/yttre),
NOT the analysis-consent basis that CDR1's FHIR read path applies by
default (#422 ``check_patient_allowed``). A patient may legitimately be
treatable while having declined research; the analysis gate would hide
them from their own clinician.

This client therefore declares ``X-Access-Purpose: care-delivery`` so CDR1
can select the care-delivery filter (care_access_policy / spärr) instead
of analysis-consent. Honouring that header is CDR-side work tracked in
#468/D6; until it lands, CDR1 still applies its default filter, so treat
live results as PROVISIONAL and do not deploy this against production reads
before #468.

Org scoping is forwarded via ``X-Org-Guids`` (the caller's affiliation
care-unit guids, from ``auth.scope_org_guids``); admins send
``X-Is-Admin: 1``. Auth is the dashboard outbound service key
(``X-Source-Service``/``X-Service-Key``), the same pair federation.py uses
for CDR2-6.

Failure mode mirrors ``ips_client.fetch_active_blocks``: network / non-2xx
errors log and return an empty list rather than raising, so a CDR1 blip
degrades the picker to "no patients" with a banner rather than a 500.
"""
from __future__ import annotations

import logging

import requests
from flask import current_app

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 8.0
CARE_DELIVERY_PURPOSE = "care-delivery"


class Cdr1Client:
    def __init__(
        self,
        base_url: str | None = None,
        service_key: str | None = None,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.service_key = service_key
        self.token = token
        self.timeout = timeout

    # -- headers ---------------------------------------------------------
    def _headers(self, org_guids: list[str], is_admin: bool) -> dict:
        h = {
            "Accept": "application/json",
            # Declare the legal basis so CDR1 selects care_access_policy
            # (spärr) rather than #422 analysis-consent (honoured by #468).
            "X-Access-Purpose": CARE_DELIVERY_PURPOSE,
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self.service_key:
            h["X-Source-Service"] = "dashboard.pdhc"
            h["X-Service-Key"] = self.service_key
        if is_admin:
            h["X-Is-Admin"] = "1"
        elif org_guids:
            h["X-Org-Guids"] = ",".join(org_guids)
        # X2 (#423): forward the operator session if one is in scope.
        try:
            from app.services.session_headers import outbound_session_headers
            h.update(outbound_session_headers())
        except Exception:  # noqa: BLE001 — session headers are best-effort
            pass
        return h

    # -- patient directory ----------------------------------------------
    def list_org_patients(
        self, org_guids: list[str], *, is_admin: bool = False,
    ) -> list[dict]:
        """Return the patients (that HAVE data) selectable for these orgs.

        Calls CDR1's care-delivery patient index
        ``GET /api/v1/clinical/patients`` (#468) — org-scoped, restricted
        to patients with observations, ordered most-recent-activity first.
        Returns ``[{guid, name, birth_date, observation_count,
        last_observed_at}]``. Empty on any error or when the base URL is
        unconfigured (local dev / tests).
        """
        if not self.base_url:
            return []
        if not is_admin and not org_guids:
            return []  # non-admin with no affiliation sees nobody (Rule 24)
        body = self._get_json(
            "/api/v1/clinical/patients", org_guids, is_admin,
            what="patient list",
        )
        if body is None:
            return []
        return parse_clinical_patients(body)

    def patient_summary(
        self, patient_guid: str, org_guids: list[str], *, is_admin: bool = False,
    ) -> list[dict]:
        """Per-concept data counts for one patient (#468), ordered by count
        desc — feeds the sorted parameter dropdown (D4/#466). Returns
        ``[{code, unit, count, first_observed_at, last_observed_at}]``."""
        if not self.base_url or not patient_guid:
            return []
        if not is_admin and not org_guids:
            return []
        body = self._get_json(
            f"/api/v1/clinical/patient/{patient_guid}/summary",
            org_guids, is_admin, what="patient summary",
        )
        if body is None:
            return []
        params = body.get("parameters")
        return params if isinstance(params, list) else []

    def patient_series(
        self, patient_guid: str, codes: list[str] | None,
        frm: str | None, to: str | None,
        org_guids: list[str], *, is_admin: bool = False,
    ) -> list[dict]:
        """Time-series points for a patient from CDR1 (#464), optionally
        filtered to concept codes and an effective-date window. Each point:
        ``{code, at, value, unit, value_string, org_guid}`` (org_guid lets
        the caller apply spärr). Empty on error / unconfigured."""
        if not self.base_url or not patient_guid:
            return []
        if not is_admin and not org_guids:
            return []
        params: list[tuple[str, str]] = []
        for c in (codes or []):
            params.append(("code", c))
        if frm:
            params.append(("from", frm))
        if to:
            params.append(("to", to))
        body = self._get_json(
            f"/api/v1/clinical/patient/{patient_guid}/series",
            org_guids, is_admin, what="patient series", params=params,
        )
        if body is None:
            return []
        pts = body.get("points")
        return pts if isinstance(pts, list) else []

    # -- transport ------------------------------------------------------
    def _get_json(
        self, path: str, org_guids: list[str], is_admin: bool, *, what: str,
        params: list[tuple[str, str]] | None = None,
    ) -> dict | None:
        url = f"{self.base_url}{path}"
        try:
            r = requests.get(
                url, headers=self._headers(org_guids, is_admin),
                params=params or None, timeout=self.timeout,
            )
        except requests.RequestException:
            current_app.logger.warning("CDR1 %s failed (network)", what)
            return None
        if r.status_code >= 400:
            current_app.logger.warning(
                "CDR1 %s failed — status %s", what, r.status_code,
            )
            return None
        try:
            return r.json() or {}
        except ValueError:
            return None


def parse_patient_bundle(bundle: dict) -> list[dict]:
    """FHIR Patient searchset Bundle → ``[{guid, name, birth_date}]``.

    Pure function (no network) so it can be unit-tested directly.
    """
    out: list[dict] = []
    for entry in (bundle.get("entry") or []):
        res = entry.get("resource") or {}
        if res.get("resourceType") != "Patient":
            continue
        guid = res.get("id")
        if not guid:
            continue
        out.append({
            "guid": guid,
            "name": _human_name(res.get("name")),
            "birth_date": res.get("birthDate"),
        })
    return out


def parse_clinical_patients(body: dict) -> list[dict]:
    """CDR1 ``/api/v1/clinical/patients`` body → normalised picker rows.

    Maps ``patient_guid`` → ``guid`` so the template's ``p.guid`` keeps
    working. Pure function (no network) for direct unit testing.
    """
    out: list[dict] = []
    for p in (body.get("patients") or []):
        guid = p.get("patient_guid") or p.get("guid")
        if not guid:
            continue
        out.append({
            "guid": guid,
            "name": p.get("name") or "",
            "birth_date": p.get("birth_date"),
            "observation_count": p.get("observation_count"),
            "last_observed_at": p.get("last_observed_at"),
        })
    return out


def _human_name(names) -> str:
    """Best-effort display name from a FHIR HumanName list."""
    if not isinstance(names, list) or not names:
        return ""
    n = names[0] or {}
    if n.get("text"):
        return str(n["text"])
    given = " ".join(n.get("given") or [])
    family = n.get("family") or ""
    full = f"{given} {family}".strip()
    return full


def build_client() -> Cdr1Client:
    """Construct a Cdr1Client from app config + the request-context token.

    Factory so route code stays terse and tests can monkeypatch it.
    """
    from flask import session
    token = None
    try:
        token = session.get("sso_token")
    except RuntimeError:  # no request context
        token = None
    return Cdr1Client(
        base_url=current_app.config.get("CDR1_BASE_URL") or "",
        service_key=current_app.config.get("DASHBOARD_PDHC_SERVICE_KEY") or None,
        token=token,
    )
