"""Resilience of the dashboard BFF (#974, Phase 6 step 24).

"One slow or failing source shows a per-source warning and the rest still
renders" — tested by MAKING sources fail through the fixture layer, never by
reasoning about the code. A "down" source raises the SAME exception the real
client raises on a network error (`fixtures.py` puts the fake at the
transport), and a "slow" source is the REAL client transport running into
`requests`' timeout exception, so both paths are the production code.

For every source x endpoint the expected outcome is a table below; each test
asserts the status, the NAMED `unavailable` entry, and that the OTHER panels'
content is still there (compared against the healthy answer, not merely
"non-empty").

THREE BEHAVIOURS ARE PINNED DELIBERATELY, each with its reason in the test's
docstring, so that changing one is a decision someone makes on purpose:

* cdr1 down -> events / series / concepts / detail answer a NAMED 503;
* icd11 is read by no BFF route since #984, so its failure changes nothing;
* ips's spärr BLOCK read fails OPEN.

`test_dashboard_fixtures_974.py` holds the first smoke version of these; this
file is the full matrix.
"""
from __future__ import annotations

import csv
import io

import pytest
import requests

from app.dashboard import fixtures as fx
from app.services import (cdr1_client, icd11_client, ips_client, plan_client,
                          request_client)

T2D, HF, CGM = fx.PATIENTS
ENDPOINTS = ("card", "plan", "concepts", "series", "csv", "events", "detail")


def _get(c, url):
    r = c.get(url)
    return r.status_code, r.get_json(silent=True), r


def _hba1c_with_plan(app):
    """A T2D HbA1c row that carries transaction, PlanDefinition AND care plan,
    so its detail asks request (limits) and plan (PD name)."""
    return next(o["observation_guid"] for o in fx.state_of(app).dataset.patient_observations(T2D)
                if o["code"] == fx.code_of("hba1c") and o["care_plan_guid"]
                and o["plan_definition_guid"])


def _urls(app, patient=T2D):
    base = f"/dashboard/patients/{patient}"
    hba1c = fx.code_of("hba1c")
    return {"card": base, "plan": f"{base}/plan", "concepts": f"{base}/concepts",
            "series": f"{base}/series/{hba1c}", "csv": f"{base}/series/{hba1c}?format=csv",
            "events": f"{base}/events?limit=500",
            "detail": f"/dashboard/events/{_hba1c_with_plan(app)}"}


#: (status, {expected unavailable entry prefixes}) or (503, error code).
#: Prefixes are matched one-to-one: NO other entry may appear.
OK = (200, set())
EXPECT = {
    # #974 fix: the card now NAMES the missing identity source.
    "cdr1": {"card": (200, {"cdr1:patient-index"}), "plan": (200, {"cdr1:Cdr1Unreachable"}),
             "concepts": (503, "cdr1_unreachable"), "series": (503, "cdr1_unreachable"),
             "csv": (503, "cdr1_unreachable"), "events": (503, "cdr1_unreachable"),
             "detail": (503, "cdr1_unreachable")},
    # #974 fix: spärr cannot be verified -> every patient endpoint FAILS
    # CLOSED by name (access.sparr_state's design; the client used to swallow
    # the error and serve blocked rows).
    "ips": {e: (503, "undetermined") for e in ENDPOINTS},
    "request": {"card": OK, "plan": (200, {"request:RequestUnreachable"}), "concepts": OK,
                "series": (200, {"limits:RequestUnreachable"}), "csv": OK,
                "events": (200, {"limits:RequestUnreachable", "request:RequestUnreachable"}),
                # #974 fix: the detail now computes T+ (from request) like the list.
                "detail": (200, {"limits:RequestUnreachable", "request:RequestUnreachable"})},
    "plan": {"card": OK, "plan": (200, {"plan:PlanUnreachable"}),
             "concepts": (200, {"plan:PlanUnreachable"}),
             "series": (200, {"plan:PlanUnreachable"}), "csv": OK,
             "events": (200, {"plan:PlanUnreachable"}),
             "detail": (200, {"plan:PlanUnreachable"})},
    "icd11": {e: OK for e in ENDPOINTS},
}


