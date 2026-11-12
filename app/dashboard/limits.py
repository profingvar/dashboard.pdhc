"""Plan limits on a patient's values — the dashboard's MDR class IIa logic.

OPERATOR DECISION 2026-11-12: the dashboard SHALL indicate limits, and
dashboard.pdhc will be marked MDR class IIa (Rule 11). Every judgement of
"outside the limit" is made HERE, in pure functions with no I/O, so it can be
read, tested and traced in one place. The browser never compares a value to
a limit; it renders the verdict this module returns.

WHERE A LIMIT COMES FROM. The only limits on the platform are the thresholds
frozen into each ServiceRequest's PlanDefinition snapshot
(`activities[].transactions[].range_min / range_max`), set when the order was
authored, under medical responsibility, for THIS patient. Measured
2026-11-12: cdr1 observations carry no referenceRange and no interpretation
(0 of 1 949), so there is nothing else. A limit is therefore always shown with
its ORDER and PlanDefinition, never as an anonymous "normal range".

THE STATES. Every verdict is one of these, and the difference between them is
the point — "no limit" is not "inside", and "could not judge" is not "no
limit":

    inside            low <= value <= high (bounds INCLUSIVE)
    below / above     strictly outside
    no_limit          no order for this patient sets a limit on this
                      transaction
    unit_mismatch     a limit exists but the value's unit cannot be safely
                      converted to the limit's unit — NOT judged
    conflict          two orders for this patient set DIFFERENT limits on the
                      same transaction — NOT judged; both are reported
    invalid_limit     the order's limit is unusable (non-numeric, low > high,
                      no unit) — NOT judged
    not_numeric       the value is text — NOT judged
    unknown           the orders could not be read — NOT judged, and not
                      "no limit" either

Only `below` and `above` mark a value. Everything that is NOT JUDGED says why.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Mapping

from app.dashboard import units

Status = Literal["inside", "below", "above", "no_limit", "unit_mismatch",
                 "conflict", "invalid_limit", "not_numeric", "unknown"]

#: Orders whose limits apply. `draft` was never issued and `revoked` was
#: withdrawn by the clinician; their thresholds are not in force.
APPLICABLE_ORDER_STATUSES = frozenset({"active", "archived", "completed"})

OUTSIDE = frozenset({"below", "above"})


@dataclass(frozen=True)
class Limit:
    """One order's limit on one transaction, with where it came from."""
    transaction_guid: str
    low: float | None
    high: float | None
    unit: str | None
    order_guid: str | None
    plan_definition_name: str | None = None
    plan_definition_version: str | None = None
    #: Set when the order's limit is unusable; the reason, in words.
    invalid: str | None = None


@dataclass(frozen=True)
class Verdict:
    status: Status
    limit: Limit | None = None
    #: The value expressed in the LIMIT's unit, when a conversion was needed.
    compared_value: float | None = None
    #: Why the value was not judged, for every status that is not judged.
    reason: str | None = None
    #: For `conflict`: every differing limit.
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


def _one_limit(tx: Mapping, order: Mapping) -> Limit | None:
    """The limit one transaction entry carries, or None when it sets none."""
    raw_low, raw_high = tx.get("range_min"), tx.get("range_max")
    if raw_low in (None, "") and raw_high in (None, ""):
        return None
    snap = order.get("plan_definition_snapshot") or {}
    pd_name = (snap.get("title") or snap.get("name")) if isinstance(snap, Mapping) else None
    pd_version = snap.get("version") if isinstance(snap, Mapping) else None
    tx_unit = (tx.get("unit") or "").strip() or None
    concept_unit = (tx.get("concept_unit_name") or "").strip() or None
    base = dict(transaction_guid=str(tx.get("guid")),
                order_guid=str(order.get("guid")) if order.get("guid") else None,
                plan_definition_name=pd_name,
                plan_definition_version=str(pd_version) if pd_version else None)
    try:
        low, high = _number(raw_low), _number(raw_high)
    except (TypeError, ValueError):
        return Limit(low=None, high=None, unit=tx_unit or concept_unit,
                     invalid=f"limit is not numeric ({raw_low!r}, {raw_high!r})", **base)
    if tx_unit and concept_unit and units.normalise_unit(tx_unit) != units.normalise_unit(concept_unit) \
            and tx_unit != concept_unit:
        return Limit(low=low, high=high, unit=None,
                     invalid=f"the transaction says {tx_unit!r} and its concept "
                             f"says {concept_unit!r}", **base)
    unit = tx_unit or concept_unit
    if unit is None:
        return Limit(low=low, high=high, unit=None,
                     invalid="the limit has no unit", **base)
    if low is not None and high is not None and low > high:
        return Limit(low=low, high=high, unit=unit,
                     invalid=f"low {low} is above high {high}", **base)
    return Limit(low=low, high=high, unit=unit, **base)


