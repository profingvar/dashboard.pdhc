"""GET /dashboard/patients/<guid>/plan — the plan panel's data (#971).

Production, 2026-11-12: 0 CarePlans, 28 ServiceRequests, and cdr1
observations whose transaction_guid matches those orders' frozen
PlanDefinitions 1 949 / 1 949 — but for three of the four patients with data,
NO order exists. These tests pin the two disagreements the panel must SHOW.
"""
from __future__ import annotations

import pytest

import app.routes.dashboard as dash
import app.services.request_client as rc
from app.dashboard.dto import PlanItem, PlanSummary
from app.services.cdr1_client import Cdr1Unreachable
from app.services.ips_client import Block
from app.tests.test_dashboard_events import CODE, CONCEPT, _app, _page, _wire

TX_ORDERED = "tx-ordered"
TX_DONE_EMPTY = "tx-done-no-obs"
TX_STRAY = "tx-stray"


def _pt(guid, at, tx):
    return {"code": CODE, "at": at, "value": 1.0, "unit": "kg",
            "value_string": None, "org_guid": "org-a", "observation_guid": guid,
            "transaction_guid": tx, "ingest_raw_guid": f"i-{guid}",
            "care_plan_guid": None}


class _PagedCdr1:
    """Serves `pages` in order, chaining cursors, and records each call."""
    base_url = "https://cdr.example"

    def __init__(self, pages, raise_=None):
        self.pages, self.raise_, self.calls = pages, raise_, []

    def list_org_patients(self, orgs, *, is_admin=False, reason=None):
        return [{"guid": "p1"}]

    def patient_events(self, guid, orgs, *, cursor=None, limit=100, **kw):
        self.calls.append({"cursor": cursor, "limit": limit})
        if self.raise_:
            raise self.raise_
        i = int(cursor) if cursor else 0
        nxt = str(i + 1) if i + 1 < len(self.pages) else None
        return _page(self.pages[i], next_cursor=nxt)


def _plans(*items_states):
    items = tuple(PlanItem(transaction_guid=tx, title=tx, state=st,
                           service_request_guid="sr-1")
                  for tx, st in items_states)
    return (PlanSummary(care_plan_guid=None, items=items,
                        source="service_request", service_request_guid="sr-1"),)


def _wire_plan(monkeypatch, cdr1, plans=(), request_raises=None):
    _wire(monkeypatch, cdr1)

    class _R:
        def plan_summaries(self, guid):
            if request_raises:
                raise request_raises
            return plans
    monkeypatch.setattr(rc, "build_client", lambda: _R())


def _get(app):
    with app.test_client() as c:
        res = c.get("/dashboard/patients/p1/plan")
        return res.status_code, res.get_json()


def test_observations_are_counted_per_transaction(monkeypatch):
    app = _app()
    cdr = _PagedCdr1([[_pt("o1", "2026-01-02T00:00:00Z", TX_ORDERED),
                       _pt("o2", "2026-01-05T00:00:00Z", TX_ORDERED)]])
    _wire_plan(monkeypatch, cdr, _plans((TX_ORDERED, "planned")))
    status, body = _get(app)
    assert status == 200
    agg = body["observationsByTransaction"][TX_ORDERED]
    assert agg == {"count": 2, "first": "2026-01-02T00:00:00Z",
                   "last": "2026-01-05T00:00:00Z"}
    assert body["unordered"] == []
    assert body["observationCountsComplete"] is True


def test_observations_with_NO_ORDER_are_listed_not_dropped(monkeypatch):
    """The commonest production case on 2026-11-12."""
    app = _app()
    cdr = _PagedCdr1([[_pt("o1", "2026-01-02T00:00:00Z", TX_STRAY)]])
    _wire_plan(monkeypatch, cdr, plans=())
    _, body = _get(app)
    assert body["plans"] == []
    assert [u["transactionGuid"] for u in body["unordered"]] == [TX_STRAY]
    assert body["unordered"][0]["count"] == 1
    assert body["unordered"][0]["display"] == "Body weight"


