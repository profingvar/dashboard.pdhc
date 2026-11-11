"""The HbA1c affine bridge (operator decision 2026-11-11 — one chart).

The numbers here are asserted NUMERICALLY, not merely "does not raise". A
conversion that runs and returns a wrong number is the failure mode this
module exists to prevent, and it looks exactly like success.
"""
from __future__ import annotations

import pytest

from app.dashboard import hba1c, units


# --------------------------------------------------------------------------
# the equation
# --------------------------------------------------------------------------

@pytest.mark.parametrize("ngsp,ifcc", [
    (5.0, 31.1), (6.0, 42.1), (7.0, 53.0), (8.0, 63.9), (9.0, 74.9),
])
def test_the_textbook_pairs_both_ways(ngsp, ifcc):
    assert hba1c.ifcc_from_ngsp(ngsp) == pytest.approx(ifcc, abs=0.1)
    assert hba1c.ngsp_from_ifcc(ifcc) == pytest.approx(ngsp, abs=0.02)


def test_the_naive_RATIO_is_not_what_this_computes():
    """The trap, asserted as a number.

    1 mmol/mol = 0.1 % is a true statement about the units and a false one
    about HbA1c. 7 % is 53, not 70 — a 32 % overstatement, and plausible
    enough to pass review.
    """
    got = hba1c.ifcc_from_ngsp(7.0)
    assert got == pytest.approx(53.0, abs=0.1)
    assert got != pytest.approx(70.0, abs=1.0)


def test_the_two_directions_are_exact_inverses():
    """Derived from ONE pair of constants, so they cannot drift.

    The commonly quoted rounded form (2.15 / 10.929) disagrees with itself in
    the third significant figure; this is why the inverse is computed rather
    than written down twice.
    """
    for v in (2.2, 5.0, 7.0, 9.3, 14.0):
        assert hba1c.ngsp_from_ifcc(hba1c.ifcc_from_ngsp(v)) == pytest.approx(v)
    for v in (0.0, 20.0, 53.0, 97.0):
        assert hba1c.ifcc_from_ngsp(hba1c.ngsp_from_ifcc(v)) == pytest.approx(v)


def test_the_module_holds_exactly_two_constants():
    """A second pair of constants is how the directions drift apart."""
    assert hba1c.SLOPE == 0.09148
    assert hba1c.INTERCEPT == 2.152
    assert hba1c.MIN_NGSP_PERCENT == hba1c.INTERCEPT


# --------------------------------------------------------------------------
# what it refuses
# --------------------------------------------------------------------------

def test_below_the_intercept_is_refused_BY_NAME_not_clamped():
    with pytest.raises(hba1c.Hba1cOutOfRange) as e:
        hba1c.ifcc_from_ngsp(1.0)
    assert e.value.unit == "%"
    assert "negative" in e.value.reason


def test_a_negative_ifcc_is_refused():
    with pytest.raises(hba1c.Hba1cOutOfRange):
        hba1c.ngsp_from_ifcc(-1.0)


def test_there_is_deliberately_NO_upper_bound():
    """A chart that quietly omits the worst point is worse than one that
    shows it. 20 % is not physiological; it is also not ours to drop."""
    assert hba1c.ifcc_from_ngsp(20.0) == pytest.approx(195.1, abs=0.5)


def test_a_non_numeric_value_is_rejected_not_coerced():
    with pytest.raises(TypeError):
        hba1c.ifcc_from_ngsp("7.0")
    with pytest.raises(TypeError):
        hba1c.ifcc_from_ngsp(True)


def test_the_bridge_joins_only_its_own_pair():
    with pytest.raises(ValueError):
        hba1c.convert(7.0, "%", "mg/dL")
    assert hba1c.convert(7.0, "%", "%") == 7.0


# --------------------------------------------------------------------------
# the separation from units.convert()
# --------------------------------------------------------------------------

def test_units_convert_STILL_refuses_the_pair():
    """The bridge did not reopen the multiplicative path, and must not.

    `convert()` answers "are these units interconvertible?" — they are not.
    The bridge answers a narrower question: "for this substance, is there a
    named function?" Conflating them puts the intercept back in a table.
    """
    assert units.are_convertible("%", "mmol/mol", substance="hba1c") is False
    with pytest.raises(units.UnitConversionRefused):
        units.convert(7.0, "%", "mmol/mol", substance="hba1c")


# --------------------------------------------------------------------------
# grouping
# --------------------------------------------------------------------------

LOINC = "http://loinc.org/4548-4"


def _row(value, unit, guid, at):
    return dict(observation_guid=guid, code_canonical=LOINC, unit=unit,
                value=value, effective_at=at)


def test_a_mixed_history_becomes_ONE_group():
    gs = units.group_series([
        _row(7.0, "%", "a", "2026-01-01T00:00:00Z"),
        _row(53.0, "mmol/mol", "b", "2026-02-01T00:00:00Z"),
    ])
    assert len(gs) == 1
    assert gs[0].unit == "mmol/mol"
    assert [round(p.value) for p in gs[0].points] == [53, 53]


def test_a_majority_percent_history_is_NOT_relabelled():
    """Majority wins over the preferred unit, as it does for every other
    group — a series mostly recorded in % stays in %, rather than every
    historical value being restated in a unit the clinician did not use."""
    gs = units.group_series([
        _row(7.0, "%", "a", "2026-01-01T00:00:00Z"),
        _row(8.0, "%", "b", "2026-02-01T00:00:00Z"),
        _row(53.0, "mmol/mol", "c", "2026-03-01T00:00:00Z"),
    ])
    assert len(gs) == 1
    assert gs[0].unit == "%"
    assert [round(p.value, 1) for p in gs[0].points] == [7.0, 8.0, 7.0]


def test_percent_without_the_hba1c_code_does_NOT_join_mmol_per_mol():
    """The bridge is opened by the SUBSTANCE, not by the units. A bare `%`
    concept must not be dragged into an HbA1c class."""
    other = "http://loinc.org/99999-9"
    gs = units.group_series([
        dict(observation_guid="a", code_canonical=other, unit="%",
             value=7.0, effective_at="2026-01-01T00:00:00Z"),
        dict(observation_guid="b", code_canonical=other, unit="mmol/mol",
             value=53.0, effective_at="2026-02-01T00:00:00Z"),
    ])
    assert len(gs) == 2


def test_an_out_of_range_value_RAISES_rather_than_falling_through():
    """The dangerous near-miss: if the bridge's refusal fell through to
    `convert()`, a bad reading would be re-interpreted by the multiplicative
    path — the very factor this design refuses. It must propagate."""
    with pytest.raises(hba1c.Hba1cOutOfRange):
        units.group_series([
            _row(1.0, "%", "a", "2026-01-01T00:00:00Z"),
            _row(53.0, "mmol/mol", "b", "2026-02-01T00:00:00Z"),
            _row(54.0, "mmol/mol", "c", "2026-03-01T00:00:00Z"),
        ])
