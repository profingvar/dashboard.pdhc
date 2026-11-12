"""Limits on a patient's values — the dashboard's MDR class IIa logic.

OPERATOR DECISIONS 2026-11-12:
  1. the dashboard SHALL indicate limits, and dashboard.pdhc will be marked
     MDR class IIa (Rule 11);
  2. the limit is the one SET IN THE PATIENT'S CAREPLAN: the CarePlan's GOAL
     for the measurement first, and only where there is none, the range in
     the CarePlan's frozen PlanDefinition. Limits in ServiceRequest (order)
     snapshots are NOT used.

Every judgement of "outside the limit" is made HERE, in pure functions with no
I/O. The browser never compares a value to a limit; it renders what this
module returns.

THE TWO SOURCES, both in request.pdhc's CarePlan (`GET /careplans?patient_guid=`):

  care_plan_goal    `CarePlan.goals` — inherited from the PlanDefinition's
                    goals at create time and editable PER PATIENT (the
                    clinician tightens thresholds for this patient). Shape is
                    plan.pdhc's `PlanDefinitionGoal.to_dict()`:
                      target_type "range"    -> target_range_low/high (inclusive)
                      target_type "quantity" -> target_quantity with
                                                target_operator >= > <= <
                      "=" or "categorical"   -> a TARGET, not a limit: unused
                    keyed by `concept_guid`; unit `target_unit`, else the
                    concept's unit in the CarePlan's snapshot.
  care_plan_range   `plan_definition_snapshot.activities[].transactions[]
                    .range_min/range_max` — the template's range, frozen at
                    create time, the same for every patient on the plan.
                    Used only for a measurement with no usable goal.

Measured 2026-11-12: production has ZERO CarePlans, so every verdict today is
`no_limit`.

THE STATES. Only `below` and `above` mark a value. Everything else that is not
`inside` is NOT JUDGED and says why — "no limit" is not "inside", and "could
not judge" is not "no limit":

    inside / below / above   judged
    no_limit                 no CarePlan of this patient sets a limit here
    unit_mismatch            the value's unit does not convert to the limit's
    conflict                 two CarePlans set DIFFERENT limits — both reported
    invalid_limit            the limit is unusable (non-numeric, no unit,
                             low > high, contradictory goals)
    not_numeric              the value is text
    unknown                  the CarePlans could not be read
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Mapping

from app.dashboard import units

Status = Literal["inside", "below", "above", "no_limit", "unit_mismatch",
                 "conflict", "invalid_limit", "not_numeric", "unknown"]
SourceKind = Literal["care_plan_goal", "care_plan_range"]

#: CarePlans whose limits apply (FHIR CarePlan.status). `draft` was never put
#: in force; `revoked` and `entered-in-error` were withdrawn.
APPLICABLE_CARE_PLAN_STATUSES = frozenset({"active", "on-hold", "completed"})

OUTSIDE = frozenset({"below", "above"})

_LOWER_OPS = {">=": True, ">": False}     # operator -> inclusive
_UPPER_OPS = {"<=": True, "<": False}


@dataclass(frozen=True)
class Limit:
    """One CarePlan's limit on one measurement, with where it came from."""
    low: float | None
    high: float | None
    unit: str | None
    source_kind: SourceKind
    care_plan_guid: str | None
    low_inclusive: bool = True
    high_inclusive: bool = True
    plan_definition_name: str | None = None
    plan_definition_version: str | None = None
    #: The goal's own text, when the limit is a goal.
    description: str | None = None
    #: Set when the limit is unusable; the reason, in words.
    invalid: str | None = None


@dataclass(frozen=True)
class Verdict:
    status: Status
    limit: Limit | None = None
    #: The value expressed in the LIMIT's unit.
    compared_value: float | None = None
    reason: str | None = None
    candidates: tuple[Limit, ...] = field(default_factory=tuple)

    @property
    def outside(self) -> bool:
        return self.status in OUTSIDE


