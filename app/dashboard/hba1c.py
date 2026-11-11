"""HbA1c NGSP (%) <-> IFCC (mmol/mol) — the affine bridge, on its own.

THIS IS DELIBERATELY NOT A ROW IN `units.py`'s CONVERSION TABLE, and the
reason is the whole point of the module.

`units.convert()` is MULTIPLICATIVE by construction: it multiplies by a
factor. The NGSP/IFCC relation is AFFINE — it has an intercept — so no factor
can express it, and the factor that *looks* right is clinically wrong:

    % and mmol/mol are numerically a pure ratio, 1 mmol/mol = 0.1 %.

Applying that ratio to HbA1c overstates every value by about a third across
the whole clinical range:

      5.0 %   ratio says 50   truth 31
      7.0 %   ratio says 70   truth 53
      9.0 %   ratio says 90   truth 75

So `units.convert("%" -> "mmol/mol")` still REFUSES, and must keep refusing.
This module is the separate, named thing that the operator decided to build
(2026-11-11) so that a patient whose history carries both units gets ONE
chart instead of two.

THE SHARPER LESSON, recorded because it is the one that generalises: knowing
the SUBSTANCE is not sufficient. The molar-mass bridge in `units.py` opens
mmol/L <-> mg/dL once the analyte is known, because that relation really is
multiplicative. Here the substance is known from the start and the relation is
still not a factor. **A bridge needs the substance AND a multiplicative
relation; this one has only the first.**

## The equation, defined ONCE

The NGSP master equation, in the NGSP direction as NGSP publishes it:

    NGSP% = 0.09148 * IFCC + 2.152

The other direction is DERIVED from those two constants rather than written
as a second pair, so the two can never drift apart — which is exactly how
`2.15 / 10.929`, the commonly quoted rounded form, ends up disagreeing with
itself in the third significant figure.

## What it refuses

IFCC is a ratio of glycated to total haemoglobin and cannot be negative, so
NGSP below the intercept (2.152 %) has no IFCC counterpart. That is refused by
name rather than clamped: a clamp would turn an impossible reading into a
plausible one.

There is deliberately NO upper bound. HbA1c above 20 % is not physiological,
but refusing it would silently drop a real extreme reading, and a chart that
quietly omits the worst point is worse than one that shows it.
"""
from __future__ import annotations

#: NGSP% = SLOPE * IFCC + INTERCEPT. The only two numbers in this module.
SLOPE = 0.09148        # NGSP % per IFCC mmol/mol
INTERCEPT = 2.152      # NGSP %, the value at IFCC = 0

#: Below this, IFCC would be negative. Not clinically reachable, but a bad
#: value must fail loudly rather than be clamped into plausibility.
MIN_NGSP_PERCENT = INTERCEPT


class Hba1cOutOfRange(ValueError):
    """An HbA1c value with no counterpart in the other unit."""

    def __init__(self, value: float, unit: str, reason: str):
        super().__init__(reason)
        self.value = value
        self.unit = unit
        self.reason = reason


def ngsp_from_ifcc(ifcc_mmol_per_mol: float) -> float:
    """IFCC mmol/mol -> NGSP %."""
    v = _numeric(ifcc_mmol_per_mol, "mmol/mol")
    if v < 0:
        raise Hba1cOutOfRange(
            v, "mmol/mol",
            "IFCC HbA1c is a ratio of glycated to total haemoglobin and "
            "cannot be negative",
        )
    return SLOPE * v + INTERCEPT


def ifcc_from_ngsp(ngsp_percent: float) -> float:
    """NGSP % -> IFCC mmol/mol.

    Derived from SLOPE and INTERCEPT, not from a second pair of constants.
    """
    v = _numeric(ngsp_percent, "%")
    if v < MIN_NGSP_PERCENT:
        raise Hba1cOutOfRange(
            v, "%",
            f"NGSP HbA1c below {MIN_NGSP_PERCENT} % has no IFCC counterpart "
            f"(IFCC would be negative); refused rather than clamped",
        )
    return (v - INTERCEPT) / SLOPE


def convert(value: float, from_unit: str, to_unit: str) -> float:
    """The pair, by unit token. Identity when the units match."""
    if from_unit == to_unit:
        return _numeric(value, from_unit)
    if from_unit == "%" and to_unit == "mmol/mol":
        return ifcc_from_ngsp(value)
    if from_unit == "mmol/mol" and to_unit == "%":
        return ngsp_from_ifcc(value)
    raise ValueError(
        f"the HbA1c bridge joins '%' and 'mmol/mol' only, not "
        f"{from_unit!r} -> {to_unit!r}"
    )


def _numeric(value, unit: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(
            f"HbA1c in {unit} must be numeric, got {value!r}"
        )
    return float(value)
