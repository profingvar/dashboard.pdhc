"""#1014 — the professional sees which data is patient-generated.

cdr1 returns `data_origin` per point ('patient_generated' or null = produced
or endorsed by healthcare). Pinned: it reaches the event list, the detail
panel, the chart series and the CSV export unchanged, and an unmarked point
stays null (never guessed).
"""
from __future__ import annotations

from app.tests import test_dashboard_detail as det
from app.tests import test_dashboard_events as ev
from app.tests import test_dashboard_series as ser


def _pgd(pt):
    return dict(pt, data_origin="patient_generated")


def test_event_rows_carry_data_origin(monkeypatch):
    app = ev._app()
    ev._wire(monkeypatch, ev._Cdr1(ev._page([
        _pgd(ev._pt("o2", "2026-04-12T10:00:00+00:00")),
        ev._pt("o1", "2026-04-08T10:00:00+00:00")])))
    status, body = ev._get(app)
    assert status == 200
    by = {r["observationGuid"]: r for r in body["rows"]}
    assert by["o2"]["dataOrigin"] == "patient_generated"
    assert "dataOrigin" in by["o1"] and by["o1"]["dataOrigin"] is None


def test_detail_row_carries_data_origin(monkeypatch):
    app = det._app()
    d = det._detail()
    d["context"]["data_origin"] = "patient_generated"
    det._wire(monkeypatch, det._Cdr1(d))
    status, body = det._get(app)
    assert status == 200
    assert body["detail"]["row"]["dataOrigin"] == "patient_generated"
    assert body["context"]["data_origin"] == "patient_generated"


def test_detail_without_origin_is_healthcare_null(monkeypatch):
    app = det._app()
    det._wire(monkeypatch, det._Cdr1(det._detail()))
    _, body = det._get(app)
    assert body["detail"]["row"]["dataOrigin"] is None


def test_series_points_and_csv_carry_data_origin(monkeypatch):
    app = ser._app()
    pts = [ser._pt(1, 70.0, "2026-01-01T00:00:00Z"),
           _pgd(ser._pt(2, 71.0, "2026-02-01T00:00:00Z"))]
    ser._wire(monkeypatch, ser._SeriesCdr1([pts]))
    body = ser._get(app).get_json()
    got = {p["observationGuid"]: p["dataOrigin"] for p in body["series"][0]["points"]}
    assert got == {"obs-00001": None, "obs-00002": "patient_generated"}

    ser._wire(monkeypatch, ser._SeriesCdr1([pts]))
    lines = ser._get(app, "csv").get_data(as_text=True).splitlines()
    header = [l for l in lines if l.startswith("code_canonical,")][0]
    assert header.endswith(",limit_status,data_origin")
    rows = {l.split(",")[4]: l.split(",")[-1] for l in lines
            if l.startswith("urn:")}
    assert rows == {"obs-00001": "", "obs-00002": "patient_generated"}


def test_the_page_script_marks_patient_generated_data():
    """The JS is not unit-tested here; pin that the markers exist so a
    refactor cannot drop them silently."""
    import pathlib
    js = (pathlib.Path(__file__).resolve().parents[1] / "static/js/dashboard.js").read_text()
    assert "function isPatientGenerated" in js
    assert "'origin-badge'" in js and "'Ursprung'" in js
    assert "pg: isPatientGenerated(q)" in js
