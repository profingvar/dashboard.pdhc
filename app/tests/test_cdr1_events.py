"""cdr1_client.patient_events — the STRICT events read (#983, dashboard side).

The older methods in cdr1_client return [] on any failure and charts.py
depends on that; they are left alone and one test below pins it. This read
raises instead, because "no events" and "cdr1 did not answer" are different
statements about a patient.

Mocked at the HTTP boundary (requests.get), never by patching the client.
"""
from __future__ import annotations

import pytest
import requests
from flask import Flask

import app.services.cdr1_client as mod
from app.services.cdr1_client import (
    Cdr1BadCursor, Cdr1Client, Cdr1NotConfigured,
    Cdr1ProjectionTooOld, Cdr1Unreachable,
)


@pytest.fixture
def ctx():
    app = Flask(__name__)
    with app.test_request_context("/"):
        yield


class _Resp:
    def __init__(self, status=200, body=None, raw=None):
        self.status_code = status
        self._body = body
        self._raw = raw

    def json(self):
        if self._raw is not None:
            raise ValueError("not json")
        return self._body


def _full_point(guid, at="2026-04-12T10:00:00+00:00"):
    return {"code": "c", "at": at, "value": 1.0, "unit": "kg",
            "value_string": None, "org_guid": "org-a",
            "observation_guid": guid, "transaction_guid": "t",
            "ingest_raw_guid": "i", "care_plan_guid": None}


def _ok(points, **extra):
    body = {"points": points, "next_cursor": None, "truncated": False,
            "limit": 100, "undated_count": 0}
    body.update(extra)
    return body


def _client():
    return Cdr1Client(base_url="https://cdr.example", token="tok")


def _patch(monkeypatch, resp=None, exc=None, seen=None):
    def fake_get(url, headers=None, params=None, timeout=None):
        if seen is not None:
            seen.append({"url": url, "params": params, "headers": headers})
        if exc:
            raise exc
        return resp
    monkeypatch.setattr(mod.requests, "get", fake_get)


# --------------------------------------------------------------------------

def test_unconfigured_refuses_BY_NAME(ctx):
    c = Cdr1Client(base_url="", token="t")
    with pytest.raises(Cdr1NotConfigured) as e:
        c.patient_events("p1", ["org-a"])
    assert "CDR1_BASE_URL" in str(e.value)
    assert "UNKNOWN" in str(e.value)


def test_a_full_page_is_returned_and_asks_for_NEWEST_FIRST(ctx, monkeypatch):
    seen = []
    _patch(monkeypatch, _Resp(200, _ok([_full_point("o1")],
                                       next_cursor="CUR", truncated=True)),
           seen=seen)
    page = _client().patient_events("p1", ["org-a"], cursor="PREV", limit=50)
    assert [p["observation_guid"] for p in page["points"]] == ["o1"]
    assert page["next_cursor"] == "CUR"          # passed through untouched
    assert page["truncated"] is True
    params = dict(seen[0]["params"])
    assert params["order"] == "desc"
    assert params["cursor"] == "PREV"
    assert params["limit"] == "50"
    assert seen[0]["url"].endswith("/api/v1/clinical/patient/p1/series")


def test_an_EMPTY_page_is_an_answer_not_an_error(ctx, monkeypatch):
    _patch(monkeypatch, _Resp(200, _ok([])))
    page = _client().patient_events("p1", ["org-a"])
    assert page["points"] == []
    assert page["truncated"] is False


def test_a_PRE_983_cdr1_is_refused_by_name_not_built_into_empty_rows(ctx, monkeypatch):
    """Production cdr1 runs the pre-#983 projection until it is deployed:
    points with no observation_guid. Rows built from that render and cannot
    be selected or looked up."""
    old_point = {"code": "c", "at": "2026-04-12T10:00:00+00:00", "value": 1.0,
                 "unit": "kg", "value_string": None, "org_guid": "org-a"}
    _patch(monkeypatch, _Resp(200, {"patient_guid": "p1", "points": [old_point],
                                    "count": 1}))
    with pytest.raises(Cdr1ProjectionTooOld) as e:
        _client().patient_events("p1", ["org-a"])
    assert "#983" in str(e.value)
    assert "observation_guid" in str(e.value)


def test_a_body_without_paging_fields_is_also_too_old(ctx, monkeypatch):
    _patch(monkeypatch, _Resp(200, {"points": [_full_point("o1")]}))
    with pytest.raises(Cdr1ProjectionTooOld):
        _client().patient_events("p1", ["org-a"])


def test_an_invalid_cursor_is_a_CLIENT_error_not_an_empty_page(ctx, monkeypatch):
    """Turning cdr1's 400 into "no more rows" would end a walk early while
    claiming it was complete."""
    _patch(monkeypatch, _Resp(400, {"error": "invalid_cursor", "message": "x"}))
    with pytest.raises(Cdr1BadCursor):
        _client().patient_events("p1", ["org-a"], cursor="garbage")


@pytest.mark.parametrize("status", [401, 403, 500, 503])
def test_other_failures_are_UNREACHABLE_with_the_status(ctx, monkeypatch, status):
    _patch(monkeypatch, _Resp(status, {"error": "x"}))
    with pytest.raises(Cdr1Unreachable) as e:
        _client().patient_events("p1", ["org-a"])
    assert e.value.status_code == status


def test_a_network_error_is_UNREACHABLE(ctx, monkeypatch):
    _patch(monkeypatch, exc=requests.ConnectionError("down"))
    with pytest.raises(Cdr1Unreachable):
        _client().patient_events("p1", ["org-a"])


def test_a_non_JSON_body_is_UNREACHABLE(ctx, monkeypatch):
    _patch(monkeypatch, _Resp(200, raw="<html>"))
    with pytest.raises(Cdr1Unreachable):
        _client().patient_events("p1", ["org-a"])


def test_the_named_errors_are_DISTINCT_types():
    for a, b in [(Cdr1NotConfigured, Cdr1Unreachable),
                 (Cdr1BadCursor, Cdr1Unreachable),
                 (Cdr1ProjectionTooOld, Cdr1Unreachable)]:
        assert not issubclass(a, b) and not issubclass(b, a), (a, b)


def test_the_OLD_series_read_is_UNCHANGED_and_still_returns_empty_on_error(ctx, monkeypatch):
    """Control. charts.py depends on patient_series returning [] on failure;
    the strict read must not have altered it."""
    _patch(monkeypatch, _Resp(500, {"error": "x"}))
    assert _client().patient_series("p1", None, None, None, ["org-a"]) == []