def _assert_outcome(endpoint, expected, st, body, resp):
    status, what = expected
    assert st == status, (endpoint, st, body)
    if status == 503:
        assert body["error"] == what and body.get("message"), (endpoint, body)
        return
    if endpoint == "csv":
        # A CSV has no `unavailable` channel; it is checked for content below.
        assert resp.mimetype == "text/csv"
        return
    got = list(body["unavailable"])
    assert len(got) == len(what), (endpoint, got, what)
    for prefix in what:
        assert sum(1 for u in got if u.startswith(prefix)) == 1, (endpoint, got, prefix)


@pytest.mark.parametrize("source", fx.SOURCES)
def test_each_failing_source_is_named_on_each_endpoint(fixtures_app, fixtures_client, source):
    urls = _urls(fixtures_app)
    # CONTROL: all healthy first — nothing named, everything 200.
    for ep, url in urls.items():
        _assert_outcome(ep, OK, *_get(fixtures_client, url))
    with fx.failing(fixtures_app, source):
        for ep, url in urls.items():
            _assert_outcome(ep, EXPECT[source][ep], *_get(fixtures_client, url))
    # ...and it recovers when the source comes back (no cache keeps it "down").
    for ep, url in urls.items():
        _assert_outcome(ep, OK, *_get(fixtures_client, url))


# ---------------------------------------------------------------------------
# the rest still renders — compared with the HEALTHY answer
# ---------------------------------------------------------------------------

def _healthy(app, c, patient=T2D):
    out = {}
    for ep, url in _urls(app, patient).items():
        st, body, resp = _get(c, url)
        assert st == 200
        out[ep] = resp.get_data(as_text=True) if ep == "csv" else body
    return out


def _down(app, c, *sources, patient=T2D):
    out = {}
    with fx.failing(app, *sources):
        for ep, url in _urls(app, patient).items():
            st, body, resp = _get(c, url)
            out[ep] = (st, resp.get_data(as_text=True) if ep == "csv" else body)
    return out


def _row_core(r):
    return (r["observationGuid"], r["value"], r["unit"], r["effectiveAt"])


def test_plan_down_leaves_card_events_and_series_intact(fixtures_app, fixtures_client):
    h = _healthy(fixtures_app, fixtures_client)
    d = _down(fixtures_app, fixtures_client, "plan")
    # The card does not read plan: identical.
    assert d["card"] == (200, h["card"])
    # The event list: every row, every value and every LIMIT verdict intact;
    # only the display name falls back to the code it was resolved from.
    st, ev = d["events"]
    assert [_row_core(r) for r in ev["rows"]] == [_row_core(r) for r in h["events"]["rows"]]
    assert [r["limit"] for r in ev["rows"]] == [r["limit"] for r in h["events"]["rows"]]
    assert all(r["displayName"] == r["codeCanonical"] for r in ev["rows"])
    assert any(r["displayName"] != r["codeCanonical"] for r in h["events"]["rows"])  # control
    # The plan panel: the plans themselves (request) are all there.
    assert d["plan"][1]["plans"] == h["plan"]["plans"]
    assert d["plan"][1]["observationsByTransaction"] == h["plan"]["observationsByTransaction"]
    # Series: same points and verdicts; the name falls back to the code.
    assert d["series"][1]["series"][0]["points"] == h["series"]["series"][0]["points"]
    assert d["series"][1]["displayName"] == fx.code_of("hba1c") != h["series"]["displayName"]
    # Detail: PD name unknown, the limit (request) still judged.
    assert d["detail"][1]["detail"]["planDefinitionName"] is None
    assert h["detail"]["detail"]["planDefinitionName"]
    assert d["detail"][1]["limit"] == h["detail"]["limit"]
    assert d["csv"] == (200, h["csv"])


