"""GET /dashboard/patients/<guid>/events — the event list (#983 unblocked it).

Shape tests run under the repo's `AUTH_MODE: off` app, where the blob is a dev
SU admin: they prove what the endpoint RETURNS, not who may call it. The gate
is proven in test_dashboard_access.py with real non-admin blobs.
"""
from __future__ import annotations

import sqlalchemy

from app import create_app
from app.models import db
from app.services.cdr1_client import (
    Cdr1BadCursor, Cdr1ProjectionTooOld, Cdr1Unreachable)
from app.services.ips_client import Block
import app.routes.dashboard as dash


def _app():
    app = create_app({
        "TESTING": True,
        "DATABASE_URL": "sqlite:///:memory:",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {
            "connect_args": {"check_same_thread": False},
            "poolclass": sqlalchemy.pool.StaticPool,
        },
        "AUTH_MODE": "off",
    })
    with app.app_context():
        db.create_all()
    return app


CODE = "urn:pdhc:concept/11111111-1111-1111-1111-111111111111"
CONCEPT = "11111111-1111-1111-1111-111111111111"


def _pt(guid, at, care_plan=None):
    return {"code": CODE, "at": at, "value": 70.0, "unit": "kg",
            "value_string": None, "org_guid": "org-a",
            "observation_guid": guid, "transaction_guid": "txn",
            "ingest_raw_guid": f"ing-{guid}", "care_plan_guid": care_plan}


class _Cdr1:
    base_url = "https://cdr.example"

    def __init__(self, page=None, raise_=None, summary=None):
        self._page = page
        self._raise = raise_
        self._summary = summary if summary is not None else [
            {"code": CODE, "unit": "kg", "count": 300}]
        self.calls = []

    def list_org_patients(self, orgs, *, is_admin=False, reason=None):
        return [{"guid": "p1"}]

    def patient_events(self, guid, orgs, **kw):
        self.calls.append(kw)
        if self._raise:
            raise self._raise
        return self._page

    def patient_summary(self, guid, orgs, *, is_admin=False, reason=None):
        return self._summary


def _page(points, **kw):
    base = {"points": points, "next_cursor": None, "truncated": False,
            "limit": 100, "undated_count": 0}
    base.update(kw)
    return base


def _wire(monkeypatch, cdr1, names=None, name_calls=None):
    monkeypatch.setattr(dash.cdr1_client, "build_client", lambda: cdr1)
    monkeypatch.setattr(dash.ips_client, "is_configured", lambda: True)
    monkeypatch.setattr(dash.ips_client, "get_active_blocks", lambda *a, **k: [])
    import app.services.plan_client as pc

    def fake_names(guids, **kw):
        if name_calls is not None:
            name_calls.append(list(guids))
        return names if names is not None else {CONCEPT: "Body weight"}
    monkeypatch.setattr(pc, "concept_display_names", fake_names)


def _get(app, path="/dashboard/patients/p1/events"):
    with app.test_client() as c:
        r = c.get(path)
        return r.status_code, r.get_json()


# --------------------------------------------------------------------------

def test_a_page_of_rows_carries_IDENTITY_and_provenance(monkeypatch):
    app = _app()
    _wire(monkeypatch, _Cdr1(_page([_pt("o2", "2026-04-12T10:00:00+00:00"),
                                    _pt("o1", "2026-04-08T10:00:00+00:00")],
                                   next_cursor="CUR", truncated=True)))
    status, body = _get(app)
    assert status == 200
    assert [r["observationGuid"] for r in body["rows"]] == ["o2", "o1"]
    r0 = body["rows"][0]
    assert r0["ingestRawGuid"] == "ing-o2"
    assert r0["transactionGuid"] == "txn"
    assert r0["displayName"] == "Body weight"
    assert body["nextCursor"] == "CUR"          # cdr1's cursor, untouched
    assert body["truncated"] is True
    assert body["sparrChecked"] is True


def test_display_names_are_ONE_batched_call_not_one_per_row(monkeypatch):
    app = _app()
    calls = []
    pts = [_pt(f"o{i}", f"2026-04-{10+i:02d}T10:00:00+00:00") for i in range(8)]
    _wire(monkeypatch, _Cdr1(_page(pts)), name_calls=calls)
    _get(app)
    assert len(calls) == 1, f"{len(calls)} name lookups for one page"


