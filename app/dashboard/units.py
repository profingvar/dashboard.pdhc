"""Series grouping and unit conversion (the operator's coding plan, step 7).

PURE FUNCTIONS ONLY. No HTTP, no database, no Flask, no third-party imports.
Everything here is decidable from the arguments, which is what makes the
refusals below testable — see `app/tests/test_dashboard_units.py`.

THE RULE, as the plan states it
-------------------------------
"Two observations belong to the same series when `concept_guid` (or
`code_canonical`) is equal AND the units are convertible. Use UCUM conversion
where available; otherwise split the series by unit."

MEASURED CORRECTION, which `dto.py`'s correction 1 already records: there is no
`concept_guid` column on a CDR observation. The identity is `code_canonical`.
Nothing in this module keys on `concept_guid`.

WHY NO UCUM LIBRARY
-------------------
A UCUM library would give the dimensional algebra for free, and would still get
the one case that matters here WRONG — or rather, it would answer a different
question. `mmol/L` and `mg/dL` are not dimensionally related: amount-of-
substance per volume is not mass per volume. The bridge between them is the
substance's MOLAR MASS, which is a property of the analyte and not of either
unit. A library that refuses the pair is no better than the fifteen-line table
below; a library that converts it is lying. So: no dependency, a small curated
table, and the molar-mass bridge made explicit and substance-gated.

The table is closed by design. An unrecognised token REFUSES rather than
guessing, and refusing shows up in the UI as two charts instead of one.

THE TRAP, AND IT IS THE POINT OF THIS MODULE
--------------------------------------------
Returning a wrong number is far worse than returning two charts, because a
clinician cannot see that it happened. Two charts are visibly two charts. A
glucose of 5.5 silently rendered as 99 is indistinguishable from a measurement.

So this module refuses in four named ways, and each refusal carries its reason
as text (`refusal_reason`) so the panel can say why it drew two axes:

1.  MOLAR vs MASS CONCENTRATION (`mmol/L` vs `mg/dL`) needs the analyte's
    molar mass. Glucose is 180.156 g/mol and creatinine is 113.120 g/mol, so
    the factor is 18.0 for one and 0.0884 for the other — it is not derivable
    from the units. Converted ONLY when the substance is established from
    `code_canonical` (a known LOINC code), never from the units alone.

2.  AN AFFINE RELATION IS NOT A UNIT CONVERSION. `%` and `mmol/mol` are
    numerically a pure ratio (1 mmol/mol = 0.1 %), and for HbA1c that ratio is
    clinically WRONG: the NGSP/IFCC master equation is affine,
    `IFCC = (NGSP - 2.15) * 10.929`, so 7 % is 53 mmol/mol, not 70. Knowing the
    substance does not help, because `convert()` is multiplicative by
    construction. Refused unconditionally. `Cel` vs `[degF]` is the same shape.

3.  A CALENDAR UNIT IS NOT A DURATION. `mo` and `a` are not a fixed number of
    seconds, so they never convert to `s`/`min`/`h`/`d`/`wk`.

4.  AN UNKNOWN UNIT IS NOT A MISSING UNIT. `"furlong"` is a unit somebody
    RECORDED and we do not understand; `None` is no unit at all. Each is
    convertible only with itself, and crucially they are not convertible with
    each other — collapsing them would merge a recorded measurement into an
    unitless one.

WHERE THE TABLE'S VOCABULARY CAME FROM
--------------------------------------
Measured in this repo rather than invented, so the aliases are the forms the
platform actually writes:

| source | tokens |
|---|---|
| `app/templates/nurse_workspace.html:79` (the live variable list) | `%`, `kg`, `kg/m2`, `mmHg` |
| `app/analyse/federation.py:534` (TIR/TBR/TAR) | `mmol/L`, and `mg/dL` named as the equivalent |
| `app/tests/test_aggregations.py:26,175` | `mg/dL`, `mmol/L` |
| `app/tests/test_patient_charts.py:57` | `kg` |
| `app/analyse/aggregations.py` | `/min` |

Note `mmHg`, not the UCUM `mm[Hg]`: the repo writes the sloppy form, so both
normalise to one token. `lb` is in the table because the plan names it; it has
**zero** occurrences in this repo (measured), so it is capability, not usage.

SCALE FACTORS, each traceable
-----------------------------
*   SI prefixes (`k`, `d`, `c`, `m`, `u`, `n`) are exact by definition.
*   `1 mm[Hg] = 133.322387415 Pa` exactly (UCUM / NIST SP 811), which is where
    the `kPa` factor `1000/133.322387415 = 7.5006157585...` comes from.
*   `1 cm[H2O] = 98.0665 Pa` exactly (UCUM), hence `98.0665/133.322387415`.
*   `1 [lb_av] = 453.59237 g` exactly (international avoirdupois pound, NIST).
*   Molar masses from IUPAC 2021 standard atomic weights
    (C 12.011, H 1.008, N 14.007, O 15.999); the arithmetic is shown at each
    entry so it can be rechecked without leaving the file.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from .dto import SeriesPoint

__all__ = [
    "UnitConversionRefused",
    "normalise_unit",
    "dimension_of",
    "refusal_reason",
    "are_convertible",
    "convert",
    "substance_from_code_canonical",
    "SeriesGroup",
    "SeriesCount",
    "group_series",
    "series_counts",
]

# ---------------------------------------------------------------------------
# dimensions
# ---------------------------------------------------------------------------

MASS = "mass"                            # base g
LENGTH = "length"                        # base m
PRESSURE = "pressure"                    # base mm[Hg]
VOLUME = "volume"                        # base L
TIME = "time"                            # base s
PERCENT = "percent"                      # base %
MOLAR_RATIO = "molar_ratio"              # base mmol/mol
COUNT = "count"                          # base 1 (dimensionless)
FREQUENCY = "frequency"                  # base /min
MOLAR_CONCENTRATION = "molar_concentration"   # base mmol/L
MASS_CONCENTRATION = "mass_concentration"     # base mg/L
MASS_PER_AREA = "mass_per_area"          # base kg/m2 (BMI)
TEMPERATURE = "temperature"              # base Cel

#: canonical token -> (dimension, factor to that dimension's base unit)
_UNITS: dict[str, tuple[str, float]] = {
    # -- mass -------------------------------------------------------------
    "kg": (MASS, 1000.0),
    "g": (MASS, 1.0),
    "mg": (MASS, 1e-3),
    "ug": (MASS, 1e-6),
    # exact: international avoirdupois pound (NIST SP 811). Present for the
    # plan's sake; 0 occurrences measured in this repo.
    "[lb_av]": (MASS, 453.59237),

    # -- length -----------------------------------------------------------
    "m": (LENGTH, 1.0),
    "cm": (LENGTH, 0.01),
    "mm": (LENGTH, 1e-3),

    # -- pressure ---------------------------------------------------------
    "mm[Hg]": (PRESSURE, 1.0),
    "kPa": (PRESSURE, 1000.0 / 133.322387415),
    "cm[H2O]": (PRESSURE, 98.0665 / 133.322387415),

    # -- volume -----------------------------------------------------------
    "L": (VOLUME, 1.0),
    "dL": (VOLUME, 0.1),
    "mL": (VOLUME, 1e-3),
    "uL": (VOLUME, 1e-6),

    # -- time (durations only; see _IRREDUCIBLE for mo/a) -----------------
    "s": (TIME, 1.0),
    "min": (TIME, 60.0),
    "h": (TIME, 3600.0),
    "d": (TIME, 86400.0),
    "wk": (TIME, 604800.0),

    # -- percent ----------------------------------------------------------
    # `%` is ALONE in its dimension on purpose. mmol/mol is NOT here; see
    # refusal 2 in the module docstring.
    "%": (PERCENT, 1.0),

    # -- molar ratio (HbA1c IFCC) -----------------------------------------
    "mmol/mol": (MOLAR_RATIO, 1.0),

    # -- dimensionless counts ---------------------------------------------
    # A closed allowlist. An arbitrary UCUM annotation (`{beats}`, `{breaths}`)
    # is dimensionless too and is deliberately NOT here: two annotations that
    # differ name two different quantities, and treating them as one would be
    # exactly the silent-wrong-number failure this module exists to prevent.
    "1": (COUNT, 1.0),
    "{count}": (COUNT, 1.0),
    "{#}": (COUNT, 1.0),

    # -- frequency / rate -------------------------------------------------
    "/min": (FREQUENCY, 1.0),
    "/s": (FREQUENCY, 60.0),
    "/h": (FREQUENCY, 1.0 / 60.0),
    "/d": (FREQUENCY, 1.0 / 1440.0),

    # -- amount-of-substance concentration --------------------------------
    "mol/L": (MOLAR_CONCENTRATION, 1000.0),
    "mmol/L": (MOLAR_CONCENTRATION, 1.0),
    "umol/L": (MOLAR_CONCENTRATION, 1e-3),
    "nmol/L": (MOLAR_CONCENTRATION, 1e-6),

    # -- mass concentration -----------------------------------------------
    "g/L": (MASS_CONCENTRATION, 1000.0),
    "g/dL": (MASS_CONCENTRATION, 10000.0),
    "mg/L": (MASS_CONCENTRATION, 1.0),
    "mg/dL": (MASS_CONCENTRATION, 10.0),
    "ug/L": (MASS_CONCENTRATION, 1e-3),
    "ug/dL": (MASS_CONCENTRATION, 0.01),

    # -- BMI --------------------------------------------------------------
    "kg/m2": (MASS_PER_AREA, 1.0),

    # -- temperature ------------------------------------------------------
    # Cel is alone here; [degF] is affine and lives in _IRREDUCIBLE.
    "Cel": (TEMPERATURE, 1.0),
}

#: RECOGNISED tokens that are convertible only with themselves, each with the
#: reason. These are not gaps in the table — they are decisions, and keeping
#: them here rather than letting them fall through to "unknown" is what stops
#: the next reader from "completing" the table with a wrong factor.
_IRREDUCIBLE: dict[str, str] = {
    "mo": ("a calendar month is not a fixed number of seconds, so 'mo' does "
           "not convert to a duration unit"),
    "a": ("a calendar year is not a fixed number of seconds, so 'a' does not "
          "convert to a duration unit"),
    "[degF]": ("degrees Fahrenheit relate to Celsius by an AFFINE equation "
               "(x - 32) * 5/9, and convert() is multiplicative by "
               "construction, so it cannot carry the offset"),
}

#: casefolded input spelling -> canonical token. Built from the two tables
#: above plus the sloppy spellings this platform actually writes.
#:
#: UCUM is case-SENSITIVE and this map is not. That is a deliberate narrowing:
#: the key set is closed and curated, no two canonical tokens in it collide
#: under casefold (asserted at import, below), and the CDR rows carry `mmHg`
#: and `mmol/L` rather than strict UCUM. An unrecognised spelling refuses, so
#: the cost of the narrowing is a split chart, never a wrong number.
_ALIASES: dict[str, str] = {}


def _alias(spelling: str, canonical: str) -> None:
    _ALIASES[spelling.casefold()] = canonical


for _tok in list(_UNITS) + list(_IRREDUCIBLE):
    _alias(_tok, _tok)

# pressure, as the repo writes it
_alias("mmHg", "mm[Hg]")
_alias("mm Hg", "mm[Hg]")
_alias("kpa", "kPa")
_alias("cmH2O", "cm[H2O]")
# micro, in every spelling a keyboard produces
_alias("µg", "ug")           # MICRO SIGN
_alias("μg", "ug")           # GREEK SMALL LETTER MU
_alias("mcg", "ug")
_alias("µmol/L", "umol/L")
_alias("μmol/L", "umol/L")
_alias("mcmol/L", "umol/L")
_alias("µL", "uL")
_alias("μL", "uL")
_alias("µg/L", "ug/L")
_alias("μg/L", "ug/L")
_alias("µg/dL", "ug/dL")
_alias("μg/dL", "ug/dL")
# litre, spelled out or lower-cased
_alias("l", "L")
_alias("dl", "dL")
_alias("ml", "mL")
_alias("liter", "L")
_alias("litre", "L")
# BMI
_alias("kg/m^2", "kg/m2")
_alias("kg/m**2", "kg/m2")
# rates
_alias("1/min", "/min")
_alias("min-1", "/min")
_alias("bpm", "/min")
_alias("1/s", "/s")
_alias("1/h", "/h")
_alias("1/d", "/d")
# counts
_alias("count", "{count}")
_alias("counts", "{count}")
_alias("{counts}", "{count}")
# percent
_alias("percent", "%")
_alias("pct", "%")
# temperature
_alias("°C", "Cel")
_alias("degC", "Cel")
_alias("°F", "[degF]")
_alias("degF", "[degF]")
# time
_alias("sec", "s")
_alias("hr", "h")
_alias("day", "d")
_alias("week", "wk")
_alias("month", "mo")
_alias("year", "a")

# A closed case-insensitive map is only safe if the canonical tokens do not
# collide under casefold. Assert it at import rather than trusting the reader:
# adding `Mg` (megagram) to _UNITS beside `mg` must fail loudly, here, rather
# than silently make every milligram a tonne.
_canonical_tokens = list(_UNITS) + list(_IRREDUCIBLE)
if len({t.casefold() for t in _canonical_tokens}) != len(_canonical_tokens):
    raise AssertionError(
        "two canonical unit tokens collide under casefold; the case-insensitive "
        "alias map above is unsafe until one of them is renamed"
    )


# ---------------------------------------------------------------------------
# dimension bridges, and the pairs that are refused on purpose
# ---------------------------------------------------------------------------

#: The ONE bridge across dimensions, and it is substance-gated. The multiplier
#: is the analyte's molar mass, which is a property of the analyte and not of
#: either unit — which is precisely why `are_convertible(a, b)` cannot answer
#: the question without being told the substance.
_MOLAR_MASS_BRIDGE = frozenset({MOLAR_CONCENTRATION, MASS_CONCENTRATION})

#: Dimension pairs that LOOK convertible and are refused regardless of
#: substance, with the reason the UI can print. Each is a relation that is not
#: a multiplication, so no factor in this module could express it.
_REFUSED_DIMENSION_PAIRS: dict[frozenset[str], str] = {
    frozenset({PERCENT, MOLAR_RATIO}): (
        "% and mmol/mol look like a pure ratio (1 mmol/mol = 0.1 %) but for "
        "HbA1c the NGSP/IFCC relation is AFFINE, IFCC = (NGSP - 2.15) * "
        "10.929 — 7 % is 53 mmol/mol, not 70 — so no multiplicative factor "
        "is correct and the series must split"
    ),
    frozenset({MASS, VOLUME}): (
        "mass and volume are bridged by DENSITY, which is a property of the "
        "substance and is not recorded on an observation"
    ),
    frozenset({MASS_CONCENTRATION, PERCENT}): (
        "a mass concentration and a percentage are bridged by the total "
        "mass or volume of the specimen, which is not recorded"
    ),
}

#: Molar masses in g/mol, with the arithmetic shown. IUPAC 2021 standard
#: atomic weights: C 12.011, H 1.008, N 14.007, O 15.999.
_MOLAR_MASS_G_PER_MOL: dict[str, float] = {
    # C6H12O6: 6*12.011 + 12*1.008 + 6*15.999 = 72.066 + 12.096 + 95.994
    # => 1 mmol/L = 18.0156 mg/dL (the textbook 18.0 factor)
    "glucose": 180.156,
    # C4H7N3O: 4*12.011 + 7*1.008 + 3*14.007 + 15.999
    #        = 48.044 + 7.056 + 42.021 + 15.999
    # => 1 mg/dL = 88.41 umol/L (the textbook 88.4 factor)
    "creatinine": 113.120,
}

#: The unit a group is charted in when no unit holds a majority of its points.
#: A tie has to be broken by SOMETHING, and "alphabetically" would chart a
#: 1-kg-1-g series in grams. These are the platform's own conventions where it
#: has one, and arbitrary-but-fixed where it does not.
_PREFERRED_UNITS: dict[str, str] = {
    MASS: "kg",
    LENGTH: "cm",
    PRESSURE: "mm[Hg]",          # app/templates/nurse_workspace.html:82
    VOLUME: "L",
    TIME: "h",
    PERCENT: "%",
    MOLAR_RATIO: "mmol/mol",
    COUNT: "1",
    FREQUENCY: "/min",
    MOLAR_CONCENTRATION: "mmol/L",
    MASS_CONCENTRATION: "mg/dL",
    MASS_PER_AREA: "kg/m2",
    TEMPERATURE: "Cel",
}

#: When the molar-mass bridge has merged the two concentration dimensions, the
#: group is charted in mmol/L — the Sweden / IFCC convention this repo already
#: states at `app/analyse/federation.py:534` ("TIR/TBR/TAR thresholds in
#: mmol/L ... equivalents in mg/dL would be 70/180").
_PREFERRED_BRIDGED_CONCENTRATION = "mmol/L"


# ---------------------------------------------------------------------------
# AFFINE BRIDGES — named relations that `convert()` cannot express
# ---------------------------------------------------------------------------
#
# `convert()` is multiplicative by construction and still REFUSES these pairs;
# nothing below changes that, and the tests that pin the refusal still pass.
# What is added is a second, explicitly named path that ONLY `group_series`
# takes, so a patient whose history carries both units gets one chart.
#
# Operator decision 2026-11-11, for HbA1c. The relation is
# `NGSP% = 0.09148 * IFCC + 2.152` and lives in `app/dashboard/hba1c.py`,
# which exists precisely so the intercept cannot be mistaken for a factor.
#
# THE RULE FOR ADDING ONE: a molar-mass bridge needs the substance AND a
# multiplicative relation. An affine bridge needs the substance AND its own
# named function. Do not add a pair here by writing a factor — if it can be a
# factor it belongs in the table above.
from . import hba1c as _hba1c

_AFFINE_BRIDGES: dict[str, Any] = {"hba1c": _hba1c}

#: The dimensions an affine bridge may merge. `%` and `mmol/mol` are each
#: alone in their own dimension precisely because the ratio between them is a
#: trap; this merges them only for a substance that has a named function.
_AFFINE_BRIDGE_DIMENSIONS = frozenset({PERCENT, MOLAR_RATIO})

#: Charted in the IFCC unit: Swedish laboratories report HbA1c in mmol/mol.
#: `app/templates/nurse_workspace.html:80` declares it in `%`, which is why a
#: single patient can carry both and why this bridge was asked for.
_PREFERRED_AFFINE_BRIDGE: dict[str, str] = {"hba1c": "mmol/mol"}

#: Substances we can NAME from a code but deliberately cannot bridge. Keeping
#: them here means "the substance is known and the conversion is still
#: refused" is expressible — the point of refusal 2.
_SUBSTANCES_WITHOUT_BRIDGE: frozenset[str] = frozenset({"hba1c"})

#: LOINC code -> substance. Small and curated, each entry's meaning named, and
#: an unlisted code resolves to None and therefore REFUSES the bridge.
#:
#: This is the only place a substance can come from in a pure function. On cdr1
#: `code_canonical` is `urn:pdhc:concept/<guid>` — an opaque plan.pdhc GUID
#: that would need an HTTP lookup to resolve, so cdr1 rows never establish a
#: substance here and their mmol/L-vs-mg/dL series SPLIT. That is the correct
#: outcome, not a gap: see `group_series(substance_resolver=...)` for the
#: caller that has already done the lookup.
_LOINC_SUBSTANCE: dict[str, str] = {
    # glucose
    "2339-0": "glucose",     # Glucose [Mass/volume] in Blood
    "2345-7": "glucose",     # Glucose [Mass/volume] in Serum or Plasma
    "15074-8": "glucose",    # Glucose [Moles/volume] in Blood
    "14749-6": "glucose",    # Glucose [Moles/volume] in Serum or Plasma
    "41653-7": "glucose",    # Glucose [Mass/volume] in Capillary blood by
                             # Glucometer -- the CGM code this repo already
                             # uses (app/routes/nurse.py:293)
    # creatinine
    "2160-0": "creatinine",  # Creatinine [Mass/volume] in Serum or Plasma
    "38483-4": "creatinine",  # Creatinine [Mass/volume] in Blood
    "14682-9": "creatinine",  # Creatinine [Moles/volume] in Serum or Plasma
    # HbA1c -- named so the refusal can say WHICH analyte it is refusing
    "4548-4": "hba1c",       # HbA1c/Hb.total in Blood (NGSP %) -- the code in
                             # app/templates/nurse_workspace.html:80
    "17856-6": "hba1c",      # HbA1c/Hb.total in Blood by HPLC
    "59261-8": "hba1c",      # HbA1c/Hb.total in Blood by IFCC (mmol/mol)
}


# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------

class UnitConversionRefused(ValueError):
    """`convert()` was asked for a pair it will not convert.

    Raised rather than returning a best guess. The whole module exists because
    a wrong number is invisible to a clinician and a refusal is not.
    """

    def __init__(self, from_unit: str | None, to_unit: str | None, reason: str):
        super().__init__(
            f"refusing to convert {from_unit!r} -> {to_unit!r}: {reason}"
        )
        self.from_unit = from_unit
        self.to_unit = to_unit
        self.reason = reason


def normalise_unit(unit: Any) -> str | None:
    """Canonical token for a recorded unit, or None for "no unit".

    None, the empty string and whitespace all mean "not recorded" (dto.py's
    own rule) and collapse to None. A non-empty spelling we do not recognise
    is returned STRIPPED but otherwise untouched: it stays a unit somebody
    recorded, distinct from None, and distinct from every other spelling.
    """
    if unit is None:
        return None
    if not isinstance(unit, str):
        unit = str(unit)
    token = unit.strip()
    if not token:
        return None
    return _ALIASES.get(token.casefold(), token)


def dimension_of(unit: Any) -> str | None:
    """The dimension of a recorded unit, or None when it has none we know.

    None is returned for "no unit", for an irreducible token and for an
    unrecognised spelling alike — so a caller must NOT read "same dimension"
    off two Nones. Use `are_convertible`.
    """
    token = normalise_unit(unit)
    if token is None:
        return None
    entry = _UNITS.get(token)
    return entry[0] if entry else None


def _classify(token: str | None) -> tuple[str, str | None]:
    """(kind, detail) for an already-normalised token.

    kind is one of: "no-unit", "dimension", "irreducible", "unknown".
    """
    if token is None:
        return ("no-unit", None)
    if token in _IRREDUCIBLE:
        return ("irreducible", token)
    entry = _UNITS.get(token)
    if entry is not None:
        return ("dimension", entry[0])
    return ("unknown", token)


def substance_from_code_canonical(code_canonical: Any) -> str | None:
    """The analyte a `code_canonical` names, or None when it does not name one.

    Handles the THREE shapes the platform actually stores, which differ per
    CDR (measured 2026-10-06, #733):

    *   `https://termbank.pdhc.se/CodeSystem/loinc/<code>` -- cdr2, cdr3
    *   `https://termbank.pdhc.se/CodeSystem/loinc|<code>` -- the FHIR search
        token form this repo writes (`app/routes/nurse.py:293,425`)
    *   `http://loinc.org/<code>` and a bare `<code>`

    and returns None for `urn:pdhc:concept/<guid>` -- cdr1's shape, where the
    identity is an opaque plan.pdhc GUID. Resolving that needs an HTTP lookup,
    which this module does not do, so the substance is NOT established and the
    molar-mass bridge stays closed. That is the refusal, by design.
    """
    if not isinstance(code_canonical, str):
        return None
    text = code_canonical.strip()
    if not text:
        return None
    # A plan.pdhc local GUID names no analyte we can read. Say so explicitly
    # rather than letting it fall through the parsing below.
    if text.startswith("urn:pdhc:concept/"):
        return None
    # The last path or token segment is the code in every shape above.
    tail = text.replace("|", "/").rstrip("/").rsplit("/", 1)[-1].strip()
    return _LOINC_SUBSTANCE.get(tail)


# ---------------------------------------------------------------------------
# convertibility
# ---------------------------------------------------------------------------

def refusal_reason(unit_a: Any, unit_b: Any, *,
                   substance: str | None = None) -> str | None:
    """None when the pair is convertible, else the reason it is not.

    `substance` defaults to None, which is the refusing default: the
    molar/mass bridge opens only when a caller states the analyte, because the
    factor is the analyte's molar mass and is not in either unit.

    This is the ONE implementation of the rule; `are_convertible` and
    `convert` both go through it, so they cannot drift apart.
    """
    token_a = normalise_unit(unit_a)
    token_b = normalise_unit(unit_b)

    # Reflexive, always: the same recorded unit is the same series. This is
    # the only thing true of "no unit", of an irreducible token and of an
    # unrecognised spelling.
    if token_a == token_b:
        return None

    kind_a, detail_a = _classify(token_a)
    kind_b, detail_b = _classify(token_b)

    if kind_a == "no-unit" or kind_b == "no-unit":
        recorded = token_b if kind_a == "no-unit" else token_a
        return (f"one observation records no unit and the other records "
                f"{recorded!r}; an absent unit is not a wildcard and must not "
                f"be assumed to be the other one")

    if kind_a == "unknown" or kind_b == "unknown":
        unknown = detail_a if kind_a == "unknown" else detail_b
        return (f"{unknown!r} is not in this module's unit table, so there is "
                f"no factor for it; an unrecognised unit converts to nothing "
                f"but itself")

    if kind_a == "irreducible" or kind_b == "irreducible":
        token = detail_a if kind_a == "irreducible" else detail_b
        return _IRREDUCIBLE[token]

    # Both sides are in the table with a dimension.
    if detail_a == detail_b:
        return None

    pair = frozenset({detail_a, detail_b})

    refused = _REFUSED_DIMENSION_PAIRS.get(pair)
    if refused is not None:
        return refused

    if pair == _MOLAR_MASS_BRIDGE:
        if substance is None:
            return ("an amount-of-substance concentration and a mass "
                    "concentration are bridged by the analyte's MOLAR MASS, "
                    "which is a property of the substance and not of either "
                    "unit; the substance could not be established from "
                    "code_canonical, so the series must split")
        if substance in _MOLAR_MASS_G_PER_MOL:
            return None
        if substance in _SUBSTANCES_WITHOUT_BRIDGE:
            return (f"the substance is known to be {substance!r} and the "
                    f"conversion is still refused: no molar mass is recorded "
                    f"for it in this module")
        return (f"no molar mass is recorded for substance {substance!r}, so "
                f"the molar/mass bridge stays closed")

    return (f"{token_a!r} and {token_b!r} measure different quantities "
            f"({detail_a} vs {detail_b}) and no bridge between them is "
            f"derivable from the units")


def are_convertible(unit_a: Any, unit_b: Any, *,
                    substance: str | None = None) -> bool:
    """True when a value in `unit_a` can be expressed exactly in `unit_b`.

    With `substance` left at its default, the molar/mass pair
    (`mmol/L` vs `mg/dL`) is FALSE — that is not a limitation, it is the
    answer: the conversion is not derivable from the units.
    """
    return refusal_reason(unit_a, unit_b, substance=substance) is None


def convert(value: float, from_unit: Any, to_unit: Any, *,
            substance: str | None = None) -> float:
    """`value` expressed in `to_unit`.

    Raises `UnitConversionRefused` for any pair `are_convertible` rejects. It
    never returns an approximation, and it never falls back to returning the
    input unchanged — a silently unconverted number is the same clinical
    failure as a wrongly converted one.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"convert() needs a numeric value, got {value!r}")

    reason = refusal_reason(from_unit, to_unit, substance=substance)
    if reason is not None:
        raise UnitConversionRefused(normalise_unit(from_unit),
                                    normalise_unit(to_unit), reason)

    token_from = normalise_unit(from_unit)
    token_to = normalise_unit(to_unit)
    if token_from == token_to:
        return float(value)

    dim_from, factor_from = _UNITS[token_from]
    dim_to, factor_to = _UNITS[token_to]

    # In the dimension's base unit.
    base = float(value) * factor_from

    if dim_from != dim_to:
        # The only crossing refusal_reason permits, and only with a substance
        # whose molar mass is recorded.
        molar_mass = _MOLAR_MASS_G_PER_MOL[substance]
        if dim_from == MOLAR_CONCENTRATION:
            # mmol/L * g/mol = mg/L   (1 mmol * 1 g/mol = 1 mg)
            base = base * molar_mass
        else:
            # mg/L / g/mol = mmol/L
            base = base / molar_mass

    return base / factor_to


