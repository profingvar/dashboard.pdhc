"""#936 — the nurse view reads a medication in R4, R5 and ips's bare shape.

It read only `medicationCodeableConcept`, so an R5 MedicationStatement (whose
`medication` is a CodeableReference) showed as a regimen with no name.
"""
import ast
import inspect
import textwrap

import pytest

from app.routes import nurse

ATC = {"system": "https://termbank.pdhc.se/CodeSystem/atc", "code": "A10BA02", "display": "metformin"}
SHAPES = {
    "r4": {"medicationCodeableConcept": {"coding": [ATC]}},
    "r5": {"medication": {"concept": {"coding": [ATC]}}},
    "ips_bare": {"medication": {"coding": [ATC]}},
}


@pytest.mark.parametrize("shape", list(SHAPES))
def test_every_shape_gives_the_same_regimen(shape):
    r = {"resourceType": "MedicationStatement", "status": "recorded",
         "effectivePeriod": {"start": "2026-01-01"}, **SHAPES[shape]}
    assert nurse._regimen(r) == {
        "canonical": nurse._coding_uri({"coding": [ATC]}),
        "display": "metformin", "start": "2026-01-01", "status": "recorded"}
    assert nurse._regimen(r)["canonical"]


def test_a_reference_only_medication_is_nameless_not_an_error():
    r = {"resourceType": "MedicationStatement", "medication": {"reference": {"reference": "Medication/x"}}}
    assert nurse._regimen(r)["canonical"] is None


def test_the_page_builds_regimens_through_the_helper():
    src = textwrap.dedent(inspect.getsource(inspect.unwrap(nurse.patient_summary)))
    called = {n.func.id for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_regimen" in called
