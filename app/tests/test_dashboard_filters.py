"""Filters (#977 part B): validated here, because cdr1 IGNORES a bound it
cannot parse — a typo would otherwise return the unfiltered history as if
filtered."""
from __future__ import annotations

import pytest
from flask import Flask

import app.routes.dashboard as dash
import app.services.cdr1_client as mod
from app.services.cdr1_client import (
    Cdr1Client, Cdr1FilterUnsupported, Cdr1Unreachable)
from app.tests.test_cdr1_events import _Resp, _full_point, _ok, _patch
from app.tests.test_dashboard_events import _app, _page, _wire


@pytest.fixture
def req():
    app = Flask(__name__)

    def make(qs):
        return app.test_request_context("/?" + qs)
    return make


# -- the bounds -------------------------------------------------------------

def test_a_date_is_a_STOCKHOLM_day(req):
    # 2026-11-01 is CET (UTC+1): local midnight is 23:00 the day before.
    assert dash._bound("2026-11-01", end=False) == "2026-10-31T23:00:00+00:00"
    # `to` ends at the next local midnight, minus 1 µs (cdr1 compares <=).
    assert dash._bound("2026-11-01", end=True) == "2026-11-01T22:59:59.999999+00:00"


def test_summer_time_is_respected():
    # 2026-07-01 is CEST (UTC+2).
    assert dash._bound("2026-07-01", end=False) == "2026-06-30T22:00:00+00:00"


@pytest.mark.parametrize("bad", ["2026-13-01", "yesterday", "2026-11-1",
                                 "2026-11-01T10:00:00"])
def test_a_bad_bound_is_REFUSED_not_ignored(bad):
    with pytest.raises(dash.InvalidFilter):
        dash._bound(bad, end=False)


def test_from_after_to_is_refused(req):
    with req("from=2026-11-05&to=2026-11-01"):
        with pytest.raises(dash.InvalidFilter):
            dash._filters()


def test_filters_parse(req):
    with req("code=a&code=b&transaction=t1&from=2026-11-01"):
        codes, frm, to, tx = dash._filters()
    assert codes == ["a", "b"] and tx == ["t1"]
    assert frm.startswith("2026-10-31T23:00") and to is None


# -- the client -------------------------------------------------------------

@pytest.fixture
def ctx():
    app = Flask(__name__)
    with app.test_request_context("/"):
        yield


def test_the_client_SENDS_every_filter(ctx, monkeypatch):
    seen = []
    _patch(monkeypatch, _Resp(200, _ok([_full_point("o1")], transaction_filter=True)), seen=seen)
    Cdr1Client(base_url="https://cdr.example", token="t").patient_events(
        "p1", ["org-a"], codes=["c"], frm="F", to="T", transactions=["t1", "t2"])
    params = seen[0]["params"]
    assert ("code", "c") in params and ("from", "F") in params and ("to", "T") in params
    assert ("transaction", "t1") in params and ("transaction", "t2") in params


def test_an_UNCONFIRMED_transaction_filter_is_refused(ctx, monkeypatch):
    """An older cdr1 ignores `transaction` and answers with everything."""
    _patch(monkeypatch, _Resp(200, _ok([_full_point("o1")])))
    with pytest.raises(Cdr1FilterUnsupported):
        Cdr1Client(base_url="https://cdr.example", token="t").patient_events(
            "p1", ["org-a"], transactions=["t1"])


def test_no_transaction_filter_needs_no_confirmation(ctx, monkeypatch):
    _patch(monkeypatch, _Resp(200, _ok([_full_point("o1")])))
    page = Cdr1Client(base_url="https://cdr.example", token="t").patient_events("p1", ["org-a"])
    assert len(page["points"]) == 1


# -- the routes -------------------------------------------------------------

class _Cdr1:
    base_url = "https://cdr.example"

    def __init__(self, raise_=None, concepts=None):
        self.raise_, self.calls = raise_, []
        self.concepts = concepts if concepts is not None else []

    def list_org_patients(self, orgs, *, is_admin=False, reason=None):
        return [{"guid": "p1"}]

    def patient_events(self, guid, orgs, **kw):
        self.calls.append(kw)
        if self.raise_:
            raise self.raise_
        return _page([])

    def patient_summary(self, guid, orgs, **kw):
        return []

    def patient_concepts(self, guid, orgs, **kw):
        if self.raise_:
            raise self.raise_
        return self.concepts


def _get(app, path):
    with app.test_client() as c:
        r = c.get(path)
        return r.status_code, r.get_json()


def test_events_pass_the_filters_through(monkeypatch):
    app = _app()
    cdr = _Cdr1()
    _wire(monkeypatch, cdr)
    status, _ = _get(app, "/dashboard/patients/p1/events?code=c1&transaction=t1"
                          "&from=2026-11-01&to=2026-11-02")
    assert status == 200
    kw = cdr.calls[0]
    assert kw["codes"] == ["c1"] and kw["transactions"] == ["t1"]
    assert kw["frm"] == "2026-10-31T23:00:00+00:00"
    assert kw["to"] == "2026-11-02T22:59:59.999999+00:00"


def test_events_REFUSE_a_bad_date_and_never_ask_cdr1(monkeypatch):
    app = _app()
    cdr = _Cdr1()
    _wire(monkeypatch, cdr)
    status, body = _get(app, "/dashboard/patients/p1/events?from=igår")
    assert status == 400 and body["error"] == "invalid_filter"
    assert cdr.calls == []


def test_an_unsupported_filter_is_a_NAMED_503(monkeypatch):
    app = _app()
    _wire(monkeypatch, _Cdr1(raise_=Cdr1FilterUnsupported("old cdr1")))
    status, body = _get(app, "/dashboard/patients/p1/events?transaction=t1")
    assert status == 503 and body["error"] == "upstream_filter_unsupported"


def test_the_series_window_reaches_cdr1(monkeypatch):
    app = _app()
    cdr = _Cdr1()
    _wire(monkeypatch, cdr)
    status, body = _get(app, "/dashboard/patients/p1/series/c1"
                             "?from=2026-11-01T00:00:00Z&to=2026-11-02T00:00:00Z")
    assert status == 200
    assert cdr.calls[0]["frm"] == "2026-11-01T00:00:00+00:00"
    assert body["window"]["from"] == "2026-11-01T00:00:00+00:00"


def test_concepts_carry_names_and_counts(monkeypatch):
    app = _app()
    from app.tests.test_dashboard_events import CODE
    _wire(monkeypatch, _Cdr1(concepts=[{"code": CODE, "unit": "kg", "count": 12,
                                        "first_observed_at": "a", "last_observed_at": "b"}]))
    status, body = _get(app, "/dashboard/patients/p1/concepts")
    assert status == 200
    assert body["concepts"] == [{"code": CODE, "displayName": "Body weight", "unit": "kg",
                                 "count": 12, "first": "a", "last": "b"}]


def test_concepts_cdr1_down_is_503_not_an_empty_list(monkeypatch):
    app = _app()
    _wire(monkeypatch, _Cdr1(raise_=Cdr1Unreachable("down", status_code=503)))
    status, body = _get(app, "/dashboard/patients/p1/concepts")
    assert status == 503
