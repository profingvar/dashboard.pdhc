"""#974: a slow source cannot hold a dashboard request past gunicorn's
timeout. The scans stop at a per-request budget and SAY they are incomplete,
exactly as at their page bound -- never a bare 502 from a killed worker."""
from __future__ import annotations

from app.dashboard import fixtures as fx

CGM = fx.PATIENT_CGM


def _series(client):
    code = client.get(f"/dashboard/patients/{CGM}/concepts").get_json()["concepts"][0]["code"]
    from urllib.parse import quote
    return client.get(f"/dashboard/patients/{CGM}/series/{quote(code, safe='')}").get_json()


def test_within_budget_the_dense_series_is_complete(fixtures_client):
    assert _series(fixtures_client)["complete"] is True


def test_past_its_budget_a_scan_stops_and_says_so(fixtures_app, fixtures_client):
    fixtures_app.config["DASHBOARD_SCAN_BUDGET_S"] = -1
    try:
        b = _series(fixtures_client)
        assert b["complete"] is False
        plan = fixtures_client.get(f"/dashboard/patients/{CGM}/plan").get_json()
        assert plan["observationCountsComplete"] is False
    finally:
        fixtures_app.config.pop("DASHBOARD_SCAN_BUDGET_S")