# ---------------------------------------------------------------------------
# grouping
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SeriesGroup:
    """One plottable series: one `code_canonical`, one unit, converted points.

    `source_units` is the distinct set of units the rows arrived in, so a
    panel can say "converted from mg/dL" rather than quietly relabelling the
    axis. `refused` is the reason this group is SEPARATE from its siblings
    under the same `code_canonical` — None when it is the only group.
    """
    code_canonical: str
    unit: str | None
    points: tuple[SeriesPoint, ...]
    source_units: tuple[str | None, ...] = ()
    #: f"{code_canonical}|{unit or ''}" -- stable across loads, usable as a
    #: chart id. Unique within a result because each unit belongs to exactly
    #: one group.
    series_key: str = ""
    converted: bool = False
    substance: str | None = None
    refused: str | None = None

    @property
    def count(self) -> int:
        return len(self.points)

    @property
    def has_series(self) -> bool:
        """True when there is more than one point, i.e. something to plot.

        One point is a value, not a series. `dto.EventRow.has_series` drives
        whether the row offers a chart at all.
        """
        return len(self.points) > 1

    def to_series(self, display_name: str | None = None,
                  downsampled_from: int | None = None):
        """Bridge to `dto.Series`, which this module must not reach into to
        build (it carries a `display_name` this module has no source for).
        Imported lazily so `units.py` keeps no import-time dependency beyond
        `SeriesPoint`."""
        from .dto import Series
        return Series(
            code_canonical=self.code_canonical,
            display_name=display_name or self.code_canonical,
            unit=self.unit,
            points=self.points,
            downsampled_from=downsampled_from,
        )


