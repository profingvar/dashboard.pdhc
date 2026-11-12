"""Limits wired through the routes (MDR class IIa, operator 2026-11-12).

The routes only CARRY verdicts from app/dashboard/limits.py; these tests pin
that they do, that unreadable orders are `unknown` (never "no limit"), that
"outside" filters server-side, and that downsampling never hides an outside
value.
"""
from __future__ import annotations

import app.routes.dashboard as dash
import app.services.request_client as rc
from app.tests.test_dashboard_events import CODE, _app, _page, _wire

TX = "tx-glu"


def _pt(i, value, at, tx=TX, unit="mmol/L"):
    return {"code": CODE, "at": at, "value": value, "unit": unit,
            "value_string": None, "org_guid": "org-a",
            "observation_guid": f"obs-{i:05d}", "transaction_guid": tx,
            "ingest_raw_guid": f"i-{i}", "care_plan_guid": None}


def _order(lo="3.0", hi="10.0", guid="sr-1"):
    return {"guid": guid, "status": "active", "patient_guid": "p1",
            "plan_definition_snapshot": {"title": "Diabetes", "version": "3", "activities": [
                {"guid": "act", "transactions": [
                    {"guid": TX, "range_min": lo, "range_max": hi, "concept_unit_name": "mmol/L"}]}]}}


class _Cdr1:
    base_url = "https://cdr.example"

    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def list_org_patients(self, orgs, *, is_admin=False, reason=None):
        return [{"guid": "p1"}]

    def patient_events(self, guid, orgs, *, cursor=None, limit=100, **kw):
        self.calls.append(dict(kw, cursor=cursor))
        i = int(cursor) if cursor else 0
        nxt = str(i + 1) if i + 1 < len(self.pages) else None
        return _page(self.pages[i], next_cursor=nxt)

    def patient_summary(self, guid, orgs, **kw):
        return [{"code": CODE, "count": 5}]


def _orders(monkeypatch, orders=None, raises=None):
    class _R:
        def fetch_service_requests(self, *, patient_guid=None, **kw):
            if raises:
                raise raises
            return orders or []
    monkeypatch.setattr(rc, "build_client", lambda: _R())


def _get(app, path):
    with app.test_client() as c:
        r = c.get(path)
        return r.status_code, r.get_json()


def test_every_row_carries_its_verdict(monkeypatch):
    app = _app()
    _wire(monkeypatch, _Cdr1([[_pt(1, 12.0, "2026-11-02T00:00:00Z"),
                               _pt(2, 6.0, "2026-11-01T00:00:00Z"),
                               _pt(3, 6.0, "2026-10-01T00:00:00Z", tx="tx-none")]]))
    _orders(monkeypatch, [_order()])
    _, body = _get(app, "/dashboard/patients/p1/events")
    st = [r["limit"]["status"] for r in body["rows"]]
    assert st == ["above", "inside", "no_limit"]
    assert body["rows"][0]["limit"]["orderGuid"] == "sr-1"


def test_unreadable_orders_make_every_row_UNKNOWN_not_no_limit(monkeypatch):
    app = _app()
    _wire(monkeypatch, _Cdr1([[_pt(1, 12.0, "2026-11-02T00:00:00Z")]]))
    _orders(monkeypatch, raises=RuntimeError("request down"))
    _, body = _get(app, "/dashboard/patients/p1/events")
    assert body["rows"][0]["limit"]["status"] == "unknown"
    assert any(u.startswith("limits:") for u in body["unavailable"])


def test_outside_filter_returns_ONLY_outside_values_and_asks_for_ranged_tx(monkeypatch):
    app = _app()
    cdr = _Cdr1([[_pt(1, 12.0, "2026-11-03T00:00:00Z"), _pt(2, 6.0, "2026-11-02T00:00:00Z")],
                 [_pt(3, 2.0, "2026-11-01T00:00:00Z")]])
    _wire(monkeypatch, cdr)
    _orders(monkeypatch, [_order()])
    status, body = _get(app, "/dashboard/patients/p1/events?outside=1")
    assert status == 200
    assert [r["observationGuid"] for r in body["rows"]] == ["obs-00001", "obs-00003"]
    assert {r["limit"]["status"] for r in body["rows"]} == {"above", "below"}
    assert all(c["transactions"] == [TX] for c in cdr.calls)
    assert len(cdr.calls) == 2 and body["outsideScanComplete"] is True


def test_outside_filter_with_NO_limits_says_so(monkeypatch):
    app = _app()
    cdr = _Cdr1([[_pt(1, 99.0, "2026-11-03T00:00:00Z")]])
    _wire(monkeypatch, cdr)
    _orders(monkeypatch, [])
    status, body = _get(app, "/dashboard/patients/p1/events?outside=1")
    assert status == 200 and body["rows"] == [] and body["noLimits"] is True
    assert cdr.calls == []


