"""Typed DTOs for the dashboard BFF — the one shared contract (plan step 4).

These are dataclasses rather than dicts for one reason: the plan's panels are
built by different hands at different times, and a typo in a dict key is a
silent empty panel. A missing attribute is an immediate AttributeError.

FOUR MEASURED CORRECTIONS TO THE PLAN'S OWN DTO LIST, all from the Gate 0
report in `plans/dashboard_sources.md`. Each one would otherwise be
discovered as a bug.

1.  THERE IS NO `concept_guid` ON A CDR OBSERVATION. Measured on cdr1: the
    column is `code_canonical`, the Path-B embedded-guid form. The plan writes
    "`concept_guid`/`code_canonical`" as if either were available; only the
    second is. So `EventRow.code_canonical` is the identity, and
    `concept_guid` is a derived, optional field.

2.  CONTEXT JOINS ON `ingest_raw_guid`, NOT `transaction_guid`. A transaction
    is a BATCH: 10 distinct `transaction_guid` values carry 1 949
    observations, so joining `observation` to `clinical_context` on it returns
    229 context rows per observation (446 439 rows in total) and would
    silently multiply the event list. `ingest_raw_guid` is exactly 1:1.

3.  `t_from_start_seconds` IS NULL FOR EVERY ROW TODAY. `care_plan_guid` is
    populated on 0 of 1 949 observations and on 0 of 1 949 `clinical_context`
    rows, so there is no CarePlan to take `period.start` from. The plan treats
    "show —" as an edge case; it is the only case until the datapoint-context
    work lands. `CarePlan.period_start` is itself nullable in request.pdhc,
    so even a linked plan may not supply an origin.

4.  A PLAN ITEM CANNOT BE MATCHED TO ITS OBSERVATIONS BY `transaction_guid`.
    Step 15 says to, and with 10 transactions over 1 949 rows that highlights
    ~195 rows per item. `plan_activity_guid` — the VISIT (#820) — is populated
    on 1 942 of 1 949 and is the usable key; `ingest_raw_guid` is exact.

AND ONE RULE THAT IS NOT A FIELD: a value that was never recorded and a value
that could not be fetched are DIFFERENT, and the dashboard must not collapse
them. `None` here means "not recorded". A source that failed is reported by
the enclosing response's `unavailable` list, never by writing None into a
field. This is #827's lesson (`has_blocked_sources` was False both when there
was no block and when ips was never asked) and icd11's `urn:pdhc:scope` tag
(an empty problem list means either "no diagnoses" or "none you may see").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# --------------------------------------------------------------------------
# patient card
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Diagnosis:
    """One ICD-11 diagnosis, from icd11.pdhc's Problem List bundle.

    `system` is fixed to the WHO MMS URI. `display` comes from
    `DiagnosisCode.label` in icd11.pdhc — NOT from termbank, which holds no
    ICD-11 concepts yet (#965). `postcoordination` carries the full cluster
    (e.g. `5A11&XS...`), whose parts each have their own label.
    """
    code: str
    display: str
    clinical_status: str
    verification_status: str
    onset: str | None = None
    abatement: str | None = None
    recorded_date: str | None = None
    postcoordination: str | None = None
    foundation_uri: str | None = None
    encounter_ref: str | None = None
    system: str = "http://id.who.int/icd/release/11/mms"


@dataclass(frozen=True)
class EpsEntry:
    display: str
    source: str
    code: str | None = None
    system: str | None = None
    date: str | None = None
    status: str | None = None


@dataclass(frozen=True)
class EpsSection:
    """One euIPS section.

    `absent_assertion` is True when the section carries an explicit
    `absent-unknown-uv-ips` statement — "no known allergies" — which euIPS
    REQUIRES rather than permitting an empty section.

    `unavailable` is the THIRD state and the reason this dataclass is not just
    a list: a section that could not be composed is not a section asserting
    absence. Collapsing the two would state a clinical fact nobody made.
    """
    section_code: str
    title: str
    entries: tuple[EpsEntry, ...] = ()
    narrative: str | None = None
    absent_assertion: bool = False
    unavailable: bool = False


@dataclass(frozen=True)
class PatientCard:
    patient_guid: str
    name: str | None = None
    birth_date: str | None = None
    sex: str | None = None
    personnummer: str | None = None
    organisation: str | None = None
    diagnoses: tuple[Diagnosis, ...] = ()
    eps_sections: tuple[EpsSection, ...] = ()
    #: icd11's `urn:pdhc:scope` note, verbatim. Present when the problem list
    #: was filtered to the caller's organisations — "no diagnoses" and "none
    #: you may see" are different answers and the clinician must be told
    #: which. Dropping this sent someone hunting a bug in 2026-10-23.
    diagnosis_scope_note: str | None = None


# --------------------------------------------------------------------------
# plan panel
# --------------------------------------------------------------------------

PlanItemState = Literal["done", "planned", "overdue", "cancelled"]


@dataclass(frozen=True)
class PlanItem:
    transaction_guid: str | None
    title: str
    state: PlanItemState
    concept_guid: str | None = None
    scheduled_at: str | None = None
    performed_at: str | None = None
    service_request_guid: str | None = None
    #: The VISIT (#820). This, not `transaction_guid`, is what links a plan
    #: item to its observations — see correction 4 in the module docstring.
    plan_activity_guid: str | None = None


@dataclass(frozen=True)
class PlanSummary:
    #: None for a plan read from a ServiceRequest (`source="service_request"`).
    #: Production held ZERO CarePlans on 2026-11-12 while 28 ServiceRequests
    #: each carried a frozen PlanDefinition, so the order IS the plan there.
    care_plan_guid: str | None
    care_plan_title: str | None = None
    plan_definition_name: str | None = None
    plan_definition_version: str | None = None
    #: Nullable in request.pdhc's own schema. When it is None there is no T+
    #: origin and every `t_from_start_seconds` under this plan is None.
    period_start: str | None = None
    period_end: str | None = None
    status: str | None = None
    items: tuple[PlanItem, ...] = ()
    #: "care_plan" or "service_request" — which request.pdhc object this is.
    source: str = "care_plan"
    service_request_guid: str | None = None
    #: When the order was placed (`ServiceRequest.created_at`). Shown as a
    #: fact about the order; NEVER a T+ origin — that is `period_start` only,
    #: and it is None on every production ServiceRequest.
    ordered_at: str | None = None


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class EventRow:
    observation_guid: str
    #: The identity. There is no `concept_guid` column on a CDR observation —
    #: see correction 1.
    code_canonical: str
    display_name: str
    value: Any
    unit: str | None
    effective_at: str
    source_cdr: str
    #: None means "no CarePlan period.start to measure from", which is every
    #: row today. The UI shows "—"; it must never substitute another date.
    t_from_start_seconds: int | None = None
    concept_guid: str | None = None
    transaction_guid: str | None = None
    care_plan_guid: str | None = None
    service_request_guid: str | None = None
    plan_activity_guid: str | None = None
    ingest_raw_guid: str | None = None
    provider_org: str | None = None
    has_series: bool = False
    series_count: int = 0


@dataclass(frozen=True)
class EventDetail:
    """Everything known about one observation, for the selection panel.

    The plan's step 18 requires a field that was not recorded to be SHOWN as
    "not recorded" rather than omitted, and says why: it makes the context
    gaps visible. Given that `care_plan_guid` is populated on 0 rows and
    `service_request_guid` on 7 of 1 949, this panel is the platform's honest
    instrument for how much provenance is actually captured. Keep every field
    even when it is None.
    """
    row: EventRow
    patient_org_guid: str | None = None
    provider_org_guid: str | None = None
    contract_guid: str | None = None
    plan_definition_name: str | None = None
    plan_definition_version: str | None = None
    reference_low: float | None = None
    reference_high: float | None = None
    interpretation: str | None = None
    method: str | None = None
    device: str | None = None
    ingested_at: str | None = None
    raw_fhir_observation: dict | None = None


@dataclass(frozen=True)
class EventsPage:
    rows: tuple[EventRow, ...] = ()
    next_cursor: str | None = None
    #: Sources that failed, by name. The plan's step 24 requires one slow or
    #: failing CDR to degrade its own panel and nothing else — so a caller
    #: must be able to tell "this patient has no more rows" from "cdr3 did not
    #: answer". An empty list is the only thing that means "complete".
    unavailable: tuple[str, ...] = ()


# --------------------------------------------------------------------------
# series
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SeriesPoint:
    observation_guid: str
    effective_at: str
    value: float
    unit: str | None = None
    reference_low: float | None = None
    reference_high: float | None = None
    transaction_guid: str | None = None


@dataclass(frozen=True)
class Series:
    """One concept's points, in ONE unit.

    Two observations belong to the same series when `code_canonical` matches
    AND the units are convertible (plan step 7). When they are not, the series
    SPLITS by unit rather than plotting incomparable numbers on one axis — so
    a response may legitimately carry several of these for one concept.

    `downsampled_from` is set when LTTB reduced the points; the panel must
    then say "showing N of M" rather than implying it has everything.
    """
    code_canonical: str
    display_name: str
    unit: str | None
    points: tuple[SeriesPoint, ...] = ()
    downsampled_from: int | None = None


@dataclass(frozen=True)
class DashboardPayload:
    patient_card: PatientCard
    plans: tuple[PlanSummary, ...] = ()
    events_page: EventsPage = field(default_factory=EventsPage)
    unavailable: tuple[str, ...] = ()
