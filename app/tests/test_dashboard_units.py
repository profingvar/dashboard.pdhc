"""Series grouping + unit conversion (coding plan step 7).

These are pure-function tests: no app fixture, no database, no client. If one
of them needs a Flask context, something has leaked into `units.py`.

The suite is organised around the REFUSALS, because they are the point. A test
that only proves `kg` converts to `g` would pass against a module that
converted `mmol/L` to `mg/dL` with a made-up factor, and that number is
invisible to a clinician. Each refusal is asserted twice -- `are_convertible`
is False AND `convert` raises -- so a module cannot pass by answering the
question one way and acting the other.
"""
from __future__ import annotations

import math

import pytest

from app.dashboard.dto import EventRow, Series
from app.dashboard.units import (
    MASS,
    MASS_CONCENTRATION,
    MOLAR_CONCENTRATION,
    PRESSURE,
    SeriesCount,
    UnitConversionRefused,
    are_convertible,
    convert,
    dimension_of,
    group_series,
    normalise_unit,
    refusal_reason,
    series_counts,
    substance_from_code_canonical,
)
from app.dashboard import units as units_mod

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

# The two code_canonical shapes that actually exist, measured per CDR (#733).
GLUCOSE_LOINC = "https://termbank.pdhc.se/CodeSystem/loinc/15074-8"   # cdr2/3
CREATININE_LOINC = "https://termbank.pdhc.se/CodeSystem/loinc/14682-9"
HBA1C_LOINC = "https://termbank.pdhc.se/CodeSystem/loinc/4548-4"
WEIGHT_LOINC = "https://termbank.pdhc.se/CodeSystem/loinc/29463-7"
CDR1_CONCEPT = "urn:pdhc:concept/3f1a9c22-0000-4000-8000-000000000001"  # cdr1


def row(code, value, unit, *, at="2026-11-01T08:00:00Z", guid=None, **extra):
    r = {
        "observation_guid": guid or f"obs-{code[-4:]}-{unit}-{value}-{at}",
        "code_canonical": code,
        "value": value,
        "unit": unit,
        "effective_at": at,
    }
    r.update(extra)
    return r


