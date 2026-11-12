"""GET /dashboard/patients/<guid>/series/<code> (#978).

Read through the STRICT cursor endpoint: points keep their identity, a
failure is named, dense series are LTTB-downsampled server-side with the
count said, and CSV export is the same audited read.
"""
from __future__ import annotations

import pytest

import app.routes.dashboard as dash
from app.services.cdr1_client import Cdr1Unreachable
from app.tests.test_dashboard_events import CODE, _app, _page, _wire


def _pt(i, value, at, unit="kg", tx="tx-1"):
    return {"code": CODE, "at": at, "value": value, "unit": unit,
            "value_string": None, "org_guid": "org-a",
            "observation_guid": f"obs-{i:05d}", "transaction_guid": tx,
            "ingest_raw_guid": f"i-{i}", "care_plan_guid": None}


class _SeriesCdr1:
    base_url = "https://cdr.example"

    def __init__(self, pages, raise_=None):
        self.pages, self.raise_, self.calls = pages, raise_, []

    def list_org_patients(self, orgs, *, is_admin=False, reason=None):
        return [{"guid": "p1"}]

    def patient_events(self, guid, orgs, *, cursor=None, limit=100, codes=None, **kw):
        self.calls.append({"cursor": cursor, "codes": codes})
        if self.raise_:
            raise self.raise_
        i = int(cursor) if cursor else 0
        nxt = str(i + 1) if i + 1 < len(self.pages) else None
        return _page(self.pages[i], next_cursor=nxt)


def _get(app, fmt=None):
    url = f"/dashboard/patients/p1/series/{CODE}" + (f"?format={fmt}" if fmt else "")
    with app.test_client() as c:
        return c.get(url)


def test_points_carry_their_OWN_identity(monkeypatch):
    app = _app()
    cdr = _SeriesCdr1([[_pt(1, 70.0, "2026-01-01T00:00:00Z"),
                        _pt(2, 71.0, "2026-02-01T00:00:00Z", tx="tx-2")]])
    _wire(monkeypatch, cdr)
    body = _get(app).get_json()
    pts = body["series"][0]["points"]
    assert [p["observationGuid"] for p in pts] == ["obs-00001", "obs-00002"]
    assert [p["transactionGuid"] for p in pts] == ["tx-1", "tx-2"]
    assert body["provenanceAvailable"] is True
    assert cdr.calls[0]["codes"] == [CODE]
    assert body["displayName"] == "Body weight"


def test_cdr1_down_is_a_NAMED_503_not_an_empty_series(monkeypatch):
    app = _app()
    _wire(monkeypatch, _SeriesCdr1([], raise_=Cdr1Unreachable("down", status_code=503)))
    res = _get(app)
    assert res.status_code == 503
    assert res.get_json()["error"] == "cdr1_unreachable"


def test_the_scan_bound_is_SAID(monkeypatch):
    app = _app()
    pages = [[_pt(i, 1.0 + i, f"2026-01-{1 + i % 28:02d}T00:00:00Z")]
             for i in range(dash._SERIES_SCAN_MAX_PAGES + 3)]
    cdr = _SeriesCdr1(pages)
    _wire(monkeypatch, cdr)
    body = _get(app).get_json()
    assert len(cdr.calls) == dash._SERIES_SCAN_MAX_PAGES
    assert body["complete"] is False


def test_a_dense_series_is_downsampled_and_SAYS_SO(monkeypatch):
    app = _app()
    n = dash._SERIES_TARGET + 500
    pts = [_pt(i, float(i % 37), f"2026-01-01T00:00:00Z") for i in range(n)]
    # distinct, increasing times
    for i, p in enumerate(pts):
        p["at"] = f"2026-01-{1 + i // 1440:02d}T{i // 60 % 24:02d}:{i % 60:02d}:00Z"
    _wire(monkeypatch, _SeriesCdr1([pts]))
    s = _get(app).get_json()["series"][0]
    assert s["downsampledFrom"] == n
    assert len(s["points"]) == dash._SERIES_TARGET
    guids = [p["observationGuid"] for p in s["points"]]
    assert guids[0] == "obs-00000" and guids[-1] == f"obs-{n - 1:05d}"
    assert len(set(guids)) == len(guids)


def test_a_sparse_series_is_NOT_downsampled(monkeypatch):
    app = _app()
    _wire(monkeypatch, _SeriesCdr1([[_pt(i, 1.0, f"2026-0{i + 1}-01T00:00:00Z") for i in range(5)]]))
    s = _get(app).get_json()["series"][0]
    assert s["downsampledFrom"] is None and len(s["points"]) == 5


def test_lttb_returns_identity_preserving_INDICES():
    """Two points with the same time and value must stay distinguishable."""
    xs = [float(i) for i in range(100)] + [99.0]
    ys = [float(i % 7) for i in range(100)] + [1.0]
    idx = dash.lttb_indices(xs, ys, 20)
    assert len(idx) == 20 and idx[0] == 0 and idx[-1] == 100
    assert idx == sorted(idx) and len(set(idx)) == 20


def test_lttb_keeps_the_extreme():
    xs = [float(i) for i in range(1000)]
    ys = [0.0] * 1000
    ys[503] = 500.0
    assert 503 in dash.lttb_indices(xs, ys, 50)


def test_non_numeric_points_are_COUNTED_not_silently_dropped(monkeypatch):
    app = _app()
    text = dict(_pt(9, None, "2026-03-01T00:00:00Z"), value_string="positiv")
    _wire(monkeypatch, _SeriesCdr1([[_pt(1, 5.0, "2026-01-01T00:00:00Z"), text]]))
    body = _get(app).get_json()
    assert body["nonNumericCount"] == 1


def test_csv_is_the_same_series_and_carries_its_caveats(monkeypatch):
    app = _app()
    pages = [[_pt(i, 1.0, f"2026-01-{1 + i:02d}T00:00:00Z")]
             for i in range(dash._SERIES_SCAN_MAX_PAGES + 1)]
    _wire(monkeypatch, _SeriesCdr1(pages))
    res = _get(app, "csv")
    assert res.status_code == 200 and res.mimetype == "text/csv"
    text = res.get_data(as_text=True)
    assert "# incomplete:" in text
    assert "code_canonical,unit,effective_at,value,observation_guid,transaction_guid" in text
    assert "obs-00000" in text
    assert "attachment" in res.headers["Content-Disposition"]
