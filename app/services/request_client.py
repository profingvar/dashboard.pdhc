"""Read-only client for request.pdhc — CarePlans, plan items, frozen SRs.

Fills `dashboard.dto.PlanSummary` / `dto.PlanItem` for the plan panel, and
looks up one frozen ServiceRequest by guid (the coding plan's fallback for
event provenance). Nothing here writes: every method is a GET.

MEASURED against request.pdhc's source on 2026-11-11, not assumed. Where the
brief and the code disagree the code won, and the disagreement is recorded
here rather than smoothed over.

Endpoints (all under `/api/v1` — `gateway/app/__init__.py` registers
`care_plans_bp`, `service_requests_bp` and `requests_bp` with that prefix, so
the bare `/careplans` in the plan is a route, not a URL):

    GET /api/v1/careplans?patient_guid=      -> {total, careplans: [...]}
    GET /api/v1/careplans/<guid>             -> CarePlan.to_dict()
    GET /api/v1/careplans/<guid>/context     -> flattened snapshot transactions
    GET /api/v1/ServiceRequest               -> {items, total, page, per_page}
    GET /api/v1/ServiceRequest/<guid>        -> ServiceRequest.to_dict()
    GET /api/v1/ServiceRequest/<guid>/matches

FOUR THINGS THE SOURCE SAYS THAT THE PLAN DOES NOT
--------------------------------------------------

1.  **`ServiceRequest.care_plan_guid` IS NEVER SERIALISED, AND NEVER
    WRITTEN.** The column exists (`service_request_models.py:28`, "since
    #310") but `ServiceRequest.to_dict()` omits it and
    `create_service_request` never sets it — a platform-wide grep for
    `care_plan_guid` outside tests returns four hits, three of them
    comments and one of them the CarePlan's own alias. So there is NO
    API-visible edge from a ServiceRequest to a CarePlan. This client
    therefore links the two the only way the wire permits: by
    `patient_guid` plus the transaction guids inside each SR's own
    `plan_definition_snapshot`, which live in the same key space as the
    CarePlan context's `transaction_guid` (both read
    `activities[].transactions[].guid`).

2.  **`GET /ServiceRequest` HAS NO `patient_guid` FILTER.**
    `service_requests.list_all` forwards only `status`, `page` and
    `per_page`; the org scope comes from the caller's own blob. So the
    patient filter here is client-side over a page, and a plan whose SRs
    were ordered by an organisation the caller is not in is invisible to
    this client — by request.pdhc's design, not by omission here.

3.  **THERE IS NO `completed` ServiceRequest STATUS.** The measured set is
    `draft` -> `active` -> `archived`, plus `revoked`. Completion is
    expressed by `completion_service.archive_if_provider_work_complete`,
    which archives an `active` SR once EVERY `ServiceRequestContractMatch`
    is terminal. `completed` is a *match* status, not an SR status. Both
    are accepted below so the mapping survives either.

4.  **THE CarePlan ROUTES CARRY NO ORG SCOPE AND NO SPÄRR.**
    `get_care_plan` / `list_care_plans` filter on guid or `patient_guid`
    and nothing else, while the ServiceRequest routes run
    `ips_client.is_sr_visible` and an org filter. Noted because a reader
    of this client could otherwise assume the plan panel inherits Rule 24
    from upstream. It does not; scoping is the caller's job here.

AUTH — `X-API-Key`, CARRYING THE OPERATOR'S OWN BEARER
------------------------------------------------------
request.pdhc does **not** accept `X-Service-Key` on any of these routes:
`requires_auth` tries `X-Provider-Token`, then `X-API-Key`, then a Flask
*session cookie*. `requires_service_key` exists but guards only the
`/internal/*` blueprint, and it compares against `INTERNAL_SERVICE_KEY` —
a secret dashboard does not hold.

Note also what the session branch means: `get_current_access_blob` reads
`session['sso_token']`, NOT an `Authorization` header. So an outbound
`Authorization: Bearer ...` from here would authenticate NOTHING. The door
is `X-API-Key`, whose value `validate_api_key` forwards to sso as a bearer —
i.e. the operator's own SSO token. That is the same wire onboard.pdhc uses
to reach request.pdhc, and it is why `/internal/service-request/<g>/context`
is deliberately NOT exposed by this client: dashboard cannot authenticate to
it, and a method that always 401s is worse than an absent one.

Every read here is audited on the request.pdhc side (`@audit_read` on the SR
routes), under the operator's identity.

FAILURE MODE — DELIBERATELY NOT ips_client's
--------------------------------------------
`ips_client.fetch_active_blocks` degrades to `[]` because a missing spärr
answer still leaves cdr1's own filter in place. Nothing here has a second
layer: an empty plan panel over a failed call would assert "this patient has
no care plan", which is a clinical claim nobody made. So this client RAISES,
and raises two distinct types — `RequestNotConfigured` and
`RequestUnreachable` — so a caller can say "not wired up" and "did not
answer" separately. An empty list from here always means request.pdhc
answered and there is nothing.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

import requests
from flask import current_app

from app.dashboard.dto import PlanItem, PlanSummary

#: The one environment variable. There is deliberately NO `DEFAULT_BASE_URL`
#: beside it: xlate's module-level `DEFAULT_BASE_URL = "http://127.0.0.1:9017"`
#: manufactured a configured-looking URL out of an unset variable, so "nobody
#: wired this up" became indistinguishable from "it blipped" (#833). Unset
#: reads as unset here, and the refusal names the key.
ENV_BASE_URL = "REQUEST_BASE_URL"

DEFAULT_TIMEOUT = 8.0

#: Soft cap request.pdhc itself applies (`min(per_page, 200)`).
MAX_PER_PAGE = 200


# ---------------------------------------------------------------------------
# Errors — two types, not one flag
# ---------------------------------------------------------------------------

class RequestClientError(Exception):
    """Base for every failure of a request.pdhc read."""


class RequestNotConfigured(RequestClientError):
    """`REQUEST_BASE_URL` is unset, so there is nowhere to ask.

    A deployment fact, not a transient one: retrying cannot help, and the
    message names the key an operator must set. Distinct from
    `RequestUnreachable` because the operator action is different — set a
    variable, versus look at why a service did not answer.
    """


class RequestUnreachable(RequestClientError):
    """request.pdhc produced no answer this client may trust.

    Covers a network error, a timeout, a non-2xx status (401/403 included —
    an authenticated refusal is an answer, but not an answer about data),
    and a body that is not JSON. `status_code` is set when there was one.

    It is an exception rather than an empty return BECAUSE an empty plan
    panel is a clinical assertion. See the module docstring.
    """

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def resolve_base_url() -> str:
    """Where request.pdhc is, by the ONE rule every caller must agree on.

    Config first (so a test or `create_app(config=...)` can override),
    environment second — `ips_client.resolve_base_url`'s pattern verbatim,
    and for its reason: `IpsClient.__init__` read `os.environ` while
    `is_configured()` read `current_app.config`, and nothing loaded the
    variable into the config, so setting `IPS_BASE_URL` made the client work
    and left the question answering False forever (#827).

    The environment half is load-bearing here and not merely a fallback:
    `create_app` loads `CDR1_BASE_URL`, `IPS_BASE_URL`, `GATEWAY_BASE_URL`
    and `SSO_BASE_URL` into the config and does not load this one. Reading
    both is what keeps the #827 shape from recurring while that stays true.

    Returns `""` when unset — never a fabricated URL, and never an
    exception, because this is called from `is_configured()` outside a
    request context.
    """
    try:
        from_config = (current_app.config.get(ENV_BASE_URL) or "").strip()
    except RuntimeError:  # no application context
        from_config = ""
    return (from_config or os.environ.get(ENV_BASE_URL, "")).strip()


def is_configured() -> bool:
    """Can request.pdhc be asked at all?

    Separate from "does this patient have a plan", and a caller that wants
    to render "we could not find out" rather than "no plan" asks this. Safe
    outside an application context.
    """
    return bool(resolve_base_url())


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

def _parse_iso(value: Any) -> datetime | None:
    """ISO-8601 string -> tz-aware UTC datetime, or None if unparseable.

    Both shapes occur on the wire and must compare: `CarePlan.period_start`
    is `DateTime(timezone=True)` so it serialises with an offset, while
    `ServiceRequest.period_start` is a NAIVE `db.DateTime` and serialises
    without one. A naive value is read as UTC — request.pdhc stores
    `datetime.now(timezone.utc)` into those columns, so that is what it is,
    not an assumption about the reader's clock.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Derived state
# ---------------------------------------------------------------------------

#: A ServiceRequest the clinician stopped. `revoked` is request.pdhc's own
#: terminal-refusal status (`service_request_service.revoke_service_request`);
#: `entered-in-error` is the FHIR R5 CarePlan value, accepted here so a
#: plan-level status maps through the same function.
CANCELLED_STATUSES = frozenset({"revoked", "entered-in-error"})

#: A ServiceRequest whose work is finished. `archived` is what request.pdhc
#: actually writes — `completion_service.archive_if_provider_work_complete`
#: archives an active SR once EVERY contract match is terminal, so `archived`
#: IS the completion signal. `completed` is accepted because it is the match
#: status and the FHIR CarePlan status, and a future SR status would plausibly
#: use the same word.
DONE_STATUSES = frozenset({"archived", "completed"})


def derive_item_state(
    service_request_status: str | None,
    scheduled_at: str | None = None,
    *,
    now: datetime | None = None,
) -> str:
    """Map a ServiceRequest status (+ its schedule) onto a `dto.PlanItemState`.

    WHY "RESULT" IS A STATUS AND NOT AN OBSERVATION. "Overdue" means a
    scheduled time has passed with no result. The tempting reading of
    "result" is "an observation exists for this plan item" — and on this
    platform that reading marks EVERY item overdue: `care_plan_guid` is
    populated on 0 of 1 949 observations on cdr1, so no observation can be
    attributed to a plan item at all. A rule whose input is uniformly NULL
    does not produce "unknown", it produces a confident wrong answer on
    every row, which is worse than no column.

    So the result side of the test is the ServiceRequest/transaction STATUS
    and nothing else. This function never looks at an observation, and it is
    pure — it takes no client and opens no socket — so the rule can be read
    and tested without the wire.

    The schedule side is still a time: `overdue` requires both a scheduled
    time in the past AND a status that is not already terminal. An item with
    no scheduled time is `planned` however old it is, because "late" is
    undefined without a due date, and inventing one from `created_at` is the
    same falsification the null `period_start` rule forbids elsewhere.
    """
    status = (service_request_status or "").strip().lower()
    if status in CANCELLED_STATUSES:
        return "cancelled"
    if status in DONE_STATUSES:
        return "done"
    due = _parse_iso(scheduled_at)
    if due is not None:
        reference = now or datetime.now(timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        if due < reference:
            # Not terminal and past due. Nothing about an observation was
            # consulted to get here, deliberately.
            return "overdue"
    return "planned"


# ---------------------------------------------------------------------------
# Pure assembly
# ---------------------------------------------------------------------------

def service_request_transaction_guids(service_request: Mapping) -> set[str]:
    """The transaction guids frozen into one SR's PlanDefinition snapshot.

    This is the join key finding 1 in the module docstring forces on us:
    `care_plan_guid` never reaches the wire, so an SR is matched to a plan
    item by the transaction guids it froze. Both sides read
    `activities[].transactions[].guid` — `context_service._extract_transactions`
    for the SR and `care_plans.care_plan_context` for the plan — so they are
    the same key space by construction, not by coincidence.
    """
    out: set[str] = set()
    snapshot = service_request.get("plan_definition_snapshot") or {}
    if not isinstance(snapshot, Mapping):
        return out
    for activity in (snapshot.get("activities") or []):
        if not isinstance(activity, Mapping):
            continue
        for tx in (activity.get("transactions") or []):
            if isinstance(tx, Mapping) and tx.get("guid"):
                out.add(str(tx["guid"]))
    return out


def _performed_at(matches: Sequence[Mapping] | None) -> str | None:
    """The latest provider response time among finished contract matches.

    request.pdhc records no "performed" timestamp on a transaction. The
    nearest MEASURED fact is `ServiceRequestContractMatch.response_at` —
    when the provider answered — so that is what this returns, and only for
    a match that actually finished (`completed`/`accepted`).

    None when nothing finished, or when matches were not fetched. It is
    never taken from `updated_at`, which dates the row's last edit and not
    the care; that substitution is the same falsification as filling in a
    null `period_start`.
    """
    best: str | None = None
    best_dt: datetime | None = None
    for m in (matches or []):
        if not isinstance(m, Mapping):
            continue
        if (m.get("status") or "").strip().lower() not in ("completed", "accepted"):
            continue
        raw = m.get("response_at")
        dt = _parse_iso(raw)
        if dt is None:
            continue
        if best_dt is None or dt > best_dt:
            best_dt, best = dt, str(raw)
    return best


def _plan_definition_meta(care_plan: Mapping) -> tuple[str | None, str | None]:
    """(name, version) out of the CarePlan's frozen PlanDefinition snapshot.

    `title or name` mirrors request.pdhc's own read of the same snapshot
    (`routes/service_requests.py:454`); plan.pdhc's PlanDefinition carries
    `title` (NOT NULL), `name` (nullable) and `version`.
    """
    snapshot = care_plan.get("plan_definition_snapshot") or {}
    if not isinstance(snapshot, Mapping):
        return None, None
    name = snapshot.get("title") or snapshot.get("name") or None
    version = snapshot.get("version") or None
    return (str(name) if name else None,
            str(version) if version else None)


def build_plan_summary(
    care_plan: Mapping,
    context: Mapping | None = None,
    service_requests: Iterable[Mapping] = (),
    *,
    matches_by_sr: Mapping[str, Sequence[Mapping]] | None = None,
    now: datetime | None = None,
) -> PlanSummary:
    """Assemble one `dto.PlanSummary` from the three reads that feed it.

    Pure: no network, no app context. `care_plan` is a `CarePlan.to_dict()`,
    `context` the `/careplans/<guid>/context` body (its `transactions` are
    the items), `service_requests` any SRs that might belong to this plan —
    they are filtered here by `patient_guid` and by transaction guid, since
    the wire carries no `care_plan_guid` on an SR.

    `period_start` IS PASSED THROUGH UNCHANGED, None INCLUDED. It is
    nullable in request.pdhc's own schema
    (`care_plan_models.py:54`, `nullable=True`) and it is the origin for
    every "T+ from start" time in the dashboard. Substituting `created_at`,
    or the plan's first observation, or `period_end`, would silently move
    every event on a clinical timeline — so there is no fallback chain on
    that line and there must not be one. `dto.PlanSummary` says the same
    thing from the other side: None means there is no T+ origin and every
    `t_from_start_seconds` under this plan is None.
    """
    patient_guid = care_plan.get("patient_guid")
    plan_definition_guid = care_plan.get("plan_definition_guid")

    # Index the candidate SRs by the transaction guids they froze, keeping
    # only those for this patient (the wire gives us no better edge).
    by_tx: dict[str, Mapping] = {}
    plan_wide: list[Mapping] = []
    for sr in service_requests:
        if not isinstance(sr, Mapping):
            continue
        if patient_guid and sr.get("patient_guid") != patient_guid:
            continue
        tx_guids = service_request_transaction_guids(sr)
        if tx_guids:
            for g in tx_guids:
                by_tx.setdefault(g, sr)
        elif plan_definition_guid and sr.get("plan_definition_guid") == plan_definition_guid:
            # A snapshot that froze no transaction guids (a lossy or
            # pre-enrichment capture) still belongs to this plan by
            # patient + PlanDefinition. Used only as the weaker fallback,
            # so a precise transaction match always wins.
            plan_wide.append(sr)

    items: list[PlanItem] = []
    for tx in ((context or {}).get("transactions") or []):
        if not isinstance(tx, Mapping):
            continue
        tx_guid = str(tx["transaction_guid"]) if tx.get("transaction_guid") else None
        sr = by_tx.get(tx_guid) if tx_guid else None
        if sr is None and plan_wide:
            sr = plan_wide[0]
        sr_guid = sr.get("guid") if sr else None
        sr_status = sr.get("status") if sr else None
        # The SR's validity period is the only scheduled time on the wire;
        # a transaction in a snapshot carries cadence, not a date.
        scheduled_at = sr.get("period_start") if sr else None
        matches = (matches_by_sr or {}).get(str(sr_guid)) if sr_guid else None
        items.append(PlanItem(
            transaction_guid=tx_guid,
            title=(tx.get("concept_name") or tx.get("concept_guid") or "—"),
            state=derive_item_state(sr_status, scheduled_at, now=now),
            concept_guid=tx.get("concept_guid"),
            scheduled_at=scheduled_at,
            performed_at=_performed_at(matches),
            service_request_guid=sr_guid,
            # The VISIT (#820) — `activity_guid` in the context body.
            plan_activity_guid=tx.get("activity_guid"),
        ))

    pd_name, pd_version = _plan_definition_meta(care_plan)
    return PlanSummary(
        care_plan_guid=str(care_plan.get("care_plan_guid")
                           or care_plan.get("guid") or ""),
        care_plan_title=care_plan.get("title"),
        plan_definition_name=pd_name,
        plan_definition_version=pd_version,
        # No fallback. See the docstring.
        period_start=care_plan.get("period_start"),
        period_end=care_plan.get("period_end"),
        status=care_plan.get("status"),
        items=tuple(items),
    )


def build_order_summary(
    service_request: Mapping,
    *,
    matches: Sequence[Mapping] | None = None,
    now: datetime | None = None,
) -> PlanSummary:
    """One ServiceRequest as a `dto.PlanSummary` — the order IS the plan.

    MEASURED 2026-11-12, production: `care_plans` holds 0 rows; 28
    ServiceRequests (23 patients) each froze a PlanDefinition, and cdr1's
    `observation.transaction_guid` matches those snapshots'
    `activities[].transactions[].guid` for 1 949 of 1 949 rows. So a plan
    panel built only from CarePlans is empty for every patient, while the
    snapshot links cleanly. This reads the snapshot.

    Every item takes the SR's status: request.pdhc tracks completion per
    ServiceRequest (archive once every contract match is terminal), not per
    transaction, so a per-item state would be invented. `period_start` is
    None on all 28 measured SRs, so no item can be `overdue` today — which
    is a true statement about the data, not a defect here.
    """
    snapshot = service_request.get("plan_definition_snapshot") or {}
    if not isinstance(snapshot, Mapping):
        snapshot = {}
    sr_guid = service_request.get("guid")
    status = service_request.get("status")
    scheduled_at = service_request.get("period_start")
    performed_at = _performed_at(matches)
    items: list[PlanItem] = []
    for activity in (snapshot.get("activities") or []):
        if not isinstance(activity, Mapping):
            continue
        visit = activity.get("guid") or activity.get("activity_guid")
        for tx in (activity.get("transactions") or []):
            if not isinstance(tx, Mapping) or not tx.get("guid"):
                continue
            items.append(PlanItem(
                transaction_guid=str(tx["guid"]),
                title=(tx.get("concept_name") or tx.get("concept_guid") or "—"),
                state=derive_item_state(status, scheduled_at, now=now),
                concept_guid=tx.get("concept_guid"),
                scheduled_at=scheduled_at,
                performed_at=performed_at,
                service_request_guid=str(sr_guid) if sr_guid else None,
                plan_activity_guid=str(visit) if visit else None,
            ))
    pd_name, pd_version = _plan_definition_meta(service_request)
    return PlanSummary(
        care_plan_guid=None,
        care_plan_title=snapshot.get("title") or None,
        plan_definition_name=pd_name,
        plan_definition_version=pd_version,
        period_start=scheduled_at,
        period_end=service_request.get("period_end"),
        status=status,
        items=tuple(items),
        source="service_request",
        service_request_guid=str(sr_guid) if sr_guid else None,
        ordered_at=service_request.get("created_at"),
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class RequestClient:
    """Read-only HTTP client for request.pdhc. Every method is a GET.

    `token` is the operator's SSO bearer, sent as `X-API-Key` — see the
    module docstring for why that header and not `Authorization` or
    `X-Service-Key`.
    """

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.base_url = (base_url or resolve_base_url()).rstrip("/")
        self.token = token
        self.timeout = timeout

    # -- headers ---------------------------------------------------------
    def _headers(self) -> dict:
        """Accept + the operator's credential + X2 session propagation.

        No `X-Service-Key`: request.pdhc's `requires_auth` never reads it,
        and `requires_service_key` guards only `/internal/*` against
        `INTERNAL_SERVICE_KEY`, which dashboard does not hold. Sending one
        would look like a credential and authenticate nothing — the mistake
        three services made independently against ips (#783).
        """
        from app.services.session_headers import outbound_session_headers
        h = {"Accept": "application/json"}
        if self.token:
            h["X-API-Key"] = self.token
        h.update(outbound_session_headers())
        return h

    # -- transport -------------------------------------------------------
    def _get(
        self,
        path: str,
        *,
        what: str,
        params: Any = None,
        item: bool = False,
    ) -> Any:
        """GET and return the parsed body, or raise.

        `item=True` marks a single-resource path, where a 404 is an ANSWER
        ("no such CarePlan") and returns None. On a LIST path a 404 is not
        emptiness — it is a route that does not exist, which a reverse-proxy
        or prefix mistake produces — so it raises. That asymmetry is what
        keeps "empty list" meaning "request.pdhc answered and there is
        nothing", which is the contract.
        """
        if not self.base_url:
            raise RequestNotConfigured(
                f"{ENV_BASE_URL} is not set, so request.pdhc cannot be asked "
                f"for {what}. Set {ENV_BASE_URL} to the public subdomain "
                "(https://request.pdhc.se) in this service's .env; there is "
                "deliberately no default to fall back to."
            )
        url = f"{self.base_url}{path}"
        try:
            r = requests.get(
                url, params=params or None,
                headers=self._headers(), timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise RequestUnreachable(
                f"request.pdhc {what} failed (network): {e}") from e
        if item and r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise RequestUnreachable(
                f"request.pdhc {what} returned {r.status_code}",
                status_code=r.status_code,
            )
        try:
            return r.json()
        except ValueError as e:
            raise RequestUnreachable(
                f"request.pdhc {what} returned a non-JSON body",
                status_code=r.status_code,
            ) from e

    # -- CarePlans -------------------------------------------------------
    def fetch_care_plans(
        self,
        patient_guid: str,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """`GET /api/v1/careplans?patient_guid=` -> the patient's CarePlans.

        `[]` means request.pdhc answered and this patient has none.
        """
        params = {"patient_guid": patient_guid, "limit": limit, "offset": offset}
        if status:
            params["status"] = status
        body = self._get("/api/v1/careplans", what="careplan list", params=params)
        rows = (body or {}).get("careplans")
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def fetch_care_plan(self, care_plan_guid: str) -> dict | None:
        """`GET /api/v1/careplans/<guid>`. None when there is no such plan."""
        body = self._get(f"/api/v1/careplans/{care_plan_guid}",
                         what="careplan read", item=True)
        return body if isinstance(body, dict) else None

    def fetch_care_plan_context(self, care_plan_guid: str) -> dict | None:
        """`GET /api/v1/careplans/<guid>/context` — the plan's items.

        Its `transactions` are the flattened snapshot rows
        (`activity_guid`, `transaction_guid`, `concept_guid`,
        `concept_name`, `range_min`, `range_max`, `requirement_type`). Note
        what is NOT there: no status and no per-transaction date. That is
        why `derive_item_state` takes its status from a ServiceRequest.
        """
        body = self._get(f"/api/v1/careplans/{care_plan_guid}/context",
                         what="careplan context", item=True)
        return body if isinstance(body, dict) else None

    # -- ServiceRequests -------------------------------------------------
    def fetch_service_request(self, service_request_guid: str) -> dict | None:
        """`GET /api/v1/ServiceRequest/<guid>` — ONE frozen SR, for event
        provenance. None when request.pdhc says there is no such SR.

        Beware the 404 here: `get_service_request` returns 404 both for
        "unknown guid" and for "the patient has blocked the requesting
        organisation" (`ips_client.is_sr_visible`, PDL Ch 4 §4 / #228).
        Deliberate upstream — the row's existence is itself the disclosure —
        so None means "not available to you", not strictly "absent".
        """
        body = self._get(f"/api/v1/ServiceRequest/{service_request_guid}",
                         what="service request read", item=True)
        return body if isinstance(body, dict) else None

    def fetch_service_requests(
        self,
        *,
        patient_guid: str | None = None,
        status: str | None = None,
        page: int = 1,
        per_page: int = MAX_PER_PAGE,
    ) -> list[dict]:
        """`GET /api/v1/ServiceRequest` — one org-scoped page.

        `patient_guid` filters CLIENT-SIDE: `list_all` accepts no such
        parameter (finding 2 in the module docstring). The page is also
        org-scoped upstream for a non-SU caller and spärr-filtered, so it
        can be shorter than `total` — which is the correct privacy
        behaviour there, and a reason not to read `total` as a count of
        what you can see.
        """
        params = {"page": page, "per_page": min(per_page, MAX_PER_PAGE)}
        if status:
            params["status"] = status
        body = self._get("/api/v1/ServiceRequest",
                         what="service request list", params=params)
        rows = (body or {}).get("items")
        rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
        if patient_guid:
            rows = [r for r in rows if r.get("patient_guid") == patient_guid]
        return rows

    def fetch_service_request_matches(self, service_request_guid: str) -> list[dict]:
        """`GET /api/v1/ServiceRequest/<guid>/matches` — contract matches.

        The only source of a provider response time, which is the nearest
        thing request.pdhc has to "performed" (see `_performed_at`).
        """
        body = self._get(
            f"/api/v1/ServiceRequest/{service_request_guid}/matches",
            what="service request matches", item=True,
        )
        if isinstance(body, dict):
            rows = body.get("matches")
            if rows is None:
                rows = body.get("items")
        else:
            rows = body
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    # -- assembled -------------------------------------------------------
    def plan_summaries(
        self,
        patient_guid: str,
        *,
        now: datetime | None = None,
        include_performed: bool = True,
    ) -> tuple[PlanSummary, ...]:
        """Every CarePlan for a patient, as `dto.PlanSummary` with items.

        Three reads per plan (list, context, SR page) plus one `/matches`
        per linked SR when `include_performed`. Pass `include_performed=
        False` to skip those — `performed_at` is then None on every item,
        which `dto`'s rule reads as "not recorded", so only do it where the
        caller does not show that column.

        Raises rather than returning `()` on a failure: an empty plan panel
        would assert the patient has no care plan.
        """
        plans = self.fetch_care_plans(patient_guid)
        service_requests = self.fetch_service_requests(patient_guid=patient_guid)
        if not plans and not service_requests:
            return ()

        matches_by_sr: dict[str, Sequence[Mapping]] = {}
        if include_performed:
            for sr in service_requests:
                guid = sr.get("guid")
                if guid:
                    matches_by_sr[str(guid)] = self.fetch_service_request_matches(str(guid))

        out: list[PlanSummary] = []
        for plan in plans:
            guid = plan.get("care_plan_guid") or plan.get("guid")
            context = self.fetch_care_plan_context(str(guid)) if guid else None
            out.append(build_plan_summary(
                plan, context, service_requests,
                matches_by_sr=matches_by_sr, now=now,
            ))
        # An SR no CarePlan item claimed is a plan of its own. On production
        # that is EVERY SR, because there are no CarePlans (2026-11-12).
        claimed = {i.service_request_guid for p in out for i in p.items
                   if i.service_request_guid}
        for sr in service_requests:
            guid = sr.get("guid")
            if guid and str(guid) not in claimed:
                out.append(build_order_summary(
                    sr, matches=matches_by_sr.get(str(guid)), now=now))
        return tuple(out)


def build_client() -> RequestClient:
    """Construct a client from app config + the request-context SSO token.

    Factory so route code stays terse and tests can monkeypatch it — the
    `cdr1_client.build_client` / `ips_client._default_client` pattern.

    The token is the operator's own, and there is no service-identity
    fallback because request.pdhc offers no door for one: `KNOWN_SERVICES`
    does not exist in that repo and `INTERNAL_SERVICE_KEY` guards only
    `/internal/*`. So these reads are session-only by construction. Outside
    a request context the client is built WITHOUT a credential and the call
    will be refused upstream — which is the honest outcome, rather than
    inventing a key that authenticates nothing.
    """
    from flask import session
    token = None
    try:
        token = session.get("sso_token")
    except RuntimeError:  # no request context
        token = None
    return RequestClient(
        base_url=resolve_base_url() or None,
        token=token,
    )
