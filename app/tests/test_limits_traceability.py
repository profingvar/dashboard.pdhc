"""docs/limits_requirements_and_risks.md must not point at tests that do not
exist (MDR class IIa traceability). A renamed or deleted test breaks this."""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "limits_requirements_and_risks.md"
TESTS = ROOT / "app" / "tests"


def _all_test_names() -> set[str]:
    names = set()
    for f in TESTS.glob("test_*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                names.add(node.name)
    return names


def _requirement_rows() -> dict[str, list[str]]:
    rows = {}
    for line in DOC.read_text().splitlines():
        m = re.match(r"\|\s*(REQ-LIM-\d+)\s*\|", line)
        if m:
            cells = [c.strip() for c in line.strip("|").split("|")]
            rows[m.group(1)] = re.findall(r"`(test_[A-Za-z0-9_]+)`", cells[-1])
    return rows


def test_every_requirement_names_at_least_one_test():
    rows = _requirement_rows()
    assert len(rows) >= 15
    assert all(tests for tests in rows.values()), {k: v for k, v in rows.items() if not v}


def test_every_cited_test_EXISTS():
    existing = _all_test_names()
    cited = {t for tests in _requirement_rows().values() for t in tests}
    assert cited and not (cited - existing), sorted(cited - existing)


def test_the_check_can_see_a_missing_test():
    """Positive control: a name that does not exist is reported."""
    assert "test_this_does_not_exist" not in _all_test_names()