def limits_from_orders(orders: Iterable[Mapping], patient_guid: str) -> dict[str, list[Limit]]:
    """transaction_guid -> every limit this patient's orders set on it.

    The transaction guid is a PlanDefinition-level id shared by every patient
    on the plan, so the PATIENT is half of the key: an order for someone else
    never supplies a limit here.
    """
    out: dict[str, list[Limit]] = {}
    for order in orders or ():
        if not isinstance(order, Mapping) or order.get("patient_guid") != patient_guid:
            continue
        if (order.get("status") or "").lower() not in APPLICABLE_ORDER_STATUSES:
            continue
        snap = order.get("plan_definition_snapshot") or {}
        if not isinstance(snap, Mapping):
            continue
        for act in snap.get("activities") or ():
            if not isinstance(act, Mapping):
                continue
            for tx in act.get("transactions") or ():
                if not isinstance(tx, Mapping) or not tx.get("guid"):
                    continue
                lim = _one_limit(tx, order)
                if lim is not None:
                    out.setdefault(lim.transaction_guid, []).append(lim)
    return out


def _same(a: Limit, b: Limit) -> bool:
    return (a.low, a.high, units.normalise_unit(a.unit) or a.unit) == \
           (b.low, b.high, units.normalise_unit(b.unit) or b.unit)


def evaluate(value: Any, value_unit: str | None, transaction_guid: str | None,
             limits: Mapping[str, list[Limit]] | None, *,
             code_canonical: str | None = None) -> Verdict:
    """The verdict on ONE value. `limits=None` means the orders were not read."""
    if limits is None:
        return Verdict("unknown", reason="the patient's orders could not be read")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return Verdict("not_numeric", reason="a text value is not compared")
    candidates = limits.get(transaction_guid or "") or []
    if not candidates:
        return Verdict("no_limit", reason="no order for this patient sets a limit "
                                          "on this measurement")
    first = candidates[0]
    distinct = [first] + [c for c in candidates[1:] if not _same(c, first)]
    if len(distinct) > 1:
        return Verdict("conflict", candidates=tuple(distinct),
                       reason=f"{len(distinct)} orders set different limits")
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
    if lim.low is not None and v < lim.low:
        return Verdict("below", limit=lim, compared_value=v)
    if lim.high is not None and v > lim.high:
        return Verdict("above", limit=lim, compared_value=v)
    return Verdict("inside", limit=lim, compared_value=v)


def wire(verdict: Verdict) -> dict:
    """The verdict as the page receives it — rendered, never recomputed."""
    lim = verdict.limit
    return {
        "status": verdict.status,
        "outside": verdict.outside,
        "low": lim.low if lim else None,
        "high": lim.high if lim else None,
        "unit": lim.unit if lim else None,
        "orderGuid": lim.order_guid if lim else None,
        "planDefinitionName": lim.plan_definition_name if lim else None,
        "planDefinitionVersion": lim.plan_definition_version if lim else None,
        "comparedValue": verdict.compared_value,
        "reason": verdict.reason,
        "candidates": [{"low": c.low, "high": c.high, "unit": c.unit,
                        "orderGuid": c.order_guid} for c in verdict.candidates],
    }
