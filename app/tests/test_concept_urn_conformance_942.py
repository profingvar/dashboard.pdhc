"""#942 — this reader implements plan.pdhc's concept reference format.

conformance/concept_urn_cases.json is a BYTE-IDENTICAL copy of
plan.pdhc/conformance/concept_urn_cases.json (workspace check:
tools/check_canonical_uri_conformance.py)."""
import json
import pathlib

import pytest

from app.services.ips_client import concept_guid_from_canonical

CORPUS = json.loads((pathlib.Path(__file__).parent / "conformance" / "concept_urn_cases.json").read_text())


@pytest.mark.parametrize("c", CORPUS["guid_of"], ids=lambda c: repr(c["value"]))
def test_guid_of(c):
    assert concept_guid_from_canonical(c["value"]) == c["guid"]