def test_a_DONE_item_with_no_observation_is_visible_as_such(monkeypatch):
    app = _app()
    cdr = _PagedCdr1([[]])
    _wire_plan(monkeypatch, cdr, _plans((TX_DONE_EMPTY, "done")))
    _, body = _get(app)
    item = body["plans"][0]["items"][0]
    assert item["state"] == "done"
    assert TX_DONE_EMPTY not in body["observationsByTransaction"]


def test_state_is_the_orders_status_NOT_the_observations(monkeypatch):
    """Observations exist for the item; its state is still what the order says."""
    app = _app()
    cdr = _PagedCdr1([[_pt("o1", "2026-01-02T00:00:00Z", TX_ORDERED)]])
    _wire_plan(monkeypatch, cdr, _plans((TX_ORDERED, "planned")))
    _, body = _get(app)
    assert body["plans"][0]["items"][0]["state"] == "planned"


def test_pages_are_followed_and_the_bound_is_SAID(monkeypatch):
    app = _app()
    pages = [[_pt(f"o{i}", "2026-01-01T00:00:00Z", TX_ORDERED)]
             for i in range(dash._PLAN_SCAN_MAX_PAGES + 2)]
    cdr = _PagedCdr1(pages)
    _wire_plan(monkeypatch, cdr, _plans((TX_ORDERED, "planned")))
    _, body = _get(app)
    assert len(cdr.calls) == dash._PLAN_SCAN_MAX_PAGES
    assert body["observationCountsComplete"] is False
    assert body["observationsByTransaction"][TX_ORDERED]["count"] == dash._PLAN_SCAN_MAX_PAGES


def test_a_short_history_reads_complete_in_one_call(monkeypatch):
    app = _app()
    cdr = _PagedCdr1([[_pt("o1", "2026-01-01T00:00:00Z", TX_ORDERED)]])
    _wire_plan(monkeypatch, cdr, _plans((TX_ORDERED, "planned")))
    _, body = _get(app)
    assert len(cdr.calls) == 1 and cdr.calls[0]["cursor"] is None
    assert body["observationCountsComplete"] is True


def test_cdr1_down_is_UNKNOWN_not_zero(monkeypatch):
    app = _app()
    cdr = _PagedCdr1([], raise_=Cdr1Unreachable("down", status_code=503))
    _wire_plan(monkeypatch, cdr, _plans((TX_DONE_EMPTY, "done")))
    status, body = _get(app)
    assert status == 200
    assert body["observationsKnown"] is False
    assert body["plansKnown"] is True
    assert any(u.startswith("cdr1:") for u in body["unavailable"])


def test_request_down_is_UNKNOWN_and_observations_still_stand(monkeypatch):
    app = _app()
    cdr = _PagedCdr1([[_pt("o1", "2026-01-02T00:00:00Z", TX_STRAY)]])
    _wire_plan(monkeypatch, cdr, request_raises=RuntimeError("request down"))
    status, body = _get(app)
    assert status == 200
    assert body["plansKnown"] is False
    assert body["observationsKnown"] is True
    assert any(u.startswith("request:") for u in body["unavailable"])


def test_sparr_filters_BEFORE_counting(monkeypatch):
    """A count of blocked observations would disclose that they exist."""
    app = _app()
    cdr = _PagedCdr1([[_pt("o1", "2026-01-02T00:00:00Z", TX_STRAY),
                       _pt("o2", "2026-01-03T00:00:00Z", TX_STRAY)]])
    _wire_plan(monkeypatch, cdr)
    monkeypatch.setattr(dash.ips_client, "get_active_blocks", lambda *a, **k: ["b"])

    def drop_first(points, blocks):
        return points[1:], []
    monkeypatch.setattr(dash.ips_client, "filter_blocked_points", drop_first)
    monkeypatch.setattr(dash.ips_client, "has_any_active_block", lambda b: True)
    _, body = _get(app)
    assert body["observationsByTransaction"][TX_STRAY]["count"] == 1
    assert body["hasBlockedSources"] is True


def test_the_clock_is_named(monkeypatch):
    app = _app()
    _wire_plan(monkeypatch, _PagedCdr1([[]]))
    _, body = _get(app)
    assert body["displayTimeZone"] == "Europe/Stockholm"
    assert body["now"].endswith("+00:00")