def test_the_series_badge_counts_the_WHOLE_history_not_the_page(monkeypatch):
    """A page-local count would put "x2" on a concept with 300 points."""
    app = _app()
    _wire(monkeypatch, _Cdr1(_page([_pt("o1", "2026-04-12T10:00:00+00:00"),
                                    _pt("o2", "2026-04-11T10:00:00+00:00")])))
    _, body = _get(app)
    assert body["rows"][0]["seriesCount"] == 300
    assert body["rows"][0]["hasSeries"] is True


def test_an_empty_summary_beside_real_points_marks_the_badge_UNKNOWN(monkeypatch):
    app = _app()
    _wire(monkeypatch, _Cdr1(_page([_pt("o1", "2026-04-12T10:00:00+00:00")]),
                             summary=[]))
    _, body = _get(app)
    assert "cdr1:summary" in body["unavailable"]


def test_T_plus_is_NONE_without_a_care_plan_and_costs_NO_call(monkeypatch):
    """0 of 1 949 cdr1 rows carry care_plan_guid today."""
    app = _app()
    import app.services.request_client as rc
    monkeypatch.setattr(rc, "build_client",
                        lambda: (_ for _ in ()).throw(AssertionError("called")))
    _wire(monkeypatch, _Cdr1(_page([_pt("o1", "2026-04-12T10:00:00+00:00")])))
    _, body = _get(app)
    assert body["rows"][0]["tFromStartSeconds"] is None


def test_T_plus_is_computed_where_a_care_plan_IS_linked(monkeypatch):
    app = _app()
    import app.services.request_client as rc

    class _RC:
        def fetch_care_plan(self, guid):
            return {"period_start": "2026-04-10T10:00:00+00:00"}
    monkeypatch.setattr(rc, "build_client", lambda: _RC())
    _wire(monkeypatch, _Cdr1(_page([_pt("o1", "2026-04-12T10:00:00+00:00",
                                        care_plan="cp-1")])))
    _, body = _get(app)
    assert body["rows"][0]["tFromStartSeconds"] == 2 * 86400


def test_an_EMPTY_page_is_an_answer(monkeypatch):
    app = _app()
    _wire(monkeypatch, _Cdr1(_page([])))
    status, body = _get(app)
    assert status == 200 and body["rows"] == []


def test_a_PRE_983_cdr1_is_a_named_503_citing_the_ticket(monkeypatch):
    app = _app()
    _wire(monkeypatch, _Cdr1(raise_=Cdr1ProjectionTooOld("no observation_guid")))
    status, body = _get(app)
    assert status == 503
    assert body["error"] == "upstream_projection_too_old"
    assert body["ticket"] == "#983"


def test_a_bad_cursor_is_a_400_not_an_empty_page(monkeypatch):
    app = _app()
    _wire(monkeypatch, _Cdr1(raise_=Cdr1BadCursor("invalid_cursor")))
    status, body = _get(app, "/dashboard/patients/p1/events?cursor=x")
    assert status == 400 and body["error"] == "invalid_cursor"


def test_an_unreachable_cdr1_is_a_503_not_an_empty_page(monkeypatch):
    app = _app()
    _wire(monkeypatch, _Cdr1(raise_=Cdr1Unreachable("down", 503)))
    status, body = _get(app)
    assert status == 503 and body["error"] == "cdr1_unreachable"


def test_a_spaerr_lift_exposure_is_AUDITED(monkeypatch):
    app = _app()
    seen = {}
    _wire(monkeypatch, _Cdr1(_page([_pt("o1", "2026-04-12T10:00:00+00:00")])))
    monkeypatch.setattr(dash.ips_client, "get_active_blocks",
                        lambda *a, **k: [Block.from_dict({
                            "guid": "b1", "patient_guid": "p1",
                            "source_scope_type": "clinic",
                            "source_scope_id": "org-a", "is_active": True})])
    monkeypatch.setattr(dash.ips_client, "filter_blocked_points",
                        lambda pts, blocks: ([], [("pt", "lift")]))
    monkeypatch.setattr("app.routes.charts._lift_exposure_audit",
                        lambda e: seen.setdefault("audited", e) or "A")
    _get(app)
    assert seen.get("audited") == [("pt", "lift")]


def test_the_cursor_and_limit_reach_cdr1_and_limit_is_CAPPED(monkeypatch):
    app = _app()
    cdr1 = _Cdr1(_page([]))
    _wire(monkeypatch, cdr1)
    _get(app, "/dashboard/patients/p1/events?cursor=ABC&limit=99999")
    assert cdr1.calls[0]["cursor"] == "ABC"
    assert cdr1.calls[0]["limit"] == 500