def test_request_down_makes_limits_unknown_never_no_limit(fixtures_app, fixtures_client):
    """MDR IIa: a care plan that could not be read must never read as "no
    limit" or "inside" — every verdict is `unknown` and says why."""
    h = _healthy(fixtures_app, fixtures_client)
    d = _down(fixtures_app, fixtures_client, "request")
    assert d["card"] == (200, h["card"])
    ev = d["events"][1]
    assert [_row_core(r) for r in ev["rows"]] == [_row_core(r) for r in h["events"]["rows"]]
    assert {r["limit"]["status"] for r in ev["rows"]} == {"unknown"}
    assert {r["limit"]["status"] for r in h["events"]["rows"]} >= {"above", "inside", "no_limit"}
    # T+ cannot be computed without the CarePlan's start: absent, not zero.
    assert all(r["tFromStartSeconds"] is None for r in ev["rows"])
    assert any(r["tFromStartSeconds"] for r in h["events"]["rows"])           # control
    # Plan panel: plans unknown (NOT "no plans"), observation counts intact.
    plan = d["plan"][1]
    assert plan["plans"] == [] and plan["plansKnown"] is False
    assert plan["observationsByTransaction"] == h["plan"]["observationsByTransaction"]
    # Series: points intact, verdicts unknown, no band drawn.
    s = d["series"][1]
    assert [p["value"] for p in s["series"][0]["points"]] == \
           [p["value"] for p in h["series"]["series"][0]["points"]]
    assert {p["limitStatus"] for p in s["series"][0]["points"]} == {"unknown"}
    assert s["limitBands"][0]["band"] is None
    # CSV: the per-row verdict column carries the caveat.
    rows = [r for r in csv.reader(io.StringIO(d["csv"][1])) if r and not r[0].startswith("#")][1:]
    assert {r[6] for r in rows} == {"unknown"} and len(rows) == 8
    # Detail: provenance intact, limit unknown.
    # T+ needs the care plan's start (request): unknown now, not stale.
    dd, hd = dict(d["detail"][1]["detail"]), dict(h["detail"]["detail"])
    assert hd["row"]["tFromStartSeconds"] is not None and dd["row"]["tFromStartSeconds"] is None
    dd["row"] = {k: v for k, v in dd["row"].items() if k != "tFromStartSeconds"}
    hd["row"] = {k: v for k, v in hd["row"].items() if k != "tFromStartSeconds"}
    assert dd == hd
    assert d["detail"][1]["limit"]["status"] == "unknown"


def test_request_down_does_not_claim_every_transaction_is_unordered(fixtures_app, fixtures_client):
    healthy = _get(fixtures_client, f"/dashboard/patients/{T2D}/plan")[1]
    assert [u["transactionGuid"] for u in healthy["unordered"]] == [fx.TX["waist"]]   # control
    with fx.failing(fixtures_app, "request"):
        plan = _get(fixtures_client, f"/dashboard/patients/{T2D}/plan")[1]
    assert plan["plansKnown"] is False
    assert {u["transactionGuid"] for u in plan["unordered"]} <= {fx.TX["waist"]}


def test_cdr1_down_answers_by_name_and_the_card_and_plans_still_render(fixtures_app, fixtures_client):
    """PINNED: cdr1 down -> events, series (and CSV), concepts and detail are a
    NAMED 503 `cdr1_unreachable`, by design. Those panels ARE cdr1 data and
    have no partial form: a 200 with no rows would tell the clinician "this
    patient has no events", which is a different and false statement
    (`patient_events` docstring, plan step 24). The card (ips) and the plan
    list (request) do not depend on cdr1 and must still render."""
    h = _healthy(fixtures_app, fixtures_client)
    d = _down(fixtures_app, fixtures_client, "cdr1")
    for ep in ("events", "series", "csv", "concepts", "detail"):
        st, body = d[ep]
        assert st == 503, ep
        if ep != "csv":
            assert body["error"] == "cdr1_unreachable", ep
    card = d["card"][1]
    assert card["patientCard"]["diagnoses"] == h["card"]["patientCard"]["diagnoses"]
    assert card["patientCard"]["epsSections"] == h["card"]["patientCard"]["epsSections"]
    plan = d["plan"][1]
    assert plan["plans"] == h["plan"]["plans"] and plan["plansKnown"] is True
    # Counts are UNKNOWN, not zero.
    assert plan["observationsKnown"] is False and plan["observationsByTransaction"] == {}
    assert h["plan"]["observationsKnown"] is True                            # control


