"""Read-only client for icd11.pdhc — the patient card's diagnosis chips.

ONE question, asked of one endpoint::

    GET {ICD11_BASE_URL}/api/v1/patients/<patient_guid>/diagnosis-summary

(`app/api/entry.py:547`, on a blueprint registered with
``url_prefix="/api/v1"`` at `app/api/entry.py:47` — the bare
``/patients/...`` in the plan is a ROUTE, not a URL.) It answers with a FHIR
R5 **Problem List Bundle** built by ``fhir.problem_list_bundle()``, and this
module turns that into ``dto.Diagnosis`` objects plus the one piece of
metadata the chips cannot be rendered honestly without.

THE SCOPE NOTE IS THE PAYLOAD, NOT A DECORATION
-----------------------------------------------
icd11 is organisation-scoped on the episode's ``patient_org_guid`` (Rule 24,
`icd11.pdhc/app/auth.py::AccessContext.scope`). An empty problem list
therefore means one of two completely different things — *this patient has no
diagnoses*, and *this patient has diagnoses you are not scoped to see* — and
icd11 returning the same empty bundle for both sent a clinician hunting a bug
that was not there on 2026-10-23. icd11's own fix was to SAY which:

``meta.tag`` always carries ``urn:pdhc:as-of`` and ``urn:pdhc:scope``
(``unrestricted`` | ``organisation``), and when the read was scoped it adds
``meta.scopeNote`` naming how many organisations it was filtered to.

So `ProblemList` carries `scope`, `scope_note` and `as_of` beside the
diagnoses, `scope_note` is passed through VERBATIM (never reworded, never
summarised, never dropped), and `is_definitively_empty` is True only for an
*unscoped* empty list. `patient_card_fields()` spells the two
`dto.PatientCard` keyword arguments — `diagnoses` and
`diagnosis_scope_note` — so a rename in `dto.py`, which this module must not
edit, breaks in one place.

A `ProblemList` with no diagnoses and a `scope_note` is NOT the same object
as one with no diagnoses and no `scope_note`. Collapsing them to `[]` is the
defect this client exists to prevent.

MEASURED AGAINST icd11.pdhc's SOURCE 2026-11-11 — six ways the wire differs
from the brief, each of which would otherwise be found as a bug
---------------------------------------------------------------------------
1.  **AN "EMPTY" BUNDLE IS NOT AN EMPTY ``entry`` ARRAY.**
    `fhir.problem_list_bundle` emits ONE sentinel Condition when there are no
    states — the IPS absent/unknown pattern, system
    ``.../absent-unknown-uv-ips``, code ``no-problem-info``, text "Inga kända
    diagnoser" — because IPS requires absence to be *stated*. It must be
    recognised, not parsed into a `Diagnosis` with a nonsense code and not
    merely skipped as malformed: `ProblemList.absent_assertion` records that
    icd11 explicitly asserted "nothing here".

2.  **RESOLVED AND INACTIVE EPISODES ARE FILTERED OUT UPSTREAM BY DEFAULT.**
    `episode.problem_list` drops ``clinical_status in ('resolved',
    'inactive')`` unless the request carries ``include_inactive=true``. So
    the only way to see a resolved diagnosis is to ask for it, which is why
    `problem_list(include_inactive=True)` exists.

3.  **THE PROBLEM-LIST PROJECTION IS NARROWER THAN THE FULL CONDITION.**
    `fhir._state_as_condition` emits id, meta.security, clinicalStatus,
    verificationStatus, code.coding (one), code.text, subject and
    ``onsetDateTime`` — and nothing else. ``recordedDate`` and
    ``abatementDateTime`` exist only on `fhir.condition()`, which this
    endpoint does not use. `Diagnosis.recorded_date` / `.abatement` are
    therefore None from this endpoint today; they are read anyway, so the
    same parser serves `/episodes/<guid>/history` (which does use
    `fhir.condition`) and so a future widening needs no change here.

4.  **``encounter_ref`` IS NOT ON A CONDITION AT ALL.** icd11 keeps
    primär/bidiagnos on the Encounter, deliberately ("the same condition can
    be huvuddiagnos at one visit and bidiagnos at another"), reachable at
    ``GET /api/v1/encounters/<ref>/diagnoses`` — keyed by encounter, so it
    cannot enrich a patient problem list. ``Condition.encounter.reference``
    is read where FHIR puts it, and is None from this endpoint.

5.  **``foundation_uri`` IS A *PART*-LEVEL COLUMN AND IS NEVER SERIALISED.**
    `DiagnosisCodePart.foundation_uri` reaches no FHIR projection, so
    `Diagnosis.foundation_uri` is always None here. Reported rather than
    faked: a WHO URI invented from a code would look authoritative.

6.  **THE RELEASE HAS NOWHERE TO GO.** Every coding carries ``version`` = the
    ICD-11 release, because icd11's Rule 28 forbids a code travelling without
    it. `dto.Diagnosis` has no field for it and this module may not edit
    `dto.py`, so the release is dropped at that boundary. `ProblemList`
    exposes it per code in `releases` so the information is not lost before
    the dto, and a chip tooltip can show it.

DISPLAY TEXT COMES FROM icd11, NOT TERMBANK
-------------------------------------------
termbank holds no ICD-11 concepts yet (#965), so a ``$lookup`` there returns
nothing for an ICD-11 code. icd11 stores the text itself —
``DiagnosisCode.label`` and ``DiagnosisCodePart.label`` are both NOT NULL —
so a postcoordinated cluster already arrives rendered in full in
``coding.display`` / ``code.text``. This client takes it from the bundle and
asks nobody else.

AUTH — ``Authorization: Bearer <the operator's SSO token>``, MEASURED
---------------------------------------------------------------------
icd11's ``require_auth`` -> ``_sso_context`` (`icd11.pdhc/app/auth.py`) reads
``Authorization: Bearer <token>`` and, failing that, a Flask **session
cookie** (``session['sso_token']``, for a clinician in a browser). It
validates against ``sso.pdhc /api/auth/me/service`` on EVERY request and
never caches the blob.

There is **no machine door**: icd11 has no ``KNOWN_SERVICES``, no
``X-Service-Key`` and no ``X-API-Key`` on any inbound route — the only
``X-Service-Key`` in that repo is OUTBOUND, in its own `cdr_client`. So the
obvious guess is right here and was wrong next door: request.pdhc takes
``X-API-Key`` and ignores ``Authorization`` entirely, and ips takes
``Authorization`` and silently ignores ``X-API-Key`` (#783, three services
made that assumption independently). Hence the header below is checked by a
test rather than assumed, and nothing else is invented.

Two consequences worth knowing before debugging a 403:

* icd11 refuses a token whose account ``must_change_password`` (#949,
  ``PASSWORD_CHANGE_REQUIRED``) and a session carrying none of the phases
  ``planning``/``request``/``analysis`` (``PHASE_NOT_PERMITTED``). Both are
  403s *about the operator*, not about this client.
* ``X-Admin-Read-Reason`` is icd11's break-glass header: an SU admin who
  states a reason reads UNSCOPED. This client never sends it. Widening a
  clinical read is a decision a human makes and states, not something a BFF
  does on their behalf, and the audit row would then carry no reason worth
  the name.

``X-Operator-Session-Id`` is also sent, via `session_headers` — that is X2
session propagation (#408) for the PDL kontroller chain, **not** a
credential. Do not read it as one.

TWO EXCEPTION TYPES, NOT ONE FLAG
---------------------------------
``Icd11NotConfigured``  — ``ICD11_BASE_URL`` is unset. A DEPLOYMENT fact an
                          operator fixes with one line; retrying cannot help.
``Icd11Unreachable``    — icd11 was asked and gave no answer this client may
                          trust (network, timeout, non-2xx including an
                          authenticated 401/403, or a non-JSON body).

Neither is ever reported as an empty list. **An empty `ProblemList.diagnoses`
always means icd11 answered and there are none** — which, with the scope note
beside it, is the whole contract. `request_client` raises for the same reason
and `ips_client` deliberately does not: a missing spärr answer still leaves
cdr1's own filter in place, whereas an empty diagnosis row on a patient card
is a clinical assertion nobody made.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

import requests
from flask import current_app

from app.dashboard.dto import Diagnosis

#: The one environment variable. There is deliberately NO `DEFAULT_BASE_URL`
#: beside it: xlate's module-level `DEFAULT_BASE_URL = "http://127.0.0.1:9017"`
#: manufactured a configured-looking URL out of an unset variable, so "nobody
#: wired this up" became indistinguishable from "it blipped" and was reported
#: as a retryable 503 forever (#833). Unset reads as unset, and the refusal
#: names the key.
ENV_BASE_URL = "ICD11_BASE_URL"

DEFAULT_TIMEOUT = 6.0

#: WHO MMS — the only coding system a diagnosis here is expressed in, and the
#: value `dto.Diagnosis.system` already defaults to. A coding in any other
#: system is not an ICD-11 diagnosis and is not parsed as one.
MMS_SYSTEM = "http://id.who.int/icd/release/11/mms"

#: The IPS absent/unknown pattern icd11 uses to STATE emptiness (finding 1).
ABSENT_UNKNOWN_SYSTEM = (
    "http://hl7.org/fhir/uv/ips/CodeSystem/absent-unknown-uv-ips")
NO_PROBLEM_INFO = "no-problem-info"

CLINICAL_STATUS_CS = "http://terminology.hl7.org/CodeSystem/condition-clinical"
VERIFICATION_STATUS_CS = (
    "http://terminology.hl7.org/CodeSystem/condition-ver-status")

#: `meta.tag` systems icd11 stamps on every problem-list bundle.
AS_OF_TAG_SYSTEM = "urn:pdhc:as-of"
SCOPE_TAG_SYSTEM = "urn:pdhc:scope"

SCOPE_UNRESTRICTED = "unrestricted"
SCOPE_ORGANISATION = "organisation"

#: A cluster expression is postcoordinated when it joins parts: `&` adds a
#: qualifier on an axis, `/` joins two stem codes in one statement
#: (`icd11.pdhc/app/models/diagnosis_code.py` docstring). A bare stem has
#: neither, and calling it "postcoordinated" would make the field meaningless.
_CLUSTER_JOINERS = ("&", "/")


# ---------------------------------------------------------------------------
# Errors — two types, not one flag
# ---------------------------------------------------------------------------

class Icd11ClientError(Exception):
    """Base for every failure of an icd11.pdhc read."""


class Icd11NotConfigured(Icd11ClientError):
    """`ICD11_BASE_URL` is unset, so there is nowhere to ask.

    A deployment fact, not a transient one. Distinct from
    `Icd11Unreachable` because the operator action is different — set a
    variable, versus look at why a service did not answer — and because a
    single boolean or an empty list makes them indistinguishable, which is
    exactly how every spärr lookup in this service came back "no blocks"
    without asking anyone (#827).
    """


class Icd11Unreachable(Icd11ClientError):
    """icd11.pdhc produced no answer this client may trust.

    A network error, a timeout, a non-2xx (401/403 included — an
    authenticated refusal is an answer, but not an answer about diagnoses),
    or a body that is not JSON. `status_code` is set when there was one.
    """

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def resolve_base_url() -> str:
    """Where icd11.pdhc is, by the ONE rule every caller must agree on.

    `ips_client.resolve_base_url()`'s pattern verbatim, and for its reason:
    `IpsClient.__init__` read `os.environ` while `is_configured()` read
    `current_app.config`, and nothing loaded the variable into the config —
    so setting `IPS_BASE_URL` made the client work and left the question
    answering False forever (#827). Two resolution paths for one deployment
    fact IS the defect; this is the single path.

    **The environment leg is load-bearing here, not a nicety.** `create_app`
    loads `CDR1_BASE_URL`, `IPS_BASE_URL`, `GATEWAY_BASE_URL` and
    `SSO_BASE_URL` into the config and does NOT load this key (measured
    2026-11-11; this module may not edit `app/__init__.py`), so in production
    — where the value arrives through compose's `env_file: .env` — the
    environment is the ONLY path. Config still wins, so a test or
    `create_app(config=...)` can point it elsewhere.

    Returns `""` when unset: never a fabricated URL, and never an exception,
    because `is_configured()` calls this outside a request context.
    """
    try:
        from_config = (current_app.config.get(ENV_BASE_URL) or "").strip()
    except RuntimeError:  # no application context
        from_config = ""
    return (from_config or os.environ.get(ENV_BASE_URL, "")).strip()


def is_configured() -> bool:
    """Can icd11.pdhc be asked at all?

    Separate from "does this patient have diagnoses", and the distinction is
    the point: a card that wants to render "we could not find out" rather
    than "no diagnoses" asks this first, without provoking an exception.
    Safe outside an application context.
    """
    return bool(resolve_base_url())


# ---------------------------------------------------------------------------
# Pure parsing — no network, no app context
# ---------------------------------------------------------------------------

def _codings(resource: Mapping, field: str = "code") -> list[Mapping]:
    block = resource.get(field)
    if not isinstance(block, Mapping):
        return []
    codings = block.get("coding")
    if not isinstance(codings, Sequence) or isinstance(codings, (str, bytes)):
        return []
    return [c for c in codings if isinstance(c, Mapping)]


def _status(resource: Mapping, field: str) -> str | None:
    """`clinicalStatus` / `verificationStatus` -> its code, or None.

    Read off the coding rather than any `text`, because the code is the
    machine-comparable half and `text` is free prose nobody promised.
    """
    for coding in _codings(resource, field):
        code = coding.get("code")
        if isinstance(code, str) and code.strip():
            return code.strip()
    return None


def is_absent_assertion(resource: Any) -> bool:
    """Is this the IPS "no information about problems" sentinel?

    Finding 1: an empty problem list is not an empty `entry` array. icd11
    emits one Condition carrying `absent-unknown-uv-ips#no-problem-info`,
    because IPS requires absence to be STATED rather than implied by an
    empty array — "the distinction the xlate defect (#833) turned on", in
    `fhir.problem_list_bundle`'s own words.

    Recognising it is what keeps it from being counted as a diagnosis AND
    from being written off as malformed. Those are three different things.
    """
    if not isinstance(resource, Mapping):
        return False
    for coding in _codings(resource):
        if (coding.get("system") == ABSENT_UNKNOWN_SYSTEM
                and coding.get("code") == NO_PROBLEM_INFO):
            return True
    return False


def is_postcoordinated(code: str | None) -> bool:
    """Does this cluster expression join more than one component?

    `5A11` is a bare stem; `1A00&XN62R` adds a qualifier on an axis and
    `5A11/BD54.0` joins two stems. Only the latter two are postcoordinated.
    """
    return bool(code) and any(j in code for j in _CLUSTER_JOINERS)


def parse_diagnosis(resource: Any) -> Diagnosis | None:
    """One FHIR Condition -> `dto.Diagnosis`, or None if it is not one.

    Pure. Returns None — SKIPPING the entry — rather than fabricating, for:

    * anything that is not a `Condition` resource,
    * the IPS absence sentinel (its "code" is a statement about the list,
      not a diagnosis),
    * a Condition with no MMS coding carrying a non-empty `code`,
    * a Condition with no display text at all,
    * a Condition missing `clinicalStatus` or `verificationStatus`.

    The last three look strict and are not. Every one of those is NOT NULL in
    icd11's own schema (`DiagnosisCode.label`, `DiagnosisEpisode`'s status
    columns) and is emitted unconditionally by `fhir._state_as_condition`, so
    an entry missing one did not come from the projection this client reads.
    `dto.Diagnosis` types all four as a plain `str`, so the alternatives are
    to invent a code, a label or a clinical status — and an invented
    `clinical_status` is a clinical claim nobody made, which is the one thing
    `dto.py`'s closing rule forbids outright.

    The FIRST MMS coding is the diagnosis. When a cluster has a
    pre-coordinated equivalent, `fhir.condition` appends it as a SECOND MMS
    coding (`simplified_to`) — additional information, explicitly "not a
    replacement" — so taking `[0]` keeps what the clinician actually entered.
    """
    if not isinstance(resource, Mapping):
        return None
    if resource.get("resourceType") != "Condition":
        return None
    if is_absent_assertion(resource):
        return None

    code = None
    display = None
    for coding in _codings(resource):
        if coding.get("system") != MMS_SYSTEM:
            continue
        raw = coding.get("code")
        if isinstance(raw, str) and raw.strip():
            code = raw.strip()
            maybe = coding.get("display")
            display = maybe.strip() if isinstance(maybe, str) and maybe.strip() else None
            break
    if not code:
        return None

    if not display:
        # `code.text` is icd11's IPS narrative — "the rendered label as its
        # own text ... the safety net", same NOT NULL label. A legitimate
        # second home for the display, not a fallback chain over nothing.
        block = resource.get("code")
        text = block.get("text") if isinstance(block, Mapping) else None
        display = text.strip() if isinstance(text, str) and text.strip() else None
    if not display:
        return None

    clinical = _status(resource, "clinicalStatus")
    verification = _status(resource, "verificationStatus")
    if not clinical or not verification:
        return None

    encounter = resource.get("encounter")
    encounter_ref = (encounter.get("reference")
                     if isinstance(encounter, Mapping) else None)

    return Diagnosis(
        code=code,
        display=display,
        clinical_status=clinical,
        verification_status=verification,
        # Present on the problem-list projection.
        onset=resource.get("onsetDateTime"),
        # Findings 3 and 4: emitted by `fhir.condition` (the episode read and
        # `/episodes/<guid>/history`), never by `fhir._state_as_condition`.
        # Read here so one parser serves both, None from this endpoint.
        abatement=resource.get("abatementDateTime"),
        recorded_date=resource.get("recordedDate"),
        postcoordination=code if is_postcoordinated(code) else None,
        # Finding 5: `DiagnosisCodePart.foundation_uri` reaches no FHIR
        # projection. Left None rather than derived from the code — a WHO URI
        # invented here would look authoritative.
        foundation_uri=None,
        encounter_ref=encounter_ref,
        # `system` keeps its dto default: by construction we only accepted an
        # MMS coding above.
    )


def _release_of(resource: Mapping) -> str | None:
    """The ICD-11 release the code was expressed in (`coding.version`).

    Rule 28 in icd11: "never the code without the release it was expressed
    in". `dto.Diagnosis` has no field for it (finding 6), so it is carried on
    `ProblemList` instead of being silently dropped at the parser.
    """
    for coding in _codings(resource):
        if coding.get("system") != MMS_SYSTEM:
            continue
        version = coding.get("version")
        if isinstance(version, str) and version.strip():
            return version.strip()
    return None


def _tag(bundle: Mapping, system: str) -> str | None:
    meta = bundle.get("meta")
    if not isinstance(meta, Mapping):
        return None
    tags = meta.get("tag")
    if not isinstance(tags, Sequence) or isinstance(tags, (str, bytes)):
        return None
    for tag in tags:
        if isinstance(tag, Mapping) and tag.get("system") == system:
            code = tag.get("code")
            if isinstance(code, str) and code.strip():
                return code.strip()
    return None


@dataclass(frozen=True)
class ProblemList:
    """A parsed diagnosis-summary bundle: the diagnoses AND who was allowed
    to see them.

    This dataclass exists so that `[]` can never be the whole answer. Three
    outcomes are distinguishable here and must stay so at every layer above:

    ===========================  ==========  ================  ==============
    outcome                      diagnoses   scope             scope_note
    ===========================  ==========  ================  ==============
    the patient has none         ``()``      ``unrestricted``  ``None``
    none you may see             ``()``      ``organisation``  **text**
    some, possibly not all       non-empty   ``organisation``  **text**
    ===========================  ==========  ================  ==============

    Row 3 is the one most easily lost: a scoped read that DID return
    diagnoses still carries the note, because the list may still be partial.
    `scope_note` is therefore parsed unconditionally, never only when empty.
    """

    #: Ready for `dto.PatientCard.diagnoses`.
    diagnoses: tuple[Diagnosis, ...] = ()
    #: `urn:pdhc:scope` — "unrestricted" | "organisation", or None when the
    #: bundle carried no tag at all (an older icd11, or a body that is not
    #: one of its bundles). None is NOT "unrestricted".
    scope: str | None = None
    #: `meta.scopeNote`, VERBATIM. Goes to
    #: `dto.PatientCard.diagnosis_scope_note`. Never reworded here: it names
    #: the number of organisations and the break-glass header, and a
    #: paraphrase would be this service's claim rather than icd11's.
    scope_note: str | None = None
    #: `urn:pdhc:as-of` — the instant the answer describes. icd11 stamps it
    #: on every bundle, defaulting to "now", so the same question asked twice
    #: is answerable identically later.
    as_of: str | None = None
    #: icd11 explicitly STATED there is nothing (the IPS sentinel, finding 1)
    #: rather than this client finding no parseable entry.
    absent_assertion: bool = False
    #: Parallel to `diagnoses`: the ICD-11 release each code was expressed in
    #: (finding 6 — `dto.Diagnosis` has no field for it).
    releases: tuple[str | None, ...] = ()
    #: Entries that were neither a diagnosis nor the absence sentinel. Non-
    #: zero means this client dropped something; it is surfaced rather than
    #: swallowed so "0 diagnoses" can be audited.
    skipped_entries: int = 0

    @property
    def is_scoped(self) -> bool:
        """Was this read filtered to the caller's organisations?"""
        return self.scope == SCOPE_ORGANISATION

    @property
    def is_definitively_empty(self) -> bool:
        """True only for "this patient has no diagnoses", full stop.

        An empty SCOPED list is not definitively empty — that is the
        2026-10-23 defect in one predicate — and neither is an empty list
        from a bundle that carried no scope tag, because an absent tag is an
        unknown answer and not an unrestricted one.
        """
        return not self.diagnoses and self.scope == SCOPE_UNRESTRICTED

    @property
    def may_be_incomplete(self) -> bool:
        """Could a diagnosis exist that this answer does not show?

        True whenever the read was scoped or the scope is unknown. What a
        chip row should hang "+ dolda uppgifter" off, rather than off
        `len(diagnoses) == 0`.
        """
        return self.scope != SCOPE_UNRESTRICTED

    def patient_card_fields(self) -> dict:
        """The two `dto.PatientCard` keyword arguments this answer supplies.

        Spelled here rather than at each call site so a rename in `dto.py` —
        which this module must not edit — breaks in one place, and so that
        the scope note cannot be forgotten by a caller who only wanted the
        chips. `plan_client.as_plan_summary_fields` for the same reason.
        """
        return {
            "diagnoses": self.diagnoses,
            "diagnosis_scope_note": self.scope_note,
        }


def parse_problem_list_bundle(bundle: Any) -> ProblemList:
    """A diagnosis-summary Bundle -> `ProblemList`. Pure (no network).

    Separate from the transport because this is where the bugs are and it
    should be testable without a socket — `cdr1_client.parse_patient_bundle`
    and `plan_client.parse_plan_definition_bundle`'s shape.

    A non-Bundle body yields an empty `ProblemList` with `scope=None`, which
    `is_definitively_empty` reads as "not an answer about absence". The
    transport has already refused anything that is not a 200 with JSON, so
    this is a shape guard and not a failure path.
    """
    if not isinstance(bundle, Mapping):
        return ProblemList()

    diagnoses: list[Diagnosis] = []
    releases: list[str | None] = []
    absent = False
    skipped = 0

    entries = bundle.get("entry")
    if isinstance(entries, Sequence) and not isinstance(entries, (str, bytes)):
        for entry in entries:
            resource = entry.get("resource") if isinstance(entry, Mapping) else None
            if is_absent_assertion(resource):
                absent = True
                continue
            parsed = parse_diagnosis(resource)
            if parsed is None:
                skipped += 1
                _log_skipped(resource)
                continue
            diagnoses.append(parsed)
            releases.append(_release_of(resource))

    meta = bundle.get("meta")
    note = meta.get("scopeNote") if isinstance(meta, Mapping) else None

    return ProblemList(
        diagnoses=tuple(diagnoses),
        scope=_tag(bundle, SCOPE_TAG_SYSTEM),
        scope_note=note if isinstance(note, str) and note.strip() else None,
        # `meta.tag[urn:pdhc:as-of]` rather than `Bundle.timestamp`: icd11
        # sets the timestamp only when an explicit `as_of` was asked for,
        # while the tag is stamped on every bundle (defaulting to now).
        as_of=_tag(bundle, AS_OF_TAG_SYSTEM) or bundle.get("timestamp"),
        absent_assertion=absent,
        releases=tuple(releases),
        skipped_entries=skipped,
    )


def _log_skipped(resource: Any) -> None:
    """Say so when an entry is dropped, with a greppable marker.

    A skipped entry is a diagnosis this service decided not to show. Doing
    that in silence is how "0 diagnoses" becomes untrustworthy, so it is
    logged — `ips_client`'s `SPARR_UNAVAILABLE` shape, so monitoring can
    alert on one pattern across services.
    """
    try:
        rid = resource.get("id") if isinstance(resource, Mapping) else None
        current_app.logger.warning(
            "ICD11_ENTRY_SKIPPED a diagnosis-summary entry was not a "
            "parseable Condition and was dropped (id=%s)", rid or "?")
    except RuntimeError:  # no application context
        pass


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

def _as_of_param(as_of: Any) -> str | None:
    """Normalise `as_of` to the ISO-8601 string icd11's `_parse_dt` accepts.

    A `datetime` is serialised here so callers need not. Note icd11 repairs
    the classic `?as_of=...+00:00` breakage (a `+` in a query string decodes
    to a space) — but `requests` percent-encodes the `+` as `%2B`, so the
    offset survives intact and the repair never has to fire.
    """
    if as_of is None:
        return None
    if isinstance(as_of, datetime):
        return as_of.isoformat()
    text = str(as_of).strip()
    return text or None


class Icd11Client:
    """Thin, read-only wrapper around icd11.pdhc. Every method is a GET.

    `token` is the operator's own SSO bearer. There is no service-identity
    fallback because icd11 offers no door for one — see the module docstring
    — so these reads are session-only by construction, and a client built
    outside a request context carries no credential and will be refused
    upstream. That is the honest outcome; inventing a key that authenticates
    nothing is the mistake three services made against ips (#783).
    """

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.base_url = (base_url if base_url is not None
                         else resolve_base_url()).strip().rstrip("/")
        self.token = token
        self.timeout = timeout

    # -- headers ---------------------------------------------------------
    def _headers(self) -> dict:
        """Accept + the operator's bearer + X2 session propagation.

        `Authorization: Bearer` is icd11's measured door (`_sso_context`).
        No `X-Service-Key` and no `X-API-Key`: icd11 reads neither on any
        inbound route, so either would look like a credential and
        authenticate nothing. No `X-Admin-Read-Reason`: break-glass is a
        human decision, stated by the human.
        """
        from app.services.session_headers import outbound_session_headers
        h = {"Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        h.update(outbound_session_headers())
        return h

    # -- transport -------------------------------------------------------
    def _get(self, path: str, *, what: str, params: Any = None,
             item: bool = False) -> Any:
        """GET and return the parsed body, or raise. Never `{}` for failure.

        `item=True` marks a single-resource path, where icd11's 404 is an
        ANSWER ("no such episode") and returns None. On the patient path a
        404 is NOT emptiness — icd11 answers an unknown patient with a 200
        and the absence sentinel, so a 404 there means the route is not where
        we think it is, which a prefix or reverse-proxy mistake produces. It
        raises, and that asymmetry is what keeps an empty diagnosis list
        meaning "icd11 answered and there are none".
        """
        if not self.base_url:
            raise Icd11NotConfigured(
                f"{ENV_BASE_URL} is not set, so icd11.pdhc cannot be asked "
                f"for {what}: this patient's diagnoses are UNKNOWN, which is "
                "not the same as absent, and the card must not render an "
                f"empty diagnosis row. Set {ENV_BASE_URL} to the public "
                "subdomain (https://icd11.pdhc.se) in this service's .env "
                "(shared/.env, which current/.env symlinks to); there is "
                "deliberately no default to fall back to."
            )
        url = f"{self.base_url}{path}"
        try:
            r = requests.get(url, params=params or None,
                             headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as e:
            raise Icd11Unreachable(
                f"icd11.pdhc {what} failed (network): {e}") from e
        if item and r.status_code == 404:
            return None
        if r.status_code >= 400:
            # icd11 answers a refusal with a FHIR OperationOutcome whose
            # `diagnostics` is the machine code (PASSWORD_CHANGE_REQUIRED,
            # PHASE_NOT_PERMITTED, SSO_NOT_CONFIGURED, ...). Carried into the
            # message because a bare "403" sends the reader to the wrong
            # service: all three are facts about the OPERATOR's sso account.
            raise Icd11Unreachable(
                f"icd11.pdhc {what} returned {r.status_code}"
                f"{_diagnostics_suffix(r)}",
                status_code=r.status_code,
            )
        try:
            return r.json()
        except ValueError as e:
            raise Icd11Unreachable(
                f"icd11.pdhc {what} returned a non-JSON body",
                status_code=r.status_code,
            ) from e

    # -- the problem list ------------------------------------------------
    def fetch_diagnosis_summary(
        self,
        patient_guid: str,
        *,
        as_of: Any = None,
        include_inactive: bool = False,
    ) -> dict:
        """`GET /api/v1/patients/<guid>/diagnosis-summary` — the raw Bundle.

        Exposed beside `problem_list()` because the bundle is the IPS
        artefact: a caller that needs to forward it (a service-request
        snapshot, a crisis export) must send icd11's own JSON and not a
        re-serialisation of this module's dataclasses.
        """
        params: dict[str, str] = {}
        value = _as_of_param(as_of)
        if value:
            params["as_of"] = value
        if include_inactive:
            # icd11 tests the literal string "true"
            # (`request.args.get("include_inactive") == "true"`), so a
            # Python bool stringified to "True" would silently do nothing.
            params["include_inactive"] = "true"
        body = self._get(
            f"/api/v1/patients/{patient_guid}/diagnosis-summary",
            what="diagnosis summary", params=params,
        )
        return body if isinstance(body, dict) else {}

    def problem_list(
        self,
        patient_guid: str,
        *,
        as_of: Any = None,
        include_inactive: bool = False,
    ) -> ProblemList:
        """The patient's problem list, parsed, WITH its scope note.

        `as_of` is icd11's time travel: the same question answered the same
        way before and after a code change. Pass a `datetime` or an ISO-8601
        string.

        `include_inactive=True` is the only way to see a resolved or inactive
        diagnosis — `episode.problem_list` filters both out otherwise
        (finding 2).

        An empty `.diagnoses` means icd11 ANSWERED and there are none for
        this reader; `.scope_note` and `.is_definitively_empty` say which
        kind of none. A failure raises.
        """
        return parse_problem_list_bundle(self.fetch_diagnosis_summary(
            patient_guid, as_of=as_of, include_inactive=include_inactive))

    # -- the append-only log ---------------------------------------------
    def fetch_episode_history(self, episode_guid: str) -> dict | None:
        """`GET /api/v1/episodes/<guid>/history` — the assertion log.

        A FHIR history Bundle of Conditions (each the episode AS AT that
        statement, via `fhir.condition`) plus a parallel `provenance` array.
        A correction appears as a NEW entry carrying `supersedes-seq` and the
        superseded statement is still there, because the fact that something
        was once asserted is itself clinical information — so this is the
        read behind "why did this chip change?".

        None when icd11 says 404. **Beware: a 403 is not a 404 here.** icd11
        answers an out-of-scope episode with 403 `outside your organisation`,
        which raises `Icd11Unreachable` — correctly, because it is not an
        answer about the history, and flattening it to None would assert the
        episode does not exist.
        """
        if not episode_guid:
            return None
        body = self._get(f"/api/v1/episodes/{episode_guid}/history",
                         what="episode history", item=True)
        return body if isinstance(body, dict) else None


def _diagnostics_suffix(response) -> str:
    """` (DIAGNOSTIC_CODE)` from a FHIR OperationOutcome, or ``""``.

    Best-effort and never raises: this runs while building an error message,
    and a failure here would replace a useful 403 with a confusing traceback.
    """
    try:
        body = response.json()
        issues = body.get("issue") if isinstance(body, Mapping) else None
        if isinstance(issues, Sequence) and not isinstance(issues, (str, bytes)):
            for issue in issues:
                if not isinstance(issue, Mapping):
                    continue
                code = issue.get("diagnostics")
                if isinstance(code, str) and code.strip():
                    return f" ({code.strip()})"
    except Exception:  # noqa: BLE001 — message decoration only
        pass
    return ""


def build_client() -> Icd11Client:
    """Construct a client from the resolved base URL + the session's token.

    The factory exists so route code stays terse and tests can monkeypatch
    it — `request_client.build_client` / `ips_client._default_client`.

    The token is the operator's own. There is no service-identity fallback:
    icd11 has no `KNOWN_SERVICES` and reads no `X-Service-Key`, so there is
    no door to give a credential to, and a client built outside a request
    context is refused upstream rather than carrying something invented.
    """
    from flask import session  # local import — module stays import-time safe
    token = None
    try:
        token = session.get("sso_token")
    except RuntimeError:  # no request context
        token = None
    return Icd11Client(base_url=resolve_base_url() or None, token=token)
