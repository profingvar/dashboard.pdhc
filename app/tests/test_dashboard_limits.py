"""app/dashboard/limits.py — the MDR class IIa limit logic.

Operator decisions 2026-11-12: limits SHALL be indicated (MDR IIa); the limit
is the one SET IN THE PATIENT'S CAREPLAN — its goal for the measurement first,
else the range in the CarePlan's PlanDefinition. Orders are not a source.
One test per state; a state that is NOT judged must never read as `inside`.
"""
from __future__ import annotations

import pytest

from app.dashboard import limits as L

P = "patient-1"
GLU = "concept-glu"
DIA = "concept-dia"


def _tx(guid="tx-glu", concept=GLU, lo="3.0", hi="10.0", unit=None, concept_unit="mmol/L"):
    return {"guid": guid, "concept_guid": concept, "range_min": lo, "range_max": hi,
            "unit": unit, "concept_unit_name": concept_unit}


def _cp(txs=None, goals=None, *, status="active", patient=P, guid="cp-1", title="Diabetes", version="3"):
    return {"care_plan_guid": guid, "status": status, "patient_guid": patient,
            "goals": goals or [],
            "plan_definition_snapshot": {"title": title, "version": version,
                                         "activities": [{"guid": "act", "transactions": txs or [_tx()]}]}}


def _goal(concept=GLU, **kw):
    g = {"concept_guid": concept, "concept_name": "B-glucos"}
    g.update(kw)
    return g


def _lim(*cps):
    return L.limits_from_care_plans(cps, P)


def _ev(value, lim, unit="mmol/L", tx="tx-glu", concept=None):
    return L.evaluate(value, unit, tx, lim, concept_guid=concept)


# -- the PlanDefinition range (no goal) ------------------------------------------

@pytest.mark.parametrize("value,status", [
    (2.9, "below"), (3.0, "inside"), (6.5, "inside"), (10.0, "inside"), (10.1, "above")])
def test_range_bounds_are_INCLUSIVE(value, status):
    v = _ev(value, _lim(_cp()))
    assert v.status == status and v.outside == (status in ("below", "above"))
    assert v.limit.source_kind == "care_plan_range"


def test_the_limit_names_its_CarePlan_and_PlanDefinition():
    v = _ev(12.0, _lim(_cp()))
    assert (v.limit.care_plan_guid, v.limit.plan_definition_name, v.limit.plan_definition_version) == \
        ("cp-1", "Diabetes", "3")


# -- the patient's GOAL wins -------------------------------------------------------

def test_a_GOAL_overrides_the_PlanDefinition_range():
    """The clinician tightened this patient's upper limit to 8."""
    lim = _lim(_cp(goals=[_goal(target_type="range", target_range_low=4.0, target_range_high=8.0)]))
    v = _ev(9.0, lim)
    assert v.status == "above" and v.limit.source_kind == "care_plan_goal"
    assert (v.limit.low, v.limit.high) == (4.0, 8.0)


def test_the_goal_is_matched_through_the_transactions_concept():
    """The observation carries a transaction; the goal is keyed by concept."""
    lim = _lim(_cp(goals=[_goal(target_type="quantity", target_operator="<=", target_quantity=8.0)]))
    assert _ev(8.5, lim).status == "above"           # tx-glu -> concept-glu -> goal


@pytest.mark.parametrize("op,value,status", [
    ("<=", 90.0, "inside"), ("<", 90.0, "above"), ("<", 89.9, "inside"),
    (">=", 60.0, "inside"), (">", 60.0, "below"), (">", 60.1, "inside")])
def test_operators_and_INCLUSIVITY(op, value, status):
    txs = [_tx("tx-dia", DIA, lo="", hi="", concept_unit="mmHg")]
    q = 90.0 if op in ("<=", "<") else 60.0
    lim = _lim(_cp(txs, [_goal(DIA, target_type="quantity", target_operator=op, target_quantity=q)]))
    assert _ev(value, lim, unit="mmHg", tx="tx-dia").status == status


def test_two_one_sided_goals_combine_into_a_range():
    txs = [_tx("tx-dia", DIA, lo="", hi="", concept_unit="mmHg")]
    lim = _lim(_cp(txs, [_goal(DIA, target_type="quantity", target_operator=">=", target_quantity=60),
                         _goal(DIA, target_type="quantity", target_operator="<", target_quantity=90)]))
    assert [_ev(v, lim, "mmHg", "tx-dia").status for v in (59, 60, 89, 90)] == \
        ["below", "inside", "inside", "above"]


def test_an_EQUALS_or_categorical_goal_is_a_target_not_a_limit_and_the_range_applies():
    lim = _lim(_cp(goals=[_goal(target_type="quantity", target_operator="=", target_quantity=6.0),
                          _goal(target_type="categorical", target_categorical_text="stabil")]))
    v = _ev(11.0, lim)
    assert v.status == "above" and v.limit.source_kind == "care_plan_range"


def test_the_goal_unit_falls_back_to_the_concepts_unit():
    lim = _lim(_cp(goals=[_goal(target_type="quantity", target_operator="<=", target_quantity=8.0)]))
    assert _ev(9.0, lim).limit.unit == "mmol/L"


def test_the_goals_own_unit_is_used_and_converted():
    lim = _lim(_cp(goals=[_goal(target_type="quantity", target_operator="<=", target_quantity=8.0,
                                target_unit="mmol/l")]))
    assert _ev(7.5, lim, unit="mmol/L").status == "inside"


# -- NOT judged ---------------------------------------------------------------------