def test_cdr1_down_card_names_the_missing_identity(fixtures_app, fixtures_client):
    with fx.failing(fixtures_app, "cdr1"):
        st, card, _ = _get(fixtures_client, f"/dashboard/patients/{T2D}")
    assert st == 200 and card["patientCard"]["name"] is None
    assert any(u.startswith("cdr1") for u in card["unavailable"])


def test_ips_down_FAILS_CLOSED_and_blocked_rows_are_never_served(fixtures_app, fixtures_client):
    """#974: with ips down the HF patient's blocked NT-proBNP results used to
    be LISTED with the banner off and `sparrChecked` True (the client
    swallowed the network error). Now every patient read is a named 503
    `undetermined`: "nobody knows yet", never "nothing is hidden"."""
    blocked = {o["observation_guid"] for o in fx.state_of(fixtures_app).dataset.patient_observations(HF)
               if o["org_guid"] == fx.ORG_BLOCKED}
    url = f"/dashboard/patients/{HF}/events?limit=500"
    _, up, _ = _get(fixtures_client, url)
    assert up["hasBlockedSources"] is True                                   # control
    assert blocked and not ({r["observationGuid"] for r in up["rows"]} & blocked)
    with fx.failing(fixtures_app, "ips"):
        st, down, _ = _get(fixtures_client, url)
        st_d, det, _ = _get(fixtures_client, f"/dashboard/events/{sorted(blocked)[0]}")
    assert (st, down["error"]) == (503, "undetermined")
    assert (st_d, det["error"]) == (503, "undetermined")

def test_icd11_is_read_by_no_bff_route(fixtures_app, fixtures_client):
    """PINNED: since #984 the card's diagnoses come from the EPS (derived from
    the CDR icd11 writes to), and no BFF route asks icd11. So icd11 down must
    change NOTHING, byte for byte. If a route ever starts reading icd11, this
    test fails and its row in EXPECT must be written."""
    h = _healthy(fixtures_app, fixtures_client)
    with fx.failing(fixtures_app, "icd11"):
        with fixtures_app.test_request_context():
            with pytest.raises(icd11_client.Icd11Unreachable):          # control: knob works
                fx.fake_client("icd11").problem_list(T2D)
    d = _down(fixtures_app, fixtures_client, "icd11")
    for ep in ENDPOINTS:
        st, body = d[ep]
        assert st == 200, ep
        if isinstance(body, dict):
            body, ref = dict(body), dict(h[ep])
            body.pop("now", None), ref.pop("now", None)
            assert body == ref, ep
        else:
            assert body == h[ep], ep


# ---------------------------------------------------------------------------
# two at once
# ---------------------------------------------------------------------------

def test_plan_and_request_down_together(fixtures_app, fixtures_client):
    h = _healthy(fixtures_app, fixtures_client)
    d = _down(fixtures_app, fixtures_client, "plan", "request")
    assert d["card"] == (200, h["card"])
    st, ev = d["events"]
    assert st == 200
    assert sorted(u.split(":")[0] for u in ev["unavailable"]) == ["limits", "plan", "request"]
    assert [_row_core(r) for r in ev["rows"]] == [_row_core(r) for r in h["events"]["rows"]]
    assert {r["limit"]["status"] for r in ev["rows"]} == {"unknown"}
    assert all(r["displayName"] == r["codeCanonical"] for r in ev["rows"])
    plan = d["plan"][1]
    # Only request: with the orders unknown nothing is listed as unordered,
    # so plan is not asked for names (#974 fix; it used to be ["plan", "request"]).
    assert sorted(u.split(":")[0] for u in plan["unavailable"]) == ["request"]
    assert plan["plansKnown"] is False and plan["unordered"] == []
    assert plan["observationsByTransaction"] == h["plan"]["observationsByTransaction"]
    s = d["series"][1]
    assert sorted(u.split(":")[0] for u in s["unavailable"]) == ["limits", "plan"]
    assert len(s["series"][0]["points"]) == 8


def test_cdr1_and_ips_down_together(fixtures_app, fixtures_client):
    """Two sources down: every patient read refuses BY NAME (spärr cannot be
    verified, or cdr1 cannot be read) -- never a 500, never an empty 200."""
    d = _down(fixtures_app, fixtures_client, "cdr1", "ips")
    for ep, (st, body) in d.items():
        assert st == 503, ep
        if isinstance(body, dict):
            assert body.get("error") in ("undetermined", "cdr1_unreachable"), (ep, body)


