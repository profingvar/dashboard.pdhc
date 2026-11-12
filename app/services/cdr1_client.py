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


class Cdr1EventsError(Exception):
    """Base for the strict events read. The older methods in this module
    return [] on any failure, which `charts.py` depends on and which is left
    alone. The events read RAISES instead, because "this patient has no
    events" and "cdr1 did not answer" are different statements and the
    patient dashboard must be able to tell them apart (plan step 24)."""


class Cdr1NotConfigured(Cdr1EventsError):
    """CDR1_BASE_URL is unset. Refused by name; nothing is fabricated."""


class Cdr1Unreachable(Cdr1EventsError):
    """Asked, and no usable answer: network, 5xx, 401/403, non-JSON."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class Cdr1BadCursor(Cdr1EventsError):
    """cdr1 refused the cursor (400 invalid_cursor). A CLIENT error: retrying
    it will fail the same way, and treating it as "no more rows" would end a
    walk early while claiming it was complete."""


class Cdr1ProjectionTooOld(Cdr1EventsError):
    """cdr1 answered, but without the fields #983 added.

    #983 is committed in cdr.pdhc and, until it is DEPLOYED, production cdr1
    returns points with no `observation_guid` and no paging fields. Building
    event rows from that would produce rows with an empty identity — rows
    that render, and that nothing can select or look up. Refused by name so
    the dashboard says "cdr1 needs #983 deployed" instead.
    """


#: The fields #983 added. Their absence means the deployed cdr1 predates it.
EVENTS_REQUIRED_POINT_FIELDS = ("observation_guid", "transaction_guid",
                                "ingest_raw_guid", "care_plan_guid")
EVENTS_REQUIRED_BODY_FIELDS = ("truncated", "next_cursor")



class Cdr1FilterUnsupported(Cdr1EventsError):
    """cdr1 did not confirm a filter it was asked to apply (the transaction
    filter): an older cdr1 IGNORES the parameter and answers with the
    unfiltered history, which must never be shown as the filtered one."""

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
    def _headers(self, org_guids: list[str], is_admin: bool,
                 reason: str | None = None) -> dict:
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
            # #575 (#212 re-home): an admin-override read is break-glass — carry
            # the attestation reason so CDR1 admits it and it lands in the audit.
            if reason:
                h["X-Admin-Read-Reason"] = reason
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
        reason: str | None = None,
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
            what="patient list", reason=reason,
        )
        if body is None:
            return []
        return parse_clinical_patients(body)

    def patient_summary(
        self, patient_guid: str, org_guids: list[str], *, is_admin: bool = False,
        reason: str | None = None,
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
            org_guids, is_admin, what="patient summary", reason=reason,
        )
        if body is None:
            return []
        params = body.get("parameters")
        return params if isinstance(params, list) else []

    def patient_concepts(
        self, patient_guid: str, org_guids: list[str], *, is_admin: bool = False,
        reason: str | None = None,
    ) -> list[dict]:
        """`patient_summary`, STRICT: raises by name instead of returning [].

        The filter bar's concept list. An empty list from the lenient method
        means "none" OR "cdr1 did not answer", and a filter offering no
        concepts because of an outage would read as "this patient has no
        data".
        """
        if not self.base_url:
            raise Cdr1NotConfigured("CDR1_BASE_URL is not set")
        if not is_admin and not org_guids:
            return []
        body = self._get_json_strict(
            f"/api/v1/clinical/patient/{patient_guid}/summary",
            org_guids, is_admin, reason=reason)
        params = body.get("parameters")
        if not isinstance(params, list):
            raise Cdr1Unreachable("cdr1 summary carried no parameters list")
        return params

    def patient_series(
        self, patient_guid: str, codes: list[str] | None,
        frm: str | None, to: str | None,
        org_guids: list[str], *, is_admin: bool = False,
        reason: str | None = None,
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
            reason=reason,
        )
        if body is None:
            return []
        pts = body.get("points")
        return pts if isinstance(pts, list) else []

    # -- events (strict) ----------------------------------------------------
    def patient_events(
        self, patient_guid: str, org_guids: list[str], *,
        is_admin: bool = False, cursor: str | None = None, limit: int = 100,
        codes: list[str] | None = None, reason: str | None = None,
        frm: str | None = None, to: str | None = None,
        transactions: list[str] | None = None,
    ) -> dict:
        """One page of a patient's observations, NEWEST FIRST (#983).

        Returns ``{points, next_cursor, truncated, limit, undated_count}``.
        `next_cursor` is cdr1's own opaque cursor, passed through untouched —
        the dashboard never constructs one, so the two ends cannot disagree
        about its encoding.

        RAISES rather than returning an empty page on failure; see
        `Cdr1EventsError`. An empty `points` list is an ANSWER.
        """
        if not self.base_url:
            raise Cdr1NotConfigured(
                "CDR1_BASE_URL is not set, so the event list cannot be read; "
                "the patient's events are UNKNOWN, which is not the same as "
                "absent")
        if not is_admin and not org_guids:
            # Rule 24: a non-admin with no affiliation sees nobody. The access
            # gate refuses such a caller before this point; this is the floor.
            return {"points": [], "next_cursor": None, "truncated": False,
                    "limit": limit, "undated_count": 0}

        params: list[tuple[str, str]] = [("order", "desc"),
                                         ("limit", str(int(limit)))]
        if cursor:
            params.append(("cursor", cursor))
        for c in (codes or []):
            params.append(("code", c))
        # `from`/`to` are VALIDATED by the caller: cdr1 parses them leniently
        # and IGNORES a value it cannot parse, so a typo would silently drop
        # the filter and return the unfiltered history as if filtered.
        if frm:
            params.append(("from", frm))
        if to:
            params.append(("to", to))
        for t in (transactions or []):
            params.append(("transaction", t))

        body = self._get_json_strict(
            f"/api/v1/clinical/patient/{patient_guid}/series",
            org_guids, is_admin, params=params, reason=reason)

        # A cdr1 that predates the transaction filter IGNORES the parameter
        # and answers with every observation. That must not reach a clinician
        # as "this plan's observations", so the filter is only trusted when
        # cdr1 says it applied it.
        if transactions and body.get("transaction_filter") is not True:
            raise Cdr1FilterUnsupported(
                "cdr1 did not confirm the transaction filter; it predates it "
                "and would return the unfiltered history")

        missing_body = [k for k in EVENTS_REQUIRED_BODY_FIELDS if k not in body]
        points = body.get("points")
        if not isinstance(points, list):
            raise Cdr1Unreachable("cdr1 events response carried no points list")
        missing_pt = sorted({k for p in points for k in EVENTS_REQUIRED_POINT_FIELDS
                             if k not in p})
        if missing_body or missing_pt:
            raise Cdr1ProjectionTooOld(
                "cdr1 answered without the fields #983 added "
                f"(body: {missing_body or 'ok'}; points: {missing_pt or 'ok'}); "
                "deploy #983 to cdr.pdhc")
        return {
            "points": points,
            "next_cursor": body.get("next_cursor"),
            "truncated": bool(body.get("truncated")),
            "limit": body.get("limit", limit),
            "undated_count": body.get("undated_count", 0),
        }

    def observation_detail(
        self, observation_guid: str, org_guids: list[str], *,
        is_admin: bool = False, reason: str | None = None,
    ) -> dict | None:
        """One observation and its provenance, or None if not in scope.

        None means cdr1 ANSWERED "no such observation in your scope" — which
        it says identically for "does not exist" and "another organisation's",
        so probing learns nothing. Every other failure raises by name.
        """
        if not self.base_url:
            raise Cdr1NotConfigured(
                "CDR1_BASE_URL is not set, so the observation cannot be read")
        if not is_admin and not org_guids:
            return None
        return self._get_json_strict(
            f"/api/v1/clinical/observation/{observation_guid}",
            org_guids, is_admin, reason=reason, allow_not_found=True)

    def _get_json_strict(
        self, path: str, org_guids: list[str], is_admin: bool, *,
        params: list[tuple[str, str]] | None = None,
        reason: str | None = None, allow_not_found: bool = False,
    ) -> dict | None:
        """Like `_get_json`, but every failure is a NAMED exception.

        `_get_json` collapses network errors, every 4xx, every 5xx and a
        non-JSON body into `None`, and its callers depend on that. Here cdr1's
        `400 invalid_cursor` would become "no data" and end a walk early, so
        this path keeps the distinctions.
        """
        url = f"{self.base_url}{path}"
        try:
            r = requests.get(
                url, headers=self._headers(org_guids, is_admin, reason),
                params=params or None, timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise Cdr1Unreachable(f"cdr1 network error: {type(exc).__name__}") from exc
        if r.status_code == 400:
            try:
                err = (r.json() or {}).get("error")
            except ValueError:
                err = None
            if err in ("invalid_cursor", "invalid_order"):
                raise Cdr1BadCursor(f"cdr1 refused the request: {err}")
            raise Cdr1Unreachable("cdr1 answered 400", status_code=400)
        if r.status_code == 404 and allow_not_found:
            # Two different 404s, and they must not be confused. cdr1's OWN
            # "no such observation in your scope" is JSON with
            # error=not_found — an answer. A route that does not exist yet is
            # Flask's default HTML 404 — which is what a cdr1 that predates
            # #983 returns for the detail route. Reading the second as the
            # first would make every detail view say "not found" until the
            # deploy, silently.
            try:
                err = (r.json() or {}).get("error")
            except ValueError:
                err = None
            if err == "not_found":
                return None
            raise Cdr1ProjectionTooOld(
                "cdr1 has no such route — it predates #983; deploy cdr.pdhc")
        if r.status_code >= 400:
            raise Cdr1Unreachable(f"cdr1 answered {r.status_code}",
                                  status_code=r.status_code)
        try:
            body = r.json()
        except ValueError as exc:
            raise Cdr1Unreachable("cdr1 answered with a non-JSON body") from exc
        if not isinstance(body, dict):
            raise Cdr1Unreachable("cdr1 answered with a non-object body")
        return body

    # -- transport ------------------------------------------------------
    def _get_json(
        self, path: str, org_guids: list[str], is_admin: bool, *, what: str,
        params: list[tuple[str, str]] | None = None,
        reason: str | None = None,
    ) -> dict | None:
        url = f"{self.base_url}{path}"
        try:
            r = requests.get(
                url, headers=self._headers(org_guids, is_admin, reason),
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