def _number(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        raise ValueError("a boolean is not a limit")
    v = float(raw)
    if math.isnan(v) or math.isinf(v):
        raise ValueError("not a finite number")
    return v


def _transactions(snapshot: Mapping) -> list[Mapping]:
    out = []
    for act in (snapshot.get("activities") or ()) if isinstance(snapshot, Mapping) else ():
        if isinstance(act, Mapping):
            out.extend(t for t in (act.get("transactions") or ()) if isinstance(t, Mapping))
    return out


def _concept_units(snapshot: Mapping) -> dict[str, str]:
    """concept_guid -> the concept's unit, from the snapshot's transactions."""
    out: dict[str, str] = {}
    for tx in _transactions(snapshot):
        cg = tx.get("concept_guid")
        unit = (tx.get("unit") or "").strip() or (tx.get("concept_unit_name") or "").strip()
        if cg and unit:
            out.setdefault(str(cg), unit)
    return out


def _goal_limit(goals: list[Mapping], unit_hint: str | None, base: dict) -> Limit | None:
    """All of ONE CarePlan's goals for ONE concept, combined into a limit.

    None when the goals set no limit at all (only targets or categories) —
    the caller then falls back to the PlanDefinition range.
    """
    low = high = None
    low_inc = high_inc = True
    units_seen: set[str] = set()
    descriptions = []
    try:
        for g in goals:
            ttype = (g.get("target_type") or "").strip().lower()
            op = (g.get("target_operator") or g.get("target_comparator") or "").strip()
            gl = gh = None
            gl_inc = gh_inc = True
            if ttype == "range":
                gl, gh = _number(g.get("target_range_low")), _number(g.get("target_range_high"))
            elif op in _LOWER_OPS:
                gl, gl_inc = _number(g.get("target_quantity", g.get("target_value"))), _LOWER_OPS[op]
            elif op in _UPPER_OPS:
                gh, gh_inc = _number(g.get("target_quantity", g.get("target_value"))), _UPPER_OPS[op]
            else:
                continue          # "=" / categorical / no operator: a target, not a limit
            if gl is None and gh is None:
                continue
            u = (g.get("target_unit") or "").strip()
            if u:
                units_seen.add(u)
            if g.get("description") or g.get("concept_name"):
                descriptions.append(str(g.get("description") or g.get("concept_name")))
            if gl is not None:
                if low is not None and gl != low:
                    return Limit(low=None, high=None, unit=None, invalid=
                                 f"the CarePlan has two different lower goals ({low}, {gl})", **base)
                low, low_inc = gl, gl_inc
            if gh is not None:
                if high is not None and gh != high:
                    return Limit(low=None, high=None, unit=None, invalid=
                                 f"the CarePlan has two different upper goals ({high}, {gh})", **base)
                high, high_inc = gh, gh_inc
    except (TypeError, ValueError) as exc:
        return Limit(low=None, high=None, unit=None, invalid=f"a goal is not numeric ({exc})", **base)

    if low is None and high is None:
        return None
    desc = "; ".join(dict.fromkeys(descriptions)) or None
    if len({units.normalise_unit(u) or u for u in units_seen}) > 1:
        return Limit(low=low, high=high, unit=None, description=desc,
                     invalid=f"the goals use different units ({', '.join(sorted(units_seen))})", **base)
    unit = next(iter(units_seen), None) or unit_hint
    if unit is None:
        return Limit(low=low, high=high, unit=None, description=desc,
                     invalid="the goal has no unit and its concept has none", **base)
    if low is not None and high is not None and low > high:
        return Limit(low=low, high=high, unit=unit, description=desc,
                     invalid=f"low {low} is above high {high}", **base)
    return Limit(low=low, high=high, unit=unit, low_inclusive=low_inc,
                 high_inclusive=high_inc, description=desc, **base)


def _range_limit(tx: Mapping, base: dict) -> Limit | None:
    raw_low, raw_high = tx.get("range_min"), tx.get("range_max")
    if raw_low in (None, "") and raw_high in (None, ""):
        return None
    tx_unit = (tx.get("unit") or "").strip() or None
    concept_unit = (tx.get("concept_unit_name") or "").strip() or None
    try:
        low, high = _number(raw_low), _number(raw_high)
    except (TypeError, ValueError):
        return Limit(low=None, high=None, unit=tx_unit or concept_unit,
                     invalid=f"limit is not numeric ({raw_low!r}, {raw_high!r})", **base)
    if tx_unit and concept_unit and tx_unit != concept_unit \
            and units.normalise_unit(tx_unit) != units.normalise_unit(concept_unit):
        return Limit(low=low, high=high, unit=None,
                     invalid=f"the transaction says {tx_unit!r} and its concept "
                             f"says {concept_unit!r}", **base)
    unit = tx_unit or concept_unit
    if unit is None:
        return Limit(low=low, high=high, unit=None, invalid="the limit has no unit", **base)
    if low is not None and high is not None and low > high:
        return Limit(low=low, high=high, unit=unit,
                     invalid=f"low {low} is above high {high}", **base)
    return Limit(low=low, high=high, unit=unit, **base)


@dataclass(frozen=True)
class PatientLimits:
    """Every applicable CarePlan's limits for one patient, ready to look up."""
    #: (care_plan_guid, concept_guid) -> goal Limit
    goals: Mapping[tuple[str, str], Limit]
    #: (care_plan_guid, transaction_guid) -> range Limit
    ranges: Mapping[tuple[str, str], Limit]
    #: care_plan_guid -> {transaction_guid: concept_guid}
    tx_concepts: Mapping[str, Mapping[str, str]]
    care_plans: tuple[str, ...]

    def candidates(self, transaction_guid: str | None,
                   concept_guid: str | None) -> list[Limit]:
        """One limit per CarePlan that governs this measurement: its GOAL for
        the concept if it has one, else its PlanDefinition RANGE for the
        transaction."""
        out = []
        for cp in self.care_plans:
            # The concept is looked up BOTH ways — the CarePlan's own mapping
            # of the transaction, and the concept in the value's code. Taking
            # only one missed a goal when the two ids differed and fell back
            # to the PlanDefinition range SILENTLY (found 2026-11-12). Every
            # goal found is a candidate; two different ones become a conflict
            # in `evaluate`, never a choice.
            concepts = {c for c in (self.tx_concepts.get(cp, {}).get(transaction_guid or ""),
                                    concept_guid) if c}
            found = [self.goals[(cp, c)] for c in sorted(concepts) if (cp, c) in self.goals]
            if found:
                out.extend(found)
                continue
            rng = self.ranges.get((cp, transaction_guid)) if transaction_guid else None
            if rng is not None:
                out.append(rng)
        return out

    def limited_transactions(self) -> set[str]:
        """Transactions that can carry an outside value: those with a range,
        and those whose concept has a goal in the same CarePlan."""
        out = {tx for (_cp, tx), lim in self.ranges.items() if not lim.invalid}
        for (cp, concept), lim in self.goals.items():
            if lim.invalid:
                continue
            out |= {tx for tx, c in self.tx_concepts.get(cp, {}).items() if c == concept}
        return out


def limits_from_care_plans(care_plans: Iterable[Mapping], patient_guid: str) -> PatientLimits:
    """This patient's applicable CarePlans -> their goals and ranges.

    The patient is half of the key: a transaction guid is shared by every
    patient on a PlanDefinition, and a CarePlan for someone else never
    supplies a limit here.
    """
    goals: dict[tuple[str, str], Limit] = {}
    ranges: dict[tuple[str, str], Limit] = {}
    tx_concepts: dict[str, dict[str, str]] = {}
    plans: list[str] = []
    for cp in care_plans or ():
        if not isinstance(cp, Mapping) or cp.get("patient_guid") != patient_guid:
            continue
        if (cp.get("status") or "").lower() not in APPLICABLE_CARE_PLAN_STATUSES:
            continue
        cp_guid = str(cp.get("care_plan_guid") or cp.get("guid") or "")
        if not cp_guid:
            continue
        plans.append(cp_guid)
        snap = cp.get("plan_definition_snapshot") or {}
        snap = snap if isinstance(snap, Mapping) else {}
        pd_version = snap.get("version")
        base = dict(care_plan_guid=cp_guid,
                    plan_definition_name=snap.get("title") or snap.get("name"),
                    plan_definition_version=str(pd_version) if pd_version else None)
        tx_concepts[cp_guid] = {str(t["guid"]): str(t.get("concept_guid") or "")
                                for t in _transactions(snap) if t.get("guid")}
        for tx in _transactions(snap):
            if tx.get("guid"):
                lim = _range_limit(tx, dict(base, source_kind="care_plan_range"))
                if lim is not None:
                    ranges[(cp_guid, str(tx["guid"]))] = lim
        by_concept: dict[str, list[Mapping]] = {}
        for g in cp.get("goals") or ():
            if isinstance(g, Mapping) and g.get("concept_guid"):
                by_concept.setdefault(str(g["concept_guid"]), []).append(g)
        unit_of = _concept_units(snap)
        for concept, gs in by_concept.items():
            lim = _goal_limit(gs, unit_of.get(concept), dict(base, source_kind="care_plan_goal"))
            if lim is not None:
                goals[(cp_guid, concept)] = lim
    return PatientLimits(goals=goals, ranges=ranges, tx_concepts=tx_concepts,
                         care_plans=tuple(plans))


def _same(a: Limit, b: Limit) -> bool:
    return (a.low, a.high, a.low_inclusive, a.high_inclusive,
            units.normalise_unit(a.unit) or a.unit) == \
           (b.low, b.high, b.low_inclusive, b.high_inclusive,
            units.normalise_unit(b.unit) or b.unit)


def evaluate(value: Any, value_unit: str | None, transaction_guid: str | None,
             limits: PatientLimits | None, *, code_canonical: str | None = None,
             concept_guid: str | None = None) -> Verdict:
    """The verdict on ONE value. `limits=None` means the CarePlans were not read."""
    if limits is None:
        return Verdict("unknown", reason="the patient's care plans could not be read")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return Verdict("not_numeric", reason="a text value is not compared")
    candidates = limits.candidates(transaction_guid, concept_guid)
    if not candidates:
        return Verdict("no_limit", reason="no care plan for this patient sets a "
                                          "limit on this measurement")
    first = candidates[0]
    distinct = [first] + [c for c in candidates[1:] if not _same(c, first)]
    if len(distinct) > 1:
        return Verdict("conflict", candidates=tuple(distinct),
                       reason=f"{len(distinct)} care plans set different limits")
    lim = first
    if lim.invalid:
        return Verdict("invalid_limit", limit=lim, reason=lim.invalid)

    v = float(value)
    if value_unit != lim.unit:
        substance = units.substance_from_code_canonical(code_canonical) if code_canonical else None
        try:
            v = units.convert(v, value_unit, lim.unit, substance=substance)
        except (units.UnitConversionRefused, TypeError, KeyError) as exc:
            return Verdict("unit_mismatch", limit=lim,
                           reason=f"value in {value_unit!r}, limit in {lim.unit!r}: {exc}")
    if lim.low is not None and (v < lim.low or (v == lim.low and not lim.low_inclusive)):
        return Verdict("below", limit=lim, compared_value=v)
    if lim.high is not None and (v > lim.high or (v == lim.high and not lim.high_inclusive)):
        return Verdict("above", limit=lim, compared_value=v)
    return Verdict("inside", limit=lim, compared_value=v)


def _wire_limit(lim: Limit) -> dict:
    return {"low": lim.low, "high": lim.high, "unit": lim.unit,
            "lowInclusive": lim.low_inclusive, "highInclusive": lim.high_inclusive,
            "sourceKind": lim.source_kind, "carePlanGuid": lim.care_plan_guid,
            "planDefinitionName": lim.plan_definition_name,
            "planDefinitionVersion": lim.plan_definition_version,
            "description": lim.description}


def wire(verdict: Verdict) -> dict:
    """The verdict as the page receives it — rendered, never recomputed."""
    base = _wire_limit(verdict.limit) if verdict.limit else {
        k: None for k in ("low", "high", "unit", "lowInclusive", "highInclusive",
                          "sourceKind", "carePlanGuid", "planDefinitionName",
                          "planDefinitionVersion", "description")}
    return dict(base, status=verdict.status, outside=verdict.outside,
                comparedValue=verdict.compared_value, reason=verdict.reason,
                candidates=[_wire_limit(c) for c in verdict.candidates])