# ---------------------------------------------------------------------------
# SLOW: the real transport runs into requests' timeout exception
# ---------------------------------------------------------------------------

class _SlowCdr1(fx.FakeCdr1Client):
    _get_json_strict = cdr1_client.Cdr1Client._get_json_strict
    _get_json = cdr1_client.Cdr1Client._get_json


class _SlowIps(fx.FakeIpsClient):
    fetch_active_blocks = ips_client.IpsClient.fetch_active_blocks
    euips_summary = ips_client.IpsClient.euips_summary


class _SlowRequest(fx.FakeRequestClient):
    _get = request_client.RequestClient._get


class _SlowPlan(fx.FakePlanClient):
    _get_json = plan_client.PlanClient._get_json


_SLOW = {"cdr1": (_SlowCdr1, cdr1_client.DEFAULT_TIMEOUT),
         "ips": (_SlowIps, ips_client.DEFAULT_TIMEOUT),
         "request": (_SlowRequest, request_client.DEFAULT_TIMEOUT),
         "plan": (_SlowPlan, plan_client.DEFAULT_TIMEOUT)}


@pytest.fixture
def timing_out(monkeypatch):
    """Every `requests.get` raises ReadTimeout and is recorded. Only the
    source swapped to its REAL transport reaches `requests` at all."""
    calls = []

    def fake_get(url, *a, **kw):
        calls.append((url, kw.get("timeout")))
        raise requests.exceptions.ReadTimeout(f"read timed out ({url})")

    monkeypatch.setattr(requests, "get", fake_get)
    return calls


@pytest.mark.parametrize("source", sorted(_SLOW))
def test_a_slow_source_is_reported_exactly_like_a_down_one(
        fixtures_app, fixtures_client, monkeypatch, timing_out, source):
    """Every client passes `timeout=` to `requests.get` and maps
    `requests.RequestException` (Timeout included) to the same exception as a
    network error. So a source that times out must produce the SAME status
    and the SAME named entry as the `failing()` knob — asserted here end to
    end, with the timeout value each call carried."""
    urls = _urls(fixtures_app)
    with fx.failing(fixtures_app, source):
        expected = {ep: _get(fixtures_client, u)[:2] for ep, u in urls.items()}
    assert not timing_out                       # the down knob never reaches requests

    slow_cls, timeout = _SLOW[source]
    monkeypatch.setitem(fx._FAKES, source, slow_cls)
    for ep, url in urls.items():
        fx._reset_caches()
        st, body, _ = _get(fixtures_client, url)
        exp_st, exp_body = expected[ep]
        assert st == exp_st, (ep, st, body)
        if isinstance(body, dict) and "unavailable" in body:
            names = sorted(u.split(":")[0] + ":" + u.split(":")[1] for u in body["unavailable"])
            exp_names = sorted(u.split(":")[0] + ":" + u.split(":")[1] for u in exp_body["unavailable"])
            assert names == exp_names, (ep, body["unavailable"])
        elif isinstance(body, dict):
            assert body.get("error") == exp_body.get("error"), ep
    # It really was the timeout path: the source's own URLs, each BOUNDED.
    assert timing_out, "no call reached the transport"
    assert {u.split("/")[2] for u, _ in timing_out} == {source}
    assert {t for _, t in timing_out} == {timeout}
    assert all(t and t > 0 for _, t in timing_out)


def test_icd11_client_maps_a_timeout_to_unreachable(fixtures_app, monkeypatch, timing_out):
    """icd11 has no BFF route to time out on (see above); its client still
    turns a timeout into its named failure rather than an empty problem list."""
    class _SlowIcd11(fx.FakeIcd11Client):
        _get = icd11_client.Icd11Client._get

    with fixtures_app.test_request_context():
        with pytest.raises(icd11_client.Icd11Unreachable):
            _SlowIcd11(fx.state_of(fixtures_app)).problem_list(T2D)
    assert timing_out and {t for _, t in timing_out} == {icd11_client.DEFAULT_TIMEOUT}
