"""#974 (PDL): the filter bar's concept list must not disclose spärr-hidden
data -- not by count, not by date range, not by a concept's mere name.

cdr1's summary counts every row; with an active block the list is now built
from the VISIBLE points. And when ips cannot be asked, the dashboard refuses
with 503 `undetermined` (fail CLOSED, as access.sparr_state always intended)
instead of serving blocked rows with `sparrChecked: true`."""
from __future__ import annotations

from app.dashboard import fixtures as fx

HF = fx.PATIENT_HF


def _visible_counts(app, client):
    rows, cur = [], None
    while True:
        b = client.get(f"/dashboard/patients/{HF}/events?limit=500" + (f"&cursor={cur}" if cur else "")).get_json()
        rows += b["rows"]
        cur = b.get("nextCursor")
        if not cur:
            break
    out = {}
    for r in rows:
        out[r["codeCanonical"]] = out.get(r["codeCanonical"], 0) + 1
    return out


def test_concept_counts_equal_what_the_event_list_SHOWS(fixtures_app, fixtures_client):
    b = fixtures_client.get(f"/dashboard/patients/{HF}/concepts").get_json()
    assert b["hasBlockedSources"] is True and b["countsComplete"] is True
    listed = {c["code"]: c["count"] for c in b["concepts"]}
    assert listed == _visible_counts(fixtures_app, fixtures_client)
    raw = {}
    for o in fx.build_dataset().patient_observations(HF):
        code = fx.code_of_observation(o) if hasattr(fx, "code_of_observation") else o.get("code")
        raw[code] = raw.get(code, 0) + 1
    assert sum(raw.values()) > sum(listed.values()), "control: some rows really are blocked"


def test_without_blocks_the_summary_is_used_unchanged(fixtures_client):
    b = fixtures_client.get(f"/dashboard/patients/{fx.PATIENT_T2D}/concepts").get_json()
    assert b["hasBlockedSources"] is False and b["concepts"]


def test_ips_down_FAILS_CLOSED_by_name_on_every_patient_endpoint(fixtures_app, fixtures_client):
    with fx.failing(fixtures_app, "ips"):
        for ep in ("", "/plan", "/concepts", "/events"):
            r = fixtures_client.get(f"/dashboard/patients/{HF}{ep}")
            assert r.status_code == 503 and r.get_json()["error"] == "undetermined", ep
    assert fixtures_client.get(f"/dashboard/patients/{HF}/events").status_code == 200   # control