def test_no_limit_is_NOT_inside():
    v = _ev(99.0, _lim(_cp()), tx="tx-other", concept="concept-other")
    assert v.status == "no_limit" and not v.outside and v.reason


def test_an_unconvertible_unit_is_NOT_judged():
    assert _ev(180.0, _lim(_cp()), unit="mg/dL").status == "unit_mismatch"


def test_a_value_with_no_unit_is_NOT_judged():
    assert _ev(5.0, _lim(_cp()), unit=None).status == "unit_mismatch"


def test_text_is_NOT_judged():
    assert L.evaluate("positiv", None, "tx-glu", _lim(_cp())).status == "not_numeric"
    assert L.evaluate(True, "mmol/L", "tx-glu", _lim(_cp())).status == "not_numeric"


def test_care_plans_that_could_not_be_read_are_UNKNOWN_not_no_limit():
    assert L.evaluate(99.0, "mmol/L", "tx-glu", None).status == "unknown"


def test_two_care_plans_with_DIFFERENT_limits_are_a_conflict():
    lim = _lim(_cp(guid="cp-1"), _cp([_tx(hi="8.0")], guid="cp-2"))
    v = _ev(9.0, lim)
    assert v.status == "conflict" and not v.outside
    assert {c.care_plan_guid for c in v.candidates} == {"cp-1", "cp-2"}


def test_two_care_plans_with_the_SAME_limit_agree():
    assert _ev(11.0, _lim(_cp(guid="cp-1"), _cp(guid="cp-2"))).status == "above"


def test_a_goal_in_one_plan_and_the_range_in_another_DIFFERENT_is_a_conflict():
    lim = _lim(_cp(guid="cp-1", goals=[_goal(target_type="range", target_range_low=4, target_range_high=8)]),
               _cp(guid="cp-2"))
    assert _ev(9.0, lim).status == "conflict"


@pytest.mark.parametrize("cp,why", [
    (_cp([_tx(lo="abc")]), "not numeric"),
    (_cp([_tx(lo="12", hi="3")]), "is above high"),
    (_cp([_tx(unit=None, concept_unit=None)]), "no unit"),
    (_cp([_tx(unit="mmHg", concept_unit="mmol/L")]), "transaction says"),
    (_cp([_tx(lo="nan")]), "not numeric"),
    (_cp(goals=[_goal(target_type="range", target_range_low=9, target_range_high=4)]), "is above high"),
    (_cp(goals=[_goal(target_type="quantity", target_operator="<=", target_quantity=8),
                _goal(target_type="quantity", target_operator="<=", target_quantity=9)]), "two different upper"),
    (_cp(goals=[_goal(target_type="quantity", target_operator=">=", target_quantity=4),
                _goal(target_type="quantity", target_operator=">=", target_quantity=5)]), "two different lower"),
    (_cp(goals=[_goal(target_type="quantity", target_operator="<=", target_quantity="x")]), "not numeric"),
    (_cp([_tx(unit=None, concept_unit=None)],
         [_goal(target_type="quantity", target_operator="<=", target_quantity=8)]), "has no unit"),
])
def test_an_unusable_limit_is_NOT_judged_and_says_why(cp, why):
    v = _ev(5.0, _lim(cp))
    assert v.status == "invalid_limit" and why in v.reason, v


# -- whose limits apply ----------------------------------------------------------------

def test_another_patients_care_plan_never_supplies_a_limit():
    assert _ev(99.0, L.limits_from_care_plans([_cp(patient="someone-else")], P)).status == "no_limit"


@pytest.mark.parametrize("status", ["draft", "revoked", "entered-in-error"])
def test_a_care_plan_not_in_force_supplies_no_limit(status):
    assert _ev(99.0, _lim(_cp(status=status))).status == "no_limit"


@pytest.mark.parametrize("status", ["active", "on-hold", "completed"])
def test_care_plans_in_force_supply_limits(status):
    assert _ev(99.0, _lim(_cp(status=status))).status == "above"


def test_limited_transactions_cover_ranges_and_goal_concepts():
    txs = [_tx(), _tx("tx-dia", DIA, lo="", hi="", concept_unit="mmHg"), _tx("tx-x", "c-x", lo="", hi="")]
    lim = _lim(_cp(txs, [_goal(DIA, target_type="quantity", target_operator="<", target_quantity=90)]))
    assert lim.limited_transactions() == {"tx-glu", "tx-dia"}


def test_wire_carries_everything_the_page_shows():
    w = L.wire(_ev(12.0, _lim(_cp())))
    assert w["status"] == "above" and w["outside"] is True
    assert (w["low"], w["high"], w["unit"], w["carePlanGuid"], w["sourceKind"]) == \
        (3.0, 10.0, "mmol/L", "cp-1", "care_plan_range")
    assert w["lowInclusive"] is True and w["highInclusive"] is True


def test_a_goal_is_found_even_when_the_codes_concept_differs_from_the_plans():
    """Found 2026-11-12: with the value's code naming another concept id than
    the CarePlan's transaction, the goal was missed and the range used
    silently."""
    lim = _lim(_cp(goals=[_goal(target_type="quantity", target_operator="<=", target_quantity=8.0)]))
    v = _ev(9.0, lim, concept="some-other-concept-id")
    assert v.status == "above" and v.limit.source_kind == "care_plan_goal"


def test_two_different_goals_reached_by_the_two_concept_ids_are_a_conflict():
    lim = _lim(_cp(goals=[_goal(target_type="quantity", target_operator="<=", target_quantity=8.0),
                          _goal("other-id", target_type="quantity", target_operator="<=",
                                target_quantity=9.0, target_unit="mmol/L")]))
    assert _ev(8.5, lim, concept="other-id").status == "conflict"