def test_outside_filter_with_unreadable_orders_is_503_not_empty(monkeypatch):
    app = _app()
    _wire(monkeypatch, _Cdr1([[]]))
    _orders(monkeypatch, raises=RuntimeError("down"))
    status, body = _get(app, "/dashboard/patients/p1/events?outside=1")
    assert status == 503 and body["error"] == "limits_unknown"


def test_series_points_carry_verdicts_and_one_limit_gives_a_band(monkeypatch):
    app = _app()
    _wire(monkeypatch, _Cdr1([[_pt(1, 12.0, "2026-11-02T00:00:00Z"),
                               _pt(2, 6.0, "2026-11-01T00:00:00Z")]]))
    _orders(monkeypatch, [_order()])
    _, body = _get(app, f"/dashboard/patients/p1/series/{CODE}")
    pts = body["series"][0]["points"]
    assert {p["observationGuid"]: p["limitStatus"] for p in pts} == {
        "obs-00001": "above", "obs-00002": "inside"}
    band = body["limitBands"][0]["band"]
    assert (band["low"], band["high"], band["unit"], band["orderGuid"]) == (3.0, 10.0, "mmol/L", "sr-1")


def test_conflicting_limits_give_NO_band(monkeypatch):
    app = _app()
    _wire(monkeypatch, _Cdr1([[_pt(1, 9.0, "2026-11-02T00:00:00Z")]]))
    _orders(monkeypatch, [_order(hi="10.0", guid="sr-1"), _order(hi="8.0", guid="sr-2")])
    _, body = _get(app, f"/dashboard/patients/p1/series/{CODE}")
    assert body["series"][0]["points"][0]["limitStatus"] == "conflict"
    assert body["limitBands"][0]["band"] is None


def test_downsampling_NEVER_hides_an_outside_value(monkeypatch):
    """LTTB keeps shape, not clinical meaning. Force the worst case — an LTTB
    that keeps only the endpoints — so ONLY the explicit keep-outside rule can
    save the value. (A first version of this test used a spike, which LTTB
    kept on its own; the rule could be deleted and it still passed.)"""
    app = _app()
    n = dash._SERIES_TARGET + 10
    pts = [_pt(i, 6.0, f"2026-01-01T00:00:00Z") for i in range(n)]
    for i, p in enumerate(pts):
        p["at"] = f"2026-0{1 + i // 1440 // 28}-{1 + (i // 1440) % 28:02d}T{(i // 60) % 24:02d}:{i % 60:02d}:00Z"
    pts[777]["value"] = 10.5
    _wire(monkeypatch, _Cdr1([pts]))
    _orders(monkeypatch, [_order()])
    monkeypatch.setattr(dash, "lttb_indices", lambda xs, ys, target: [0, len(xs) - 1])
    _, body = _get(app, f"/dashboard/patients/p1/series/{CODE}")
    s = body["series"][0]
    assert s["downsampledFrom"] == n
    kept = {p["observationGuid"]: p["limitStatus"] for p in s["points"]}
    assert kept == {"obs-00000": "inside", "obs-00777": "above", f"obs-{n - 1:05d}": "inside"}


def test_two_DIFFERENT_judged_limits_in_one_series_give_NO_band(monkeypatch):
    """Both points are judged (no conflict per point), but under different
    limits: one shaded band would misstate one of them."""
    app = _app()
    order = _order()
    order["plan_definition_snapshot"]["activities"][0]["transactions"].append(
        {"guid": "tx-other", "range_min": "4.0", "range_max": "8.0", "concept_unit_name": "mmol/L"})
    _wire(monkeypatch, _Cdr1([[_pt(1, 6.0, "2026-11-02T00:00:00Z"),
                               _pt(2, 6.0, "2026-11-01T00:00:00Z", tx="tx-other")]]))
    _orders(monkeypatch, [order])
    _, body = _get(app, f"/dashboard/patients/p1/series/{CODE}")
    assert {p["limitStatus"] for p in body["series"][0]["points"]} == {"inside"}
    assert body["limitBands"][0] == {"band": None, "reason": "more than one limit applies"}


def test_detail_carries_the_verdict(monkeypatch):
    app = _app()

    class _D(_Cdr1):
        def observation_detail(self, guid, orgs, **kw):
            return {"patient_guid": "p1", "observation_guid": guid, "code": CODE,
                    "at": "2026-11-02T00:00:00Z", "org_guid": "org-a", "value": 12.0,
                    "unit": "mmol/L", "ingest_raw_guid": "i", "received_at": None,
                    "context_found": True, "context": {"transaction_guid": TX}}
    _wire(monkeypatch, _D([[]]))
    _orders(monkeypatch, [_order()])
    status, body = _get(app, "/dashboard/events/obs-1")
    assert status == 200
    assert body["limit"]["status"] == "above" and body["limit"]["high"] == 10.0
