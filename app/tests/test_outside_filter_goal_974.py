"""#974 finding, fixed 2026-11-17 (MDR IIa): the "Utanför gräns" filter must
return EXACTLY the values the list itself marks outside.

It narrowed cdr1's scan to the transactions that carry a limit, but a CarePlan
GOAL judges a value by its concept alone -- so the T2D fixture's HbA1c values
with no transaction (54, 57, ...) were marked "över" in the list and in the
chart and were MISSING from the filter a clinician uses to find them.
"""
from __future__ import annotations

import pytest

from app.dashboard import fixtures as fx


def _all_rows(c, guid, extra=""):
    rows, cur = [], None
    while True:
        url = f"/dashboard/patients/{guid}/events?limit=500{extra}" + (f"&cursor={cur}" if cur else "")
        r = c.get(url)
        assert r.status_code == 200, r.get_json()
        b = r.get_json()
        rows += b["rows"]
        cur = b.get("nextCursor")
        if not cur:
            return rows


def _key(r):
    assert r.get("observationGuid"), r
    return r["observationGuid"]


@pytest.mark.parametrize("patient", [fx.PATIENT_T2D, fx.PATIENT_HF, fx.PATIENT_CGM])
def test_the_filter_returns_exactly_what_the_list_marks_outside(fixtures_client, patient):
    listed = _all_rows(fixtures_client, patient)
    marked = {_key(r) for r in listed if r["limit"]["outside"]}
    assert marked, "control: the fixture has outside values for this patient"
    out = fixtures_client.get(f"/dashboard/patients/{patient}/events?outside=1").get_json()
    assert out["outsideScanComplete"] is True
    assert {_key(r) for r in out["rows"]} == marked


def test_an_outside_value_WITHOUT_a_transaction_is_found(fixtures_client):
    listed = _all_rows(fixtures_client, fx.PATIENT_T2D)
    orphans = {_key(r) for r in listed if r["limit"]["outside"] and not r.get("transactionGuid")}
    assert orphans, "control: the defect's shape exists in the fixture"
    out = fixtures_client.get(f"/dashboard/patients/{fx.PATIENT_T2D}/events?outside=1").get_json()
    assert orphans <= {_key(r) for r in out["rows"]}