class ObjRow:
    """Rows arrive as objects as well as dicts; the module must take both."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------

def test_normalise_collapses_absent_unit_spellings_to_none():
    assert normalise_unit(None) is None
    assert normalise_unit("") is None
    assert normalise_unit("   ") is None


def test_normalise_uses_the_spellings_this_repo_actually_writes():
    # app/templates/nurse_workspace.html:79-85 writes mmHg, kg, kg/m2, %
    assert normalise_unit("mmHg") == "mm[Hg]"
    assert normalise_unit(" mm[Hg] ") == "mm[Hg]"
    assert normalise_unit("kg/m^2") == "kg/m2"
    assert normalise_unit("MMOL/L") == "mmol/L"
    assert normalise_unit("µmol/L") == "umol/L"   # MICRO SIGN
    assert normalise_unit("μmol/L") == "umol/L"   # GREEK MU
    assert normalise_unit("bpm") == "/min"


def test_normalise_keeps_an_unrecognised_spelling_as_itself():
    # Not None, not dropped: it is a unit somebody recorded.
    assert normalise_unit(" furlong ") == "furlong"
    assert dimension_of("furlong") is None


def test_canonical_tokens_do_not_collide_under_casefold():
    # The alias map is case-insensitive over a closed table. Adding `Mg`
    # (megagram) next to `mg` must be caught here, not discovered as every
    # milligram having become a tonne.
    tokens = list(units_mod._UNITS) + list(units_mod._IRREDUCIBLE)
    assert len({t.casefold() for t in tokens}) == len(tokens)


# ---------------------------------------------------------------------------
# convertibility -- the straightforward half
# ---------------------------------------------------------------------------

def test_same_unit_is_convertible_and_conversion_is_identity():
    assert are_convertible("kg", "kg")
    assert convert(70.5, "kg", "kg") == 70.5
    assert are_convertible("mmHg", "mm[Hg]")   # same unit, two spellings


def test_mass_converts_within_its_dimension():
    assert dimension_of("kg") == MASS
    assert convert(1.0, "kg", "g") == pytest.approx(1000.0)
    assert convert(72000.0, "g", "kg") == pytest.approx(72.0)
    assert convert(500.0, "mg", "g") == pytest.approx(0.5)
    # exact international avoirdupois pound
    assert convert(1.0, "[lb_av]", "g") == pytest.approx(453.59237)


def test_pressure_converts_and_mmhg_is_the_base():
    assert dimension_of("mmHg") == PRESSURE
    # 120 mmHg is 16.0 kPa (1 mm[Hg] = 133.322387415 Pa exactly)
    assert convert(120.0, "mmHg", "kPa") == pytest.approx(15.9987, abs=1e-4)
    assert convert(16.0, "kPa", "mm[Hg]") == pytest.approx(120.01, abs=1e-2)


def test_volume_time_count_and_rate_dimensions_convert():
    assert convert(1.0, "L", "mL") == pytest.approx(1000.0)
    assert convert(5.0, "dL", "L") == pytest.approx(0.5)
    assert convert(2.0, "h", "min") == pytest.approx(120.0)
    assert convert(1.0, "wk", "d") == pytest.approx(7.0)
    assert convert(60.0, "/min", "/s") == pytest.approx(1.0)
    assert are_convertible("{count}", "1")
    assert convert(3.0, "count", "1") == pytest.approx(3.0)


def test_percent_groups_with_percent_but_has_no_other_member():
    assert are_convertible("%", "%")
    assert convert(7.0, "%", "percent") == 7.0


# ---------------------------------------------------------------------------
# REFUSAL 1 -- molar vs mass concentration needs the substance
# ---------------------------------------------------------------------------

def test_mmol_per_l_and_mg_per_dl_are_not_convertible_from_the_units_alone():
    # The headline refusal. mmol/L is amount-of-substance per volume and
    # mg/dL is mass per volume; the bridge is the analyte's molar mass, which
    # is in neither unit.
    assert dimension_of("mmol/L") == MOLAR_CONCENTRATION
    assert dimension_of("mg/dL") == MASS_CONCENTRATION
    assert are_convertible("mmol/L", "mg/dL") is False
    assert are_convertible("mg/dL", "mmol/L") is False
    reason = refusal_reason("mmol/L", "mg/dL")
    assert reason is not None
    assert "MOLAR MASS" in reason


def test_convert_raises_rather_than_guessing_a_glucose_factor():
    with pytest.raises(UnitConversionRefused) as exc:
        convert(5.5, "mmol/L", "mg/dL")
    assert "MOLAR MASS" in exc.value.reason
    assert exc.value.from_unit == "mmol/L"
    assert exc.value.to_unit == "mg/dL"


def test_convertible_within_each_concentration_dimension_separately():
    # Refusing the crossing must not break either dimension's own ladder.
    assert convert(5.0, "mmol/L", "umol/L") == pytest.approx(5000.0)
    assert convert(90.0, "mg/dL", "mg/L") == pytest.approx(900.0)
    assert are_convertible("mmol/L", "mol/L")
    assert are_convertible("mg/dL", "g/L")


def test_glucose_loinc_code_establishes_the_substance_and_the_bridge_opens():
    assert substance_from_code_canonical(GLUCOSE_LOINC) == "glucose"
    assert are_convertible("mmol/L", "mg/dL", substance="glucose")
    # 5.5 mmol/L glucose = 5.5 * 180.156 / 10 = 99.086 mg/dL
    assert convert(5.5, "mmol/L", "mg/dL",
                   substance="glucose") == pytest.approx(99.0858, abs=1e-4)
    # and back
    assert convert(99.0858, "mg/dL", "mmol/L",
                   substance="glucose") == pytest.approx(5.5, abs=1e-4)


def test_the_factor_is_substance_specific_glucose_differs_from_creatinine():
    # THE reason the units alone cannot answer. Same unit pair, same input,
    # two different correct answers.
    as_glucose = convert(1.0, "mg/dL", "umol/L", substance="glucose")
    as_creatinine = convert(1.0, "mg/dL", "umol/L", substance="creatinine")
    assert as_creatinine == pytest.approx(88.41, abs=0.01)   # textbook 88.4
    assert as_glucose == pytest.approx(55.51, abs=0.01)
    assert not math.isclose(as_glucose, as_creatinine, rel_tol=0.01)


def test_an_unlisted_substance_does_not_open_the_bridge():
    assert are_convertible("mmol/L", "mg/dL", substance="unobtainium") is False
    with pytest.raises(UnitConversionRefused):
        convert(1.0, "mmol/L", "mg/dL", substance="unobtainium")


def test_substance_resolution_handles_every_code_canonical_shape_in_use():
    assert substance_from_code_canonical(
        "https://termbank.pdhc.se/CodeSystem/loinc/2339-0") == "glucose"
    # the FHIR search-token form app/routes/nurse.py:293 writes
    assert substance_from_code_canonical(
        "https://termbank.pdhc.se/CodeSystem/loinc|41653-7") == "glucose"
    assert substance_from_code_canonical("http://loinc.org/2160-0") == "creatinine"
    assert substance_from_code_canonical("14682-9") == "creatinine"
    assert substance_from_code_canonical("99999-9") is None
    assert substance_from_code_canonical(None) is None


def test_cdr1_concept_guid_establishes_no_substance():
    # cdr1's code_canonical is an opaque plan.pdhc GUID. Resolving it needs
    # HTTP, which this module does not do -- so it must NOT be guessed at.
    assert substance_from_code_canonical(CDR1_CONCEPT) is None
    assert substance_from_code_canonical("urn:pdhc:concept/smoke-glucose") is None


# ---------------------------------------------------------------------------
# REFUSAL 2 -- an affine relation is not a unit conversion
# ---------------------------------------------------------------------------

def test_hba1c_percent_to_mmol_per_mol_is_refused_even_though_the_ratio_exists():
    # 1 mmol/mol IS 0.1 % as a pure ratio, and applying that to HbA1c gives
    # 70 mmol/mol for 7 % where the true answer is 53. The relation is affine.
    assert are_convertible("%", "mmol/mol") is False
    reason = refusal_reason("%", "mmol/mol")
    assert "AFFINE" in reason and "HbA1c" in reason
    with pytest.raises(UnitConversionRefused):
        convert(7.0, "%", "mmol/mol")


def test_knowing_the_substance_does_not_rescue_an_affine_relation():
    # The contrast with glucose: there, naming the analyte was enough. Here it
    # is not, because convert() is multiplicative by construction.
    assert substance_from_code_canonical(HBA1C_LOINC) == "hba1c"
    assert are_convertible("%", "mmol/mol", substance="hba1c") is False
    with pytest.raises(UnitConversionRefused):
        convert(7.0, "%", "mmol/mol", substance="hba1c")


def test_fahrenheit_to_celsius_is_refused_as_affine():
    assert are_convertible("degF", "Cel") is False
    assert "AFFINE" in refusal_reason("°F", "Cel")
    with pytest.raises(UnitConversionRefused):
        convert(98.6, "degF", "Cel")
    assert are_convertible("Cel", "Cel")      # still groups with itself


# ---------------------------------------------------------------------------
# REFUSAL 3 -- a calendar unit is not a duration
# ---------------------------------------------------------------------------

def test_calendar_month_and_year_do_not_convert_to_durations():
    assert are_convertible("mo", "d") is False
    assert are_convertible("a", "s") is False
    assert "calendar month" in refusal_reason("mo", "d")
    with pytest.raises(UnitConversionRefused):
        convert(1.0, "mo", "d")
    assert are_convertible("mo", "mo")        # groups with itself


# ---------------------------------------------------------------------------
# REFUSAL 4 -- an unknown unit is not a missing unit
# ---------------------------------------------------------------------------

def test_an_unrecognised_unit_is_convertible_with_nothing_but_itself():
    assert are_convertible("furlong", "furlong")
    assert are_convertible("furlong", "kg") is False
    assert are_convertible("kg", "furlong") is False
    assert are_convertible("furlong", "fathom") is False
    assert "not in this module's unit table" in refusal_reason("furlong", "kg")
    with pytest.raises(UnitConversionRefused):
        convert(1.0, "furlong", "kg")


def test_an_absent_unit_is_not_a_wildcard():
    assert are_convertible(None, None)        # two unitless rows are one series
    assert are_convertible("", None)          # "" means "not recorded" too
    assert are_convertible(None, "kg") is False
    assert are_convertible("kg", "") is False
    assert "not a wildcard" in refusal_reason(None, "kg")
    with pytest.raises(UnitConversionRefused):
        convert(1.0, None, "kg")


def test_an_unknown_unit_and_an_absent_unit_are_different_things():
    # "furlong" is a unit somebody RECORDED and we cannot read; None is no
    # unit at all. Merging them would fold a measurement into a unitless
    # series and relabel it. This is the assertion mutation (c) must break.
    assert normalise_unit("furlong") is not None
    assert are_convertible("furlong", None) is False
    assert are_convertible(None, "furlong") is False
    with pytest.raises(UnitConversionRefused):
        convert(1.0, "furlong", None)

    groups = group_series([
        row(WEIGHT_LOINC, 70.0, "furlong", at="2026-11-01T08:00:00Z"),
        row(WEIGHT_LOINC, 71.0, None, at="2026-11-02T08:00:00Z"),
    ])
    assert len(groups) == 2
    assert {g.unit for g in groups} == {"furlong", None}


def test_different_dimensions_do_not_convert():
    assert are_convertible("kg", "mmHg") is False
    assert are_convertible("kg", "L") is False          # needs density
    assert "DENSITY" in refusal_reason("kg", "L")
    assert are_convertible("mg/dL", "%") is False
    assert are_convertible("cm", "s") is False


def test_are_convertible_and_refusal_reason_never_disagree():
    tokens = [None, "", "kg", "g", "mmHg", "kPa", "L", "mL", "s", "min", "mo",
              "%", "mmol/mol", "mmol/L", "mg/dL", "umol/L", "Cel", "degF",
              "furlong", "/min", "1", "kg/m2"]
    for a in tokens:
        for b in tokens:
            for substance in (None, "glucose", "hba1c"):
                ok = are_convertible(a, b, substance=substance)
                reason = refusal_reason(a, b, substance=substance)
                assert ok is (reason is None), (a, b, substance)
                if ok:
                    # convertible means convert() must not raise
                    convert(1.0, a, b, substance=substance)
                else:
                    with pytest.raises(UnitConversionRefused):
                        convert(1.0, a, b, substance=substance)


def test_convert_rejects_a_non_numeric_value_rather_than_coercing():
    with pytest.raises(TypeError):
        convert(True, "kg", "g")          # bool is an int but not a datum
    with pytest.raises(TypeError):
        convert("heavy", "kg", "g")


# ---------------------------------------------------------------------------
# group_series
# ---------------------------------------------------------------------------

def test_identical_units_form_one_group():
    groups = group_series([
        row(WEIGHT_LOINC, 70.0, "kg", at="2026-11-01T08:00:00Z", guid="a"),
        row(WEIGHT_LOINC, 71.0, "kg", at="2026-11-02T08:00:00Z", guid="b"),
        row(WEIGHT_LOINC, 72.0, "kg", at="2026-11-03T08:00:00Z", guid="c"),
    ])
    assert len(groups) == 1
    g = groups[0]
    assert g.unit == "kg"
    assert g.count == 3 and g.has_series is True
    assert [p.value for p in g.points] == [70.0, 71.0, 72.0]
    assert g.converted is False
    assert g.refused is None


def test_convertible_units_form_one_group_with_values_converted():
    groups = group_series([
        row(WEIGHT_LOINC, 70.0, "kg", at="2026-11-01T08:00:00Z", guid="a"),
        row(WEIGHT_LOINC, 71.0, "kg", at="2026-11-02T08:00:00Z", guid="b"),
        row(WEIGHT_LOINC, 72000.0, "g", at="2026-11-03T08:00:00Z", guid="c"),
    ])
    assert len(groups) == 1
    g = groups[0]
    assert g.unit == "kg"                      # majority unit
    assert g.source_units == ("g", "kg")
    assert g.converted is True
    assert [p.value for p in g.points] == pytest.approx([70.0, 71.0, 72.0])
    assert {p.unit for p in g.points} == {"kg"}   # every point relabelled


def test_the_majority_unit_wins_so_a_gram_series_is_not_relabelled_to_kg():
    groups = group_series([
        row(WEIGHT_LOINC, 500.0, "g", at="2026-11-01T08:00:00Z", guid="a"),
        row(WEIGHT_LOINC, 600.0, "g", at="2026-11-02T08:00:00Z", guid="b"),
        row(WEIGHT_LOINC, 0.7, "kg", at="2026-11-03T08:00:00Z", guid="c"),
    ])
    assert groups[0].unit == "g"
    assert [p.value for p in groups[0].points] == pytest.approx([500.0, 600.0, 700.0])


def test_a_tie_is_broken_by_the_preferred_unit_not_alphabetically():
    # 1 kg and 1 g: "g" < "kg", so a purely lexicographic tie-break would
    # chart a body weight in grams.
    groups = group_series([
        row(WEIGHT_LOINC, 70.0, "kg", at="2026-11-01T08:00:00Z", guid="a"),
        row(WEIGHT_LOINC, 71000.0, "g", at="2026-11-02T08:00:00Z", guid="b"),
    ])
    assert groups[0].unit == "kg"
    assert [p.value for p in groups[0].points] == pytest.approx([70.0, 71.0])


def test_a_bridged_concentration_tie_is_charted_in_mmol_per_litre():
    # The Sweden / IFCC convention this repo already states at
    # app/analyse/federation.py:534.
    groups = group_series([
        row(GLUCOSE_LOINC, 5.5, "mmol/L", at="2026-11-01T08:00:00Z", guid="a"),
        row(GLUCOSE_LOINC, 99.0858, "mg/dL", at="2026-11-02T08:00:00Z", guid="b"),
    ])
    assert len(groups) == 1
    assert groups[0].unit == "mmol/L"
    assert [p.value for p in groups[0].points] == pytest.approx([5.5, 5.5],
                                                                abs=1e-4)


def test_non_convertible_units_split_into_separate_groups():
    groups = group_series([
        row(WEIGHT_LOINC, 70.0, "kg", at="2026-11-01T08:00:00Z", guid="a"),
        row(WEIGHT_LOINC, 70.0, "kg", at="2026-11-02T08:00:00Z", guid="b"),
        row(WEIGHT_LOINC, 120.0, "mmHg", at="2026-11-03T08:00:00Z", guid="c"),
    ])
    assert len(groups) == 2
    assert [g.unit for g in groups] == ["kg", "mm[Hg]"]
    assert [g.count for g in groups] == [2, 1]
    # and each group says WHY it is separate, so the panel can print it
    assert all(g.refused for g in groups)
    assert "different quantities" in groups[0].refused


def test_mmol_and_mg_per_dl_split_when_the_substance_is_unknown():
    # cdr1 shape: the identity is an opaque GUID, so the molar mass is not
    # knowable and these MUST become two charts rather than one wrong one.
    rows = [
        row(CDR1_CONCEPT, 5.5, "mmol/L", at="2026-11-01T08:00:00Z", guid="a"),
        row(CDR1_CONCEPT, 6.1, "mmol/L", at="2026-11-02T08:00:00Z", guid="b"),
        row(CDR1_CONCEPT, 99.0, "mg/dL", at="2026-11-03T08:00:00Z", guid="c"),
    ]
    groups = group_series(rows)
    assert len(groups) == 2
    assert {g.unit for g in groups} == {"mmol/L", "mg/dL"}
    # no value was touched
    values = sorted(p.value for g in groups for p in g.points)
    assert values == pytest.approx([5.5, 6.1, 99.0])
    assert all("MOLAR MASS" in g.refused for g in groups)
    assert all(g.substance is None for g in groups)


def test_mmol_and_mg_per_dl_join_when_a_loinc_code_names_glucose():
    groups = group_series([
        row(GLUCOSE_LOINC, 5.5, "mmol/L", at="2026-11-01T08:00:00Z", guid="a"),
        row(GLUCOSE_LOINC, 6.1, "mmol/L", at="2026-11-02T08:00:00Z", guid="b"),
        row(GLUCOSE_LOINC, 99.0858, "mg/dL", at="2026-11-03T08:00:00Z", guid="c"),
    ])
    assert len(groups) == 1
    g = groups[0]
    assert g.unit == "mmol/L"
    assert g.substance == "glucose"
    assert [p.value for p in g.points] == pytest.approx([5.5, 6.1, 5.5], abs=1e-4)


def test_an_injected_resolver_can_open_the_bridge_for_a_cdr1_guid():
    # Proves the refusal is about not KNOWING the substance, not a dead path:
    # a caller that already resolved the concept against plan.pdhc may say so.
    rows = [
        row(CDR1_CONCEPT, 5.5, "mmol/L", at="2026-11-01T08:00:00Z", guid="a"),
        row(CDR1_CONCEPT, 6.1, "mmol/L", at="2026-11-02T08:00:00Z", guid="b"),
        row(CDR1_CONCEPT, 99.0858, "mg/dL", at="2026-11-03T08:00:00Z", guid="c"),
    ]
    assert len(group_series(rows)) == 2
    joined = group_series(rows, substance_resolver=lambda code: "glucose")
    assert len(joined) == 1
    assert joined[0].substance == "glucose"
    assert [p.value for p in joined[0].points] == pytest.approx(
        [5.5, 6.1, 5.5], abs=1e-4)


def test_hba1c_percent_and_mmol_per_mol_JOIN_via_the_affine_bridge():
    """THE DECISION CHANGED, 2026-11-11, and this test changed with it.

    It used to assert a SPLIT, and that was right for what the module could
    do: `convert()` is multiplicative and the NGSP/IFCC relation is affine, so
    there was no honest way to draw one chart. The operator asked for one
    chart, so the affine function now exists by name in
    `app/dashboard/hba1c.py` and `group_series` uses it.

    WHAT DID NOT CHANGE, and the test below it still proves: `convert()` and
    `are_convertible()` STILL REFUSE the pair. The two units are not
    interconvertible — a caller asking that question must still hear no. What
    is true is narrower: for THIS substance there is a named function, and
    grouping may use it.
    """
    groups = group_series([
        row(HBA1C_LOINC, 7.0, "%", at="2026-11-01T08:00:00Z", guid="a"),
        row(HBA1C_LOINC, 53.0, "mmol/mol", at="2026-11-02T08:00:00Z", guid="b"),
    ])
    assert len(groups) == 1
    g = groups[0]
    # charted in the IFCC unit: Swedish laboratories report mmol/mol
    assert g.unit == "mmol/mol"
    assert [round(p.value) for p in g.points] == [53, 53]
    assert g.converted is True


def test_the_hba1c_bridge_does_NOT_reopen_convert(self_check=None):
    """The separation, asserted from the grouping side.

    If a future edit "simplifies" by putting a factor in the table, this fails
    — and so does the clinical arithmetic, by 32 %.
    """
    from app.dashboard import units as u
    assert u.are_convertible("%", "mmol/mol", substance="hba1c") is False
    assert u.refusal_reason("%", "mmol/mol", substance="hba1c") is not None


def test_different_concepts_never_share_a_group_even_in_the_same_unit():
    groups = group_series([
        row(GLUCOSE_LOINC, 5.5, "mmol/L", guid="a"),
        row(CREATININE_LOINC, 80.0, "umol/L", guid="b"),
    ])
    assert len(groups) == 2
    assert [g.code_canonical for g in groups] == [CREATININE_LOINC, GLUCOSE_LOINC]


def test_rows_may_be_objects_and_carry_reference_range_through():
    groups = group_series([
        ObjRow(observation_guid="a", code_canonical=WEIGHT_LOINC, value=70.0,
               unit="kg", effective_at="2026-11-01T08:00:00Z",
               reference_low=50.0, reference_high=90.0,
               transaction_guid="tx-1"),
        ObjRow(observation_guid="b", code_canonical=WEIGHT_LOINC, value=71000.0,
               unit="g", effective_at="2026-11-02T08:00:00Z"),
    ])
    assert len(groups) == 1
    first = groups[0].points[0]
    assert first.reference_low == 50.0 and first.reference_high == 90.0
    assert first.transaction_guid == "tx-1"
    assert groups[0].points[1].value == pytest.approx(71.0)


def test_a_non_numeric_value_cannot_be_a_chart_point_and_is_dropped():
    groups = group_series([
        row(WEIGHT_LOINC, 70.0, "kg", at="2026-11-01T08:00:00Z", guid="a"),
        row(WEIGHT_LOINC, None, "kg", at="2026-11-02T08:00:00Z", guid="b"),
        row(WEIGHT_LOINC, "heavy", "kg", at="2026-11-03T08:00:00Z", guid="c"),
        row(WEIGHT_LOINC, "71.5", "kg", at="2026-11-04T08:00:00Z", guid="d"),
    ])
    assert len(groups) == 1
    assert [p.observation_guid for p in groups[0].points] == ["a", "d"]
    assert groups[0].points[1].value == pytest.approx(71.5)


def test_empty_input_gives_no_groups():
    assert group_series([]) == []
    assert series_counts([]) == {}


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------

def test_group_order_is_deterministic_and_independent_of_input_order():
    rows = [
        row(WEIGHT_LOINC, 70.0, "kg", at="2026-11-01T08:00:00Z", guid="a"),
        row(WEIGHT_LOINC, 120.0, "mmHg", at="2026-11-02T08:00:00Z", guid="b"),
        row(CDR1_CONCEPT, 5.5, "mmol/L", at="2026-11-03T08:00:00Z", guid="c"),
        row(CDR1_CONCEPT, 99.0, "mg/dL", at="2026-11-04T08:00:00Z", guid="d"),
        row(CREATININE_LOINC, 80.0, "umol/L", at="2026-11-05T08:00:00Z", guid="e"),
        row(CREATININE_LOINC, 81.0, None, at="2026-11-06T08:00:00Z", guid="f"),
    ]
    expected = [(g.code_canonical, g.unit) for g in group_series(rows)]
    # documented order: (code_canonical, unit or ""), so a unitless group of a
    # concept sorts before any of that concept's units, and the LOINC URIs
    # sort before the urn: form
    assert expected == [
        (CREATININE_LOINC, None),
        (CREATININE_LOINC, "umol/L"),
        (WEIGHT_LOINC, "kg"),
        (WEIGHT_LOINC, "mm[Hg]"),
        (CDR1_CONCEPT, "mg/dL"),
        (CDR1_CONCEPT, "mmol/L"),
    ]
    for permutation in (list(reversed(rows)),
                        rows[3:] + rows[:3],
                        [rows[i] for i in (4, 1, 5, 0, 3, 2)]):
        assert [(g.code_canonical, g.unit)
                for g in group_series(permutation)] == expected


def test_points_are_chronological_whatever_order_the_rows_arrive_in():
    rows = [
        row(WEIGHT_LOINC, 72.0, "kg", at="2026-11-03T08:00:00Z", guid="c"),
        row(WEIGHT_LOINC, 70.0, "kg", at="2026-11-01T08:00:00Z", guid="a"),
        row(WEIGHT_LOINC, 71.0, "kg", at="2026-11-02T08:00:00Z", guid="b"),
    ]
    points = group_series(rows)[0].points
    assert [p.observation_guid for p in points] == ["a", "b", "c"]
    assert [p.effective_at for p in points] == sorted(
        p.effective_at for p in points)


def test_identical_timestamps_are_broken_by_guid_so_the_order_is_total():
    at = "2026-11-01T08:00:00Z"
    rows = [
        row(WEIGHT_LOINC, 1.0, "kg", at=at, guid="z"),
        row(WEIGHT_LOINC, 2.0, "kg", at=at, guid="a"),
        row(WEIGHT_LOINC, 3.0, "kg", at=at, guid="m"),
    ]
    assert [p.observation_guid for p in group_series(rows)[0].points] == \
        ["a", "m", "z"]
    assert [p.observation_guid for p in group_series(list(reversed(rows)))[0].points] == \
        ["a", "m", "z"]


def test_series_key_is_stable_and_unique_per_group():
    groups = group_series([
        row(CDR1_CONCEPT, 5.5, "mmol/L", guid="a"),
        row(CDR1_CONCEPT, 99.0, "mg/dL", guid="b"),
    ])
    keys = [g.series_key for g in groups]
    assert keys == [f"{CDR1_CONCEPT}|mg/dL", f"{CDR1_CONCEPT}|mmol/L"]
    assert len(set(keys)) == len(keys)


# ---------------------------------------------------------------------------
# series_counts -- what fills EventRow.series_count / has_series
# ---------------------------------------------------------------------------

def test_a_single_point_concept_has_series_false_and_count_one():
    counts = series_counts([row(WEIGHT_LOINC, 70.0, "kg", guid="only")])
    assert counts["only"] == SeriesCount(count=1, has_series=False,
                                         series_key=f"{WEIGHT_LOINC}|kg",
                                         unit="kg")
    # and the group agrees
    assert group_series([row(WEIGHT_LOINC, 70.0, "kg", guid="only")])[0].has_series \
        is False


def test_counts_are_per_series_not_per_concept_so_a_split_is_reported_honestly():
    counts = series_counts([
        row(CDR1_CONCEPT, 5.5, "mmol/L", at="2026-11-01T08:00:00Z", guid="a"),
        row(CDR1_CONCEPT, 6.1, "mmol/L", at="2026-11-02T08:00:00Z", guid="b"),
        row(CDR1_CONCEPT, 99.0, "mg/dL", at="2026-11-03T08:00:00Z", guid="c"),
    ])
    # per concept there are 3; per series it is 2 and 1, and only the first
    # pair is plottable together
    assert counts["a"].count == 2 and counts["a"].has_series is True
    assert counts["b"].count == 2
    assert counts["c"].count == 1 and counts["c"].has_series is False
    assert counts["a"].series_key == counts["b"].series_key
    assert counts["c"].series_key != counts["a"].series_key


def test_a_converted_point_counts_in_the_group_it_was_converted_into():
    counts = series_counts([
        row(GLUCOSE_LOINC, 5.5, "mmol/L", at="2026-11-01T08:00:00Z", guid="a"),
        row(GLUCOSE_LOINC, 99.0, "mg/dL", at="2026-11-02T08:00:00Z", guid="b"),
    ])
    assert counts["a"].count == 2 and counts["b"].count == 2
    assert counts["a"].series_key == counts["b"].series_key
    assert counts["b"].unit == "mmol/L"


def test_counts_are_computed_in_ONE_grouped_pass_not_n_plus_1(monkeypatch):
    # The plan's step 7 forbids an N+1. Assert the mechanism, not the timing:
    # series_counts must group the whole page exactly once, however many rows
    # it is given.
    calls = []
    real = units_mod.group_series

    def counting(rows, **kw):
        rows = list(rows)
        calls.append(len(rows))
        return real(rows, **kw)

    monkeypatch.setattr(units_mod, "group_series", counting)

    rows = [row(WEIGHT_LOINC, float(i), "kg",
                at=f"2026-11-{i + 1:02d}T08:00:00Z", guid=f"g{i}")
            for i in range(40)]
    counts = units_mod.series_counts(rows)
    assert len(counts) == 40
    assert calls == [40]          # one call, carrying every row


def test_counts_cover_exactly_the_rows_that_became_points():
    rows = [
        row(WEIGHT_LOINC, 70.0, "kg", at="2026-11-01T08:00:00Z", guid="a"),
        row(WEIGHT_LOINC, "n/a", "kg", at="2026-11-02T08:00:00Z", guid="b"),
    ]
    counts = series_counts(rows)
    groups = group_series(rows)
    assert set(counts) == {p.observation_guid for g in groups for p in g.points}
    assert "b" not in counts     # absent, rather than a 0 that reads as a count


def test_counts_fill_an_event_row_without_touching_the_dto():
    rows = [
        row(WEIGHT_LOINC, 70.0, "kg", at="2026-11-01T08:00:00Z", guid="a"),
        row(WEIGHT_LOINC, 71.0, "kg", at="2026-11-02T08:00:00Z", guid="b"),
    ]
    counts = series_counts(rows)
    built = [
        EventRow(observation_guid=r["observation_guid"],
                 code_canonical=r["code_canonical"], display_name="Body weight",
                 value=r["value"], unit=r["unit"],
                 effective_at=r["effective_at"], source_cdr="cdr1",
                 has_series=counts[r["observation_guid"]].has_series,
                 series_count=counts[r["observation_guid"]].count)
        for r in rows
    ]
    assert all(e.has_series is True and e.series_count == 2 for e in built)


def test_a_group_converts_to_the_shared_dto_series():
    group = group_series([
        row(WEIGHT_LOINC, 70.0, "kg", at="2026-11-01T08:00:00Z", guid="a"),
        row(WEIGHT_LOINC, 71000.0, "g", at="2026-11-02T08:00:00Z", guid="b"),
    ])[0]
    series = group.to_series(display_name="Body weight")
    assert isinstance(series, Series)
    assert series.code_canonical == WEIGHT_LOINC
    assert series.unit == "kg"            # dto.Series is "points in ONE unit"
    assert len(series.points) == 2
    assert series.points[1].value == pytest.approx(71.0)
    assert series.downsampled_from is None