@dataclass(frozen=True)
class SeriesCount:
    """What `dto.EventRow.series_count` / `.has_series` need for ONE row.

    A dataclass rather than a bare int for dto.py's own stated reason: a typo
    in a dict key is a silent empty panel, and `count` vs `has_series` is
    exactly the pair a caller gets backwards.
    """
    count: int
    has_series: bool
    series_key: str
    unit: str | None


def _get(row: Any, name: str, default: Any = None) -> Any:
    """Field access over either a mapping or an object."""
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _numeric(value: Any) -> float | None:
    """A float, or None when this row cannot be a chart point.

    `bool` is rejected although it is an `int` subclass: True is not 1.0 on a
    chart. A numeric string is accepted because CDR JSON carries some values
    as strings.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _class_key(token: str | None, substance: str | None) -> tuple[str, str | None]:
    """The equivalence class a unit belongs to, under this concept.

    Convertibility as defined above IS an equivalence relation, which is what
    makes a single grouping pass correct rather than an approximation:

    *   same dimension -- reflexive, symmetric, transitive by construction;
    *   the molar-mass bridge MERGES two whole dimensions when the substance
        is known, so transitivity survives it (mg/L ~ mg/dL ~ mmol/L);
    *   "no unit", an irreducible token and an unrecognised spelling are each
        a singleton class, equal only to themselves.
    """
    kind, detail = _classify(token)
    if kind == "dimension":
        if (detail in _AFFINE_BRIDGE_DIMENSIONS
                and substance in _AFFINE_BRIDGES):
            # One class for `%` and `mmol/mol`, named by the substance whose
            # AFFINE function joins them. `convert()` still refuses the pair.
            return ("affine-bridge", substance)
        if (detail in _MOLAR_MASS_BRIDGE
                and substance in _MOLAR_MASS_G_PER_MOL):
            # One class for both concentration dimensions, named by the
            # substance that bridges them.
            return ("bridged-concentration", substance)
        return ("dimension", detail)
    if kind == "no-unit":
        return ("no-unit", None)
    # irreducible and unknown are both singleton classes keyed by the token;
    # the kinds are kept distinct so a decision never reads as an omission.
    return (kind, detail)


def _preferred_unit_for(class_key: tuple[str, str | None]) -> str | None:
    """The tie-break unit for a group, or None when its class has no
    preference (a singleton class has nothing to break a tie between)."""
    kind, detail = class_key
    if kind == "affine-bridge":
        return _PREFERRED_AFFINE_BRIDGE.get(detail or "")
    if kind == "bridged-concentration":
        return _PREFERRED_BRIDGED_CONCENTRATION
    if kind == "dimension":
        return _PREFERRED_UNITS.get(detail or "")
    return None


def _bridge(value: float, from_unit: str | None, to_unit: str | None, *,
            substance: str | None) -> float:
    """Normalise one point into its group's unit.

    Tries a NAMED AFFINE BRIDGE first, then `convert()`. This is the only
    place the two paths meet, and the order matters: `convert()` refuses the
    affine pairs, so asking it first would raise on exactly the rows this
    exists to join.

    `convert()` itself is untouched and still refuses them — a caller asking
    "are these two units interconvertible?" must still get `False`, because
    they are not. What is true is narrower: for THIS substance there is a
    named function, and grouping may use it.
    """
    bridge = _AFFINE_BRIDGES.get(substance or "")
    if bridge is not None and from_unit and to_unit:
        try:
            return bridge.convert(value, from_unit, to_unit)
        except ValueError as exc:
            # A pair the bridge does not cover falls through to convert();
            # an out-of-range VALUE must not, or a bad reading would be
            # silently re-interpreted by the multiplicative path.
            if isinstance(exc, _hba1c.Hba1cOutOfRange):
                raise
    return convert(value, from_unit, to_unit, substance=substance)


def group_series(
    rows: Iterable[Any],
    *,
    substance_resolver: Callable[[str], str | None] | None = None,
) -> list[SeriesGroup]:
    """Group observation rows into plottable series. ONE pass over `rows`.

    `rows` are mappings or objects carrying `code_canonical`, `unit`, `value`,
    `effective_at` and `observation_guid`; `reference_low`, `reference_high`
    and `transaction_guid` are carried through when present.

    Same `code_canonical` AND convertible units -> one group, every value
    converted to the group's canonical unit. Otherwise separate groups.

    `substance_resolver` is the ONLY way a cdr1 `urn:pdhc:concept/<guid>` row
    can ever open the molar-mass bridge: a caller that has already resolved
    the concept against plan.pdhc may pass a function from `code_canonical` to
    a substance name. The default is None, so this module on its own refuses.
    It is consulted only when the built-in LOINC table does not answer.

    ORDER IS DETERMINISTIC AND INDEPENDENT OF INPUT ORDER, because an unstable
    order reshuffles the UI's charts between loads:

    *   groups sorted by `(code_canonical, unit or "")`, so a unitless group
        sorts before any unit of the same concept;
    *   points within a group sorted by `(effective_at, observation_guid)`.
        `effective_at` is compared as a STRING: that is chronological for the
        ISO-8601 UTC the CDRs emit, and `observation_guid` breaks ties so the
        order is total even for identical timestamps.

    The group's canonical unit is the unit MOST of its points arrived in, then
    `_PREFERRED_UNITS` for its class, then the lexicographically smallest
    token. Majority first rather than a fixed per-dimension unit, so a series
    recorded in grams is charted in grams instead of being relabelled to
    0.0075 kg; the preferred unit then breaks a tie, because "alphabetically"
    would chart a one-kg-one-gram series in grams.

    Rows whose value is not numeric cannot be chart points and are DROPPED.
    `series_counts` is computed from this same function, so the counts can
    never disagree with the groups a caller plots.
    """
    # key -> {"code": ..., "rows": [(token, numeric_value, row)], "substance": ...}
    buckets: dict[tuple[Any, ...], dict[str, Any]] = {}

    for row in rows:
        code = _get(row, "code_canonical")
        if not isinstance(code, str) or not code.strip():
            continue
        code = code.strip()
        value = _numeric(_get(row, "value"))
        if value is None:
            continue

        substance = substance_from_code_canonical(code)
        if substance is None and substance_resolver is not None:
            substance = substance_resolver(code)

        token = normalise_unit(_get(row, "unit"))
        class_key = _class_key(token, substance)
        key = (code,) + class_key
        bucket = buckets.get(key)
        if bucket is None:
            bucket = buckets[key] = {
                "code": code, "substance": substance,
                "class_key": class_key, "rows": [],
            }
        bucket["rows"].append((token, value, row))

    groups: list[SeriesGroup] = []
    for bucket in buckets.values():
        code = bucket["code"]
        substance = bucket["substance"]
        entries = bucket["rows"]

        # Majority unit; ties to the class's preferred unit, then to the
        # lexicographically smallest token so the result is total.
        tally: dict[str | None, int] = {}
        for token, _value, _row in entries:
            tally[token] = tally.get(token, 0) + 1
        preferred = _preferred_unit_for(bucket["class_key"])
        canonical_unit = min(
            tally,
            key=lambda t: (-tally[t], 0 if t == preferred else 1, t or ""),
        )

        points: list[SeriesPoint] = []
        for token, value, row in entries:
            converted = (value if token == canonical_unit
                         else _bridge(value, token, canonical_unit,
                                      substance=substance))
            points.append(SeriesPoint(
                observation_guid=_get(row, "observation_guid"),
                effective_at=_get(row, "effective_at"),
                value=converted,
                unit=canonical_unit,
                reference_low=_get(row, "reference_low"),
                reference_high=_get(row, "reference_high"),
                transaction_guid=_get(row, "transaction_guid"),
            ))

        points.sort(key=lambda p: (p.effective_at or "",
                                   p.observation_guid or ""))
        groups.append(SeriesGroup(
            code_canonical=code,
            unit=canonical_unit,
            points=tuple(points),
            source_units=tuple(sorted(tally, key=lambda t: t or "")),
            series_key=f"{code}|{canonical_unit or ''}",
            converted=len(tally) > 1,
            substance=substance,
        ))

    groups.sort(key=lambda g: (g.code_canonical, g.unit or ""))

    # Annotate each group that has siblings with WHY it is separate, so the
    # panel can explain two axes instead of just drawing them.
    by_code: dict[str, list[SeriesGroup]] = {}
    for group in groups:
        by_code.setdefault(group.code_canonical, []).append(group)
    annotated: list[SeriesGroup] = []
    for group in groups:
        siblings = by_code[group.code_canonical]
        reason = None
        if len(siblings) > 1:
            other = next(s for s in siblings if s is not group)
            reason = refusal_reason(group.unit, other.unit,
                                    substance=group.substance)
        annotated.append(SeriesGroup(
            code_canonical=group.code_canonical,
            unit=group.unit,
            points=group.points,
            source_units=group.source_units,
            series_key=group.series_key,
            converted=group.converted,
            substance=group.substance,
            refused=reason,
        ))
    return annotated


def series_counts(
    rows: Iterable[Any],
    *,
    substance_resolver: Callable[[str], str | None] | None = None,
) -> dict[str, SeriesCount]:
    """`observation_guid` -> what fills `dto.EventRow.series_count`/`has_series`.

    ONE grouped pass, as the plan's step 7 requires ("one grouped count, not
    N+1"): `group_series` is called exactly once for the whole page of rows
    and every row's answer is read off the group it landed in. A caller does
    `counts[row.observation_guid]` per row, which is a dict lookup and not a
    query.

    The count is the number of points in the row's OWN series -- so a row that
    is the only observation of its concept reports `count=1` and
    `has_series=False`. Counting per concept instead of per series would
    promise a chart that then splits into two.

    A row with no `observation_guid`, or one whose value is not numeric, has
    no entry: it is in no series, and an absent key is honest where a 0 would
    be read as a count.
    """
    out: dict[str, SeriesCount] = {}
    for group in group_series(rows, substance_resolver=substance_resolver):
        count = group.count
        has = group.has_series
        for point in group.points:
            if point.observation_guid is None:
                continue
            out[point.observation_guid] = SeriesCount(
                count=count,
                has_series=has,
                series_key=group.series_key,
                unit=group.unit,
            )
    return out
