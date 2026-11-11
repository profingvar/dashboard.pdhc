"""Read-only client for plan.pdhc — PlanDefinition identity + concept names.

Two questions the dashboard BFF has to ask plan.pdhc, and nothing else:

1.  **Which PlanDefinition is this?** ``GET /api/v1/PlanDefinition/<fhir_id>``
    and the searchset ``GET /api/v1/PlanDefinition``. The dashboard needs
    ``name`` and ``version`` for ``dto.PlanSummary.plan_definition_name`` /
    ``plan_definition_version``; ``PlanDefinitionRef`` carries exactly those
    (plus title/status, which are free on the same resource) and nothing more.

2.  **What is this concept CALLED?** ``GET /api/v1/concepts`` (the registry)
    and ``GET /api/v1/concepts/<guid>`` (one row). The events list resolves
    hundreds of ``code_canonical`` values to display names, so the lookup is
    BATCHED: ``concept_display_names()`` pulls the registry ONE page at a time
    (plan caps ``per_page`` at 200) and serves every guid from that one index,
    behind a TTL cache. A request-per-row is an explicit failure in the
    coding plan, and the shape here is the one `request.pdhc`'s
    ``context_service`` already uses against the same endpoint.

THE READS ARE UNAUTHENTICATED, BY NECESSITY — ticket #931
-----------------------------------------------------------
plan.pdhc has no usable machine-auth door: its ``KNOWN_SERVICES`` holds only
``sim.pdhc`` and ``SIM_PDHC_SERVICE_KEY`` is unset, so that dict admits
nobody. ``outbound_session_headers()`` carries ``X-Operator-Session-Id``
only — that is session propagation (#408) for PDL kontroller, **not**
authentication; do not read it as a credential. So this client sends no
bearer, no service key and no API key, exactly as `cdr.pdhc`'s
``plan_client`` and `request.pdhc`'s ``context_service`` already read plan.
``GET /concepts`` and ``GET /PlanDefinition`` answer 200 without credentials
today (#925 gated two lookup TABLES; it did not gate these). Inventing a
credential here would be inventing a door — when #931 gives plan one, this
is the single place to add it.

TWO EXCEPTION TYPES, NOT ONE FLAG
---------------------------------
The coding plan requires one failing upstream to degrade its own panel and
nothing else, which means a caller has to be able to tell *why* the panel is
empty:

  ``PlanNotConfigured``  — ``PLAN_BASE_URL`` is unset. A DEPLOYMENT fact.
  ``PlanUnreachable``    — plan.pdhc was asked and did not answer usefully.

Neither is ever reported as ``[]`` or ``{}``. An empty result from this
module means *plan.pdhc answered and there is nothing* — the dto module's own
rule ("a value that was never recorded and a value that could not be fetched
are DIFFERENT"), applied at the transport. ``ips_client`` deliberately
degrades to ``[]`` instead, because a missing spärr answer must not take a
chart page down; a missing concept NAME is cosmetic and a missing
PlanDefinition is one panel, so here the caller is told.

MEASURED RESPONSE SHAPES (plan.pdhc, read 2026-11-11 from its source)
---------------------------------------------------------------------
* ``/api/v1/concepts`` is a PAGE, not a list: ``{items, total, page,
  per_page}``, ``per_page`` server-capped at 200 and defaulting to 50.
* It has **no guid filter** — the only filters are ``search``,
  ``concept_type``, ``response_type``, ``canonical_lib``, ``has_values``. So
  "batched by guid" can only mean "fetch the index once", which is what this
  does.
* A concept's display name is ``concept_display_text`` falling back to
  ``concept_name`` (``concept_display_text`` is nullable).
* A concept row carries BOTH ``guid`` and ``concept_guid`` (#294 D3 emits the
  generic key during a transition that will drop ``guid``), so the index
  reads either.
* ``/api/v1/PlanDefinition`` is a FHIR searchset Bundle (``entry[].resource``)
  paged by ``_count`` (capped 200, default 20) and ``_offset`` — NOT by
  ``page``/``per_page`` like ``/concepts``.
* A missing resource is ``404`` with ``{"error": "Not found"}``, not an empty
  bundle.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

import requests
from flask import current_app


DEFAULT_TIMEOUT = 6.0

#: plan.pdhc caps `per_page` at 200 server-side (`api/concepts.py`). Asking
#: for more is silently reduced, so page at the cap: a smaller page would
#: turn one request into several for no reason.
CONCEPT_PAGE_SIZE = 200

#: Backstop on the paging loop, mirroring request.pdhc's: a 10k-concept
#: registry is paged in full, anything beyond that is a bug upstream and must
#: not become an unbounded loop in a web request.
MAX_CONCEPT_PAGES = 50

#: `_count` cap on the FHIR searchset (`api/fhir_plandefinitions.py`).
PLANDEF_PAGE_SIZE = 200

#: Concept names change when a human edits the registry, which is rare. Five
#: minutes keeps a long dashboard session to one index fetch while still
#: picking up a rename without a restart.
DEFAULT_TTL_SECONDS = 300.0


class PlanNotConfigured(Exception):
    """``PLAN_BASE_URL`` is not set, so plan.pdhc cannot be asked at all.

    Distinct from `PlanUnreachable` on purpose, and the distinction is the
    whole point of having two: this one is a deployment omission an operator
    fixes with one line in `.env`, the other is a service that is down. A
    single boolean, or an empty list, makes them indistinguishable — which is
    exactly how every spärr lookup in this service came back "no blocks"
    without asking anyone (#827).
    """


class PlanUnreachable(Exception):
    """plan.pdhc was asked and did not give a usable answer.

    Network error, a non-2xx that is not a resource-level 404, or a body that
    is not JSON. Named the same as `cdr.pdhc`'s own plan client so the
    platform has one word for the condition.
    """


# ---------------------------------------------------------------------------
# PlanDefinition identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanDefinitionRef:
    """The identity fields of a PlanDefinition, and deliberately no more.

    `dto.PlanSummary` wants `plan_definition_name` and
    `plan_definition_version`; `title`/`status` ride along on the same
    resource at no cost and are what a human reads. Everything else on a FHIR
    PlanDefinition (actions, goals, library) belongs to request.pdhc's
    CarePlan, not to this panel.
    """
    fhir_id: str
    name: str | None = None
    version: str | None = None
    title: str | None = None
    status: str | None = None

    @classmethod
    def from_resource(cls, resource: dict) -> "PlanDefinitionRef | None":
        """Parse one FHIR PlanDefinition resource. Pure — no network.

        Returns None for anything that is not a PlanDefinition with an id,
        so a mixed or malformed Bundle entry is skipped rather than
        fabricating a ref with an empty identity.
        """
        if not isinstance(resource, dict):
            return None
        if resource.get("resourceType") not in (None, "PlanDefinition"):
            return None
        fhir_id = resource.get("id")
        if not fhir_id:
            return None
        return cls(
            fhir_id=str(fhir_id),
            name=resource.get("name"),
            version=resource.get("version"),
            title=resource.get("title"),
            status=resource.get("status"),
        )

    def as_plan_summary_fields(self) -> dict:
        """The two `dto.PlanSummary` keyword arguments this ref supplies.

        Spelled here rather than at each call site so a rename in `dto.py` —
        which this module must not edit — breaks in one place.
        """
        return {
            "plan_definition_name": self.name,
            "plan_definition_version": self.version,
        }


def parse_plan_definition_bundle(bundle: dict) -> list[PlanDefinitionRef]:
    """FHIR searchset Bundle → ``[PlanDefinitionRef]``. Pure (no network).

    Same shape as `cdr1_client.parse_patient_bundle`, for the same reason:
    the parsing is where the bugs are and it should be testable without a
    socket.
    """
    out: list[PlanDefinitionRef] = []
    for entry in (bundle.get("entry") or []):
        if not isinstance(entry, dict):
            continue
        ref = PlanDefinitionRef.from_resource(entry.get("resource") or {})
        if ref is not None:
            out.append(ref)
    return out


# ---------------------------------------------------------------------------
# Concept display names
# ---------------------------------------------------------------------------


def display_name_of(concept: dict) -> str | None:
    """A concept row → the name to show a clinician. Pure (no network).

    `concept_display_text` is the curated label and is NULLABLE;
    `concept_name` is NOT NULL and is the fallback. Returning None rather
    than "" keeps "plan has no name for this" distinguishable from "plan
    named it the empty string".
    """
    if not isinstance(concept, dict):
        return None
    for key in ("concept_display_text", "concept_name"):
        val = concept.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _guid_of(concept: dict) -> str | None:
    """A concept row's guid, under either of the two keys plan emits.

    `to_dict()` carries `guid` AND `concept_guid` (#294 RFC D3) and says
    `guid` will be dropped after a release cycle. Reading both means that
    drop is not a silent outage here.
    """
    if not isinstance(concept, dict):
        return None
    for key in ("guid", "concept_guid"):
        val = concept.get(key)
        if val:
            return str(val)
    return None


# ---------------------------------------------------------------------------
# Base URL
# ---------------------------------------------------------------------------


def resolve_base_url() -> str:
    """Where plan.pdhc is — ONE resolution path, config then environment.

    The order and the single path are `ips_client.resolve_base_url()`'s
    verbatim (#827): two resolution paths for one deployment fact made
    `IPS_BASE_URL` work for the client and read as unset for the predicate,
    for months.

    Note `create_app()` does NOT currently load `PLAN_BASE_URL` into
    `app.config` (measured 2026-11-11: it loads `CDR1_BASE_URL`,
    `IPS_BASE_URL`, `GATEWAY_BASE_URL`, `SSO_BASE_URL` and not this one), and
    this module must not edit `app/__init__.py`. Reading the environment
    second is therefore not a nicety here — it is the only path production's
    `env_file: .env` has. Config still wins so a test or
    `create_app(config=...)` can point it elsewhere.

    **There is no module-level default, deliberately.** `xlate.pdhc`'s client
    carried `DEFAULT_BASE_URL = "http://127.0.0.1:9017"` and so manufactured
    a configured-looking URL out of an unset variable, making "nobody wired
    this up" indistinguishable from "it blipped" — reported as a retryable
    503 forever (#833). Unset reads as unset and refuses BY NAME.
    """
    try:
        from_config = (current_app.config.get("PLAN_BASE_URL") or "").strip()
    except RuntimeError:  # no application context
        from_config = ""
    return (from_config or os.environ.get("PLAN_BASE_URL", "")).strip()


def is_configured() -> bool:
    """Can plan.pdhc be asked at all?

    Separate from "did it have anything to say", so a panel can render "plan
    data unavailable — PLAN_BASE_URL is not set" without first provoking an
    exception. Never raises, including outside an app context.
    """
    return bool(resolve_base_url())


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class PlanClient:
    """Thin, read-only wrapper around plan.pdhc's open GET surface.

    Every method is a GET. There is no write path here and there must not be
    one: plan.pdhc is the platform's semantic authority and the dashboard is
    a reader of it.
    """

    def __init__(self, base_url: str | None = None,
                 timeout: float = DEFAULT_TIMEOUT):
        self.base_url = (base_url if base_url is not None
                         else resolve_base_url()).strip().rstrip("/")
        self.timeout = timeout

    # -- headers ---------------------------------------------------------
    def _headers(self) -> dict:
        """Accept + the operator session, and NO credential.

        plan.pdhc has no machine-auth door that admits this service (#931):
        `KNOWN_SERVICES` holds only `sim.pdhc`, whose key is unset. So these
        reads are unauthenticated BY NECESSITY, like cdr.pdhc's and
        request.pdhc's reads of the same endpoints. `X-Operator-Session-Id`
        below is session propagation (#408) so the read lands in the PDL
        kontroller chain — it authenticates nothing, and a future reader must
        not mistake it for a credential.
        """
        h = {"Accept": "application/json"}
        try:
            from app.services.session_headers import outbound_session_headers
            h.update(outbound_session_headers())
        except Exception:  # noqa: BLE001 — session headers are best-effort
            pass
        return h

    # -- transport -------------------------------------------------------
    def _require_base_url(self) -> str:
        if not self.base_url:
            raise PlanNotConfigured(
                "PLAN_BASE_URL is not configured, so plan.pdhc cannot be "
                "asked: PlanDefinition names/versions and concept display "
                "names are unavailable for this read. Set PLAN_BASE_URL in "
                "dashboard.pdhc's .env (shared/.env, which current/.env "
                "symlinks to).")
        return self.base_url

    def _get_json(self, path: str, *, what: str,
                  params: dict | None = None,
                  none_on_404: bool = False) -> dict | None:
        """GET and decode, or raise. Never returns ``{}`` for a failure.

        ``none_on_404`` is for the single-resource reads, where plan answers
        404 with ``{"error": "Not found"}``: that is a real answer ("no such
        resource") and the caller gets None, which is NOT the same as an
        empty collection. On a collection path a 404 means the route is not
        there — a misconfiguration, not an empty result — so it raises.
        """
        base = self._require_base_url()
        url = f"{base}{path}"
        try:
            r = requests.get(url, headers=self._headers(),
                             params=params or None, timeout=self.timeout)
        except requests.RequestException as e:
            raise PlanUnreachable(f"plan.pdhc {what} network error: {e}") from e
        if r.status_code == 404 and none_on_404:
            return None
        if r.status_code >= 400:
            raise PlanUnreachable(
                f"plan.pdhc {what} returned {r.status_code}")
        try:
            body = r.json()
        except ValueError as e:
            raise PlanUnreachable(
                f"plan.pdhc {what} returned a non-JSON body") from e
        return body if isinstance(body, dict) else {}

    # -- PlanDefinition --------------------------------------------------
    def get_plan_definition(self, fhir_id: str) -> PlanDefinitionRef | None:
        """``GET /api/v1/PlanDefinition/<fhir_id>``.

        Returns None when plan.pdhc says 404 — there is no such
        PlanDefinition — and raises `PlanUnreachable` when it could not be
        asked. Those are different and the panel says different things.
        """
        if not fhir_id:
            return None
        body = self._get_json(
            f"/api/v1/PlanDefinition/{fhir_id}",
            what=f"PlanDefinition read [{str(fhir_id)[:12]}]",
            none_on_404=True,
        )
        if body is None:
            return None
        return PlanDefinitionRef.from_resource(body)

    def list_plan_definitions(self, *, status: str | None = None,
                              count: int = PLANDEF_PAGE_SIZE,
                              offset: int = 0) -> list[PlanDefinitionRef]:
        """``GET /api/v1/PlanDefinition`` → the searchset, parsed.

        ``[]`` means plan.pdhc answered with an empty Bundle. A failure
        raises. Paged with FHIR's ``_count``/``_offset`` — note this endpoint
        does NOT take ``page``/``per_page`` the way ``/concepts`` does.
        """
        params: dict = {"_count": str(max(1, min(PLANDEF_PAGE_SIZE, count)))}
        if offset:
            params["_offset"] = str(offset)
        if status:
            params["status"] = status
        body = self._get_json("/api/v1/PlanDefinition",
                              what="PlanDefinition search", params=params)
        return parse_plan_definition_bundle(body or {})

    # -- concepts --------------------------------------------------------
    def get_concept(self, guid: str) -> dict | None:
        """``GET /api/v1/concepts/<guid>`` — the single row, verbatim.

        None on 404. This is the ONLY per-guid read in the module and it is
        not on the events path: resolving a page of rows goes through
        `fetch_concept_index` / `concept_display_names`.
        """
        if not guid:
            return None
        return self._get_json(
            f"/api/v1/concepts/{guid}",
            what=f"concept read [{str(guid)[:12]}]",
            none_on_404=True,
        )

    def fetch_concept_index(self) -> dict[str, str]:
        """The whole registry as ``{guid: display_name}`` — the BATCH read.

        ONE request while the registry fits in a page (``per_page`` capped at
        200 server-side), then one more per further page until ``total`` is
        consumed. Paging is not optional: a single page silently drops every
        concept past the cap, which is the bug request.pdhc's own comment on
        this endpoint warns about.

        ``{}`` means plan.pdhc answered and the registry is empty. A failure
        raises, so a caller never mistakes an outage for a registry of
        anonymous concepts.
        """
        names: dict[str, str] = {}
        page = 1
        seen = 0
        while page <= MAX_CONCEPT_PAGES:
            body = self._get_json(
                "/api/v1/concepts",
                what=f"concept index page {page}",
                params={"per_page": str(CONCEPT_PAGE_SIZE), "page": str(page)},
            ) or {}
            items = body.get("items")
            if not isinstance(items, list):
                items = []
            for row in items:
                guid = _guid_of(row)
                name = display_name_of(row)
                if guid and name:
                    names[guid] = name
            seen += len(items)
            total = body.get("total")
            if (len(items) < CONCEPT_PAGE_SIZE
                    or (isinstance(total, int) and seen >= total)):
                break
            page += 1
        return names


# ---------------------------------------------------------------------------
# Index cache
# ---------------------------------------------------------------------------


class _ConceptIndex:
    """Per-process TTL cache of the whole ``{guid: display_name}`` index.

    The whole index rather than per-guid entries, and that is the point: a
    per-guid cache misses on an unknown guid and would re-ask plan.pdhc for
    every unmapped row on the page — an N+1 wearing a cache. Here a guid
    plan.pdhc does not know is answered from a FRESH index without a request.

    Thread-safe. Each gunicorn worker warms its own copy; nothing is shared.
    """

    def __init__(self, ttl: float = DEFAULT_TTL_SECONDS):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._names: dict[str, str] = {}
        self._expires_at: float | None = None

    def fresh(self) -> bool:
        with self._lock:
            return (self._expires_at is not None
                    and time.monotonic() < self._expires_at)

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return dict(self._names)

    def replace(self, names: dict[str, str]) -> None:
        with self._lock:
            self._names = dict(names)
            self._expires_at = time.monotonic() + self.ttl

    def invalidate(self) -> None:
        with self._lock:
            self._names = {}
            self._expires_at = None


_index = _ConceptIndex()


def invalidate_concepts() -> None:
    """Drop the cached concept index (tests, and a future rename webhook)."""
    _index.invalidate()


def concept_display_names(
    guids,
    *,
    client: PlanClient | None = None,
    use_cache: bool = True,
) -> dict[str, str]:
    """Resolve many concept guids to display names in ONE request.

    Returns ``{guid: display_name}`` for the guids plan.pdhc knows, omitting
    the ones it does not — the caller decides what to show for a name plan
    has never heard of, and a fabricated placeholder here would be
    indistinguishable from a real label.

    ``{}`` means either "you asked about nothing" or "plan.pdhc answered and
    knows none of these". It NEVER means a failure: `PlanNotConfigured` and
    `PlanUnreachable` propagate so the events panel can mark itself degraded
    (``EventsPage.unavailable``) while every other panel renders.
    """
    wanted = [str(g) for g in (guids or []) if g]
    if not wanted:
        return {}
    if use_cache and _index.fresh():
        names = _index.snapshot()
    else:
        client = client or build_client()
        names = client.fetch_concept_index()
        if use_cache:
            _index.replace(names)
    return {g: names[g] for g in dict.fromkeys(wanted) if g in names}


def concept_display_name(
    guid: str,
    *,
    client: PlanClient | None = None,
    use_cache: bool = True,
) -> str | None:
    """One concept's display name, or None if plan.pdhc does not know it.

    Goes through the batch path on purpose, so a route that calls this in a
    loop gets one request and not one per call.
    """
    if not guid:
        return None
    return concept_display_names(
        [guid], client=client, use_cache=use_cache).get(str(guid))


def build_client() -> PlanClient:
    """Construct a `PlanClient` from the resolved base URL.

    No token and no service key — see `PlanClient._headers` and #931. The
    factory exists so route code stays terse and tests can monkeypatch it.
    """
    return PlanClient()
