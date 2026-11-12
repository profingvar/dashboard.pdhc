"""app/dashboard/limits.py — the MDR class IIa limit logic (operator 2026-11-12).

One test per verdict state, plus the boundaries. A state that is NOT judged
must never read as `inside`.
"""
from __future__ import annotations

import pytest

from app.dashboard import limits as L

P = "patient-1"


def _order(txs, *, status="active", patient=P, guid="sr-1", title="Diabetes", version="3"):
    return {"guid": guid, "status": status, "patient_guid": patient,
            "plan_definition_snapshot": {"title": title, "version": version,
                                         "activities": [{"guid": "act", "transactions": txs}]}}


def _tx(guid="tx-glu", lo="3.0", hi="10.0", unit=None, concept_unit="mmol/L"):
    return {"guid": guid, "range_min": lo, "range_max": hi, "unit": unit,
            "concept_unit_name": concept_unit, "concept_name": "B-glucos"}


def _lim(*orders):
    return L.limits_from_orders(orders, P)


# -- the in/out states ---------------------------------------------------------

@pytest.mark.parametrize("value,status", [
    (2.9, "below"), (3.0, "inside"), (6.5, "inside"), (10.0, "inside"), (10.1, "above")])
def test_bounds_are_INCLUSIVE(value, status):
    v = L.evaluate(value, "mmol/L", "tx-glu", _lim(_order([_tx()])))
    assert v.status == status
    assert v.outside == (status in ("below", "above"))


def test_the_limit_carries_its_ORDER_and_PlanDefinition():
    v = L.evaluate(12.0, "mmol/L", "tx-glu", _lim(_order([_tx()])))
    assert v.limit.order_guid == "sr-1"
    assert v.limit.plan_definition_name == "Diabetes"
    assert v.limit.plan_definition_version == "3"


def test_a_one_sided_limit():
    v = L.evaluate(200.0, "mmHg", "tx-sys", _lim(_order([_tx("tx-sys", lo="", hi="160", concept_unit="mmHg")])))
    assert v.status == "above"
    v = L.evaluate(50.0, "mmHg", "tx-sys", _lim(_order([_tx("tx-sys", lo="", hi="160", concept_unit="mmHg")])))
    assert v.status == "inside"


def test_a_convertible_unit_is_compared_in_the_LIMITs_unit():
    v = L.evaluate(5.0, "mmol/l", "tx-glu", _lim(_order([_tx()])))
    assert v.status == "inside" and v.compared_value == 5.0


# -- the NOT-judged states -----------------------------------------------------

def test_no_limit_is_NOT_inside():
    v = L.evaluate(99.0, "mmol/L", "tx-other", _lim(_order([_tx()])))
    assert v.status == "no_limit" and not v.outside and v.reason


def test_an_unconvertible_unit_is_NOT_judged():
    """mg/dL vs mmol/L needs a molar mass; without one it is refused."""
    v = L.evaluate(180.0, "mg/dL", "tx-glu", _lim(_order([_tx()])))
    assert v.status == "unit_mismatch" and not v.outside


def test_a_value_with_no_unit_is_NOT_judged():
    v = L.evaluate(5.0, None, "tx-glu", _lim(_order([_tx()])))
    assert v.status == "unit_mismatch"


def test_text_is_NOT_judged():
    assert L.evaluate("positiv", None, "tx-glu", _lim(_order([_tx()]))).status == "not_numeric"
    assert L.evaluate(True, "mmol/L", "tx-glu", _lim(_order([_tx()]))).status == "not_numeric"


def test_orders_that_could_not_be_read_are_UNKNOWN_not_no_limit():
    v = L.evaluate(99.0, "mmol/L", "tx-glu", None)
    assert v.status == "unknown"


def test_two_orders_with_DIFFERENT_limits_are_a_conflict_not_a_choice():
    lim = _lim(_order([_tx(hi="10.0")], guid="sr-1"), _order([_tx(hi="8.0")], guid="sr-2"))
    v = L.evaluate(9.0, "mmol/L", "tx-glu", lim)
    assert v.status == "conflict" and not v.outside
    assert {c.order_guid for c in v.candidates} == {"sr-1", "sr-2"}


def test_two_orders_with_the_SAME_limit_agree():
    lim = _lim(_order([_tx()], guid="sr-1"), _order([_tx()], guid="sr-2"))
    assert L.evaluate(11.0, "mmol/L", "tx-glu", lim).status == "above"


@pytest.mark.parametrize("tx,why", [
    (_tx(lo="abc"), "not numeric"),
    (_tx(lo="12", hi="3"), "is above high"),
    (_tx(unit=None, concept_unit=None), "no unit"),
    (_tx(unit="mmHg", concept_unit="mmol/L"), "transaction says"),
    (_tx(lo="nan"), "not numeric"),
])
def test_an_unusable_limit_is_NOT_judged_and_says_why(tx, why):
    v = L.evaluate(5.0, "mmol/L", "tx-glu", _lim(_order([tx])))
    assert v.status == "invalid_limit" and why in v.reason


# -- whose limits apply ---------------------------------------------------------

def test_another_patients_order_never_supplies_a_limit():
    lim = L.limits_from_orders([_order([_tx()], patient="someone-else")], P)
    assert L.evaluate(99.0, "mmol/L", "tx-glu", lim).status == "no_limit"


@pytest.mark.parametrize("status", ["draft", "revoked"])
def test_a_draft_or_revoked_order_is_not_in_force(status):
    lim = _lim(_order([_tx()], status=status))
    assert L.evaluate(99.0, "mmol/L", "tx-glu", lim).status == "no_limit"


@pytest.mark.parametrize("status", ["active", "archived", "completed"])
def test_issued_orders_are_in_force(status):
    assert L.evaluate(99.0, "mmol/L", "tx-glu", _lim(_order([_tx()], status=status))).status == "above"


def test_wire_carries_everything_the_page_shows():
    w = L.wire(L.evaluate(12.0, "mmol/L", "tx-glu", _lim(_order([_tx()]))))
    assert w["status"] == "above" and w["outside"] is True
    assert (w["low"], w["high"], w["unit"], w["orderGuid"]) == (3.0, 10.0, "mmol/L", "sr-1")
