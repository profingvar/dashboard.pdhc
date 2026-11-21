"""Per-panel ROUTE tests for the patient dashboard (#974, Phase 6 step 23).

Gate 0 chose Jinja + vanilla JS, so a "component test per panel" is a test of
the JSON route that panel renders. The page draws what these routes return and
recomputes nothing (limits, T+, spärr, downsampling are all decided here), so
the wire content IS the panel's content.

Driven by the synthetic fixture layer (`app/dashboard/fixtures.py`) and built
ON TOP of `test_dashboard_fixtures_974.py`, which proves the layer answers;
this file asserts what each panel says. Expected values are derived from the
fixture DATASET wherever possible (not re-typed), so a test fails when the
route and the data disagree, not when someone tidies a constant.

Every positive assertion carries a CONTROL: the same question asked where the
answer must differ. A route that returns nothing, or the same thing for every
input, would otherwise pass "the value is X" checks vacuously.

NOT tested here, deliberately: the `?outside=1` (Utanför gräns) filter — a
defect there is being fixed in `app/routes/dashboard.py` at the time of
writing, and `test_outside_filter_goal_974.py` owns it.
"""
from __future__ import annotations

import copy
import csv
import io
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pytest

from app.dashboard import fixtures as fx

T2D, HF, CGM = fx.PATIENTS
STHLM = ZoneInfo("Europe/Stockholm")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _get(c, url):
    r = c.get(url)
    return r.status_code, r.get_json(silent=True)


def _ds(app) -> fx.Dataset:
    return fx.state_of(app).dataset


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _events_url(guid, **params):
    q = urlencode([(k, v) for k, vs in params.items()
                   for v in (vs if isinstance(vs, (list, tuple)) else [vs])])
    return f"/dashboard/patients/{guid}/events" + (f"?{q}" if q else "")


def _walk(c, guid, *, limit=50, **params):
    """Every event row, following the cursor. Returns (rows, pages)."""
    rows, cursor, pages = [], None, 0
    while True:
        extra = dict(params, limit=limit)
        if cursor:
            extra["cursor"] = cursor
        st, b = _get(c, _events_url(guid, **extra))
        assert st == 200, b
        rows += b["rows"]
        pages += 1
        cursor = b["nextCursor"]
        if not cursor:
            return rows, pages
        assert pages < 200, "the cursor never ended"


def _blocked_guids(app, patient):
    return {o["observation_guid"] for o in _ds(app).patient_observations(patient)
            if o["org_guid"] == fx.ORG_BLOCKED}


def _card(c, guid):
    st, b = _get(c, f"/dashboard/patients/{guid}")
    assert st == 200, b
    return b


def _plan(c, guid):
    st, b = _get(c, f"/dashboard/patients/{guid}/plan")
    assert st == 200, b
    return b


def _series(c, guid, concept, **params):
    q = urlencode(params)
    st, b = _get(c, f"/dashboard/patients/{guid}/series/{fx.code_of(concept)}"
                    + (f"?{q}" if q else ""))
    assert st == 200, b
    return b


def _detail(c, obs_guid):
    st, b = _get(c, f"/dashboard/events/{obs_guid}")
    assert st == 200, b
    return b


def _obs(app, patient, concept, *, tagged=None, care_plan=None, org=None):
    """Dataset observations of one concept, filtered on the context keys."""
    out = []
    for o in _ds(app).patient_observations(patient):
        if o["code"] != fx.code_of(concept):
            continue
        if tagged is not None and bool(o["transaction_guid"]) != tagged:
            continue
        if care_plan is not None and bool(o["care_plan_guid"]) != care_plan:
            continue
        if org is not None and o["org_guid"] != org:
            continue
        out.append(o)
    return out


# ===========================================================================
# CARD
# ===========================================================================

def test_card_chips_carry_display_text_status_and_cdr_backing(fixtures_client):
    hf = _card(fixtures_client, HF)
    chips = {d["code"]: d for d in hf["patientCard"]["diagnoses"]}
    assert set(chips) == {"BD10", "BA00", "5A11"}
    for code, d in chips.items():
        # Display text is the ICD-11 label, never the bare code.
        assert d["display"] == fx.ICD11_LABELS[code] and d["display"] != code
        assert d["system"] == "http://id.who.int/icd/release/11/mms"

    # cdr-backed vs EPS-only: 5A11 on the HF patient is held by the EPS alone.
    assert chips["5A11"]["cdrBacked"] is False and chips["5A11"]["cdr"] is None
    assert chips["BD10"]["cdrBacked"] is True and chips["BD10"]["cdr"] == "cdr2"
    assert hf["diagnosisSource"] == {
        "source": "eps", "label": "Patientsammanfattning (härledd ur CDR)",
        "available": True, "count": 3, "cdrBacked": 2}

    # Resolved is shown AS resolved, not dropped and not shown as active.
    assert chips["BA00"]["clinicalStatus"] == "resolved"
    assert chips["BD10"]["clinicalStatus"] == "active"

    # CONTROL: the same code on another patient is active and CDR-backed, so
    # status and backing come from the data, not from a constant per code.
    t2d = {d["code"]: d for d in _card(fixtures_client, T2D)["patientCard"]["diagnoses"]}
    assert t2d["BA00"]["clinicalStatus"] == "active"
    assert t2d["5A11"]["cdrBacked"] is True
    assert _card(fixtures_client, T2D)["diagnosisSource"]["cdrBacked"] == 2


def test_card_carries_the_cluster_code_whole(fixtures_client):
    (chip,) = _card(fixtures_client, CGM)["patientCard"]["diagnoses"]
    assert chip["code"] == "5A11&XT8W"                 # not split into two chips
    assert chip["display"] == fx.ICD11_LABELS["5A11&XT8W"]
    # CONTROL: a stem-only patient has no cluster.
    assert not any("&" in d["code"]
                   for d in _card(fixtures_client, T2D)["patientCard"]["diagnoses"])


def test_card_eps_sections_tell_absent_from_missing(fixtures_client):
    t2d = {s["sectionCode"]: s for s in _card(fixtures_client, T2D)["patientCard"]["epsSections"]}
    assert set(t2d) == {"48765-2", "11450-4", "10160-0"}

    allergies = t2d["48765-2"]
    assert allergies["absentAssertion"] is True
    assert allergies["narrative"] == "Inga kända allergier" and allergies["entries"] == []

    # The problems section is the same list the chips are built from.
    problems = t2d["11450-4"]
    assert [e["code"] for e in problems["entries"]] == ["5A11", "BA00"]
    assert {e["source"] for e in problems["entries"]} == {"ips"}

    meds = t2d["10160-0"]
    assert [(e["code"], e["display"], e["system"]) for e in meds["entries"]] == [
        ("A10BA02", "Metformin 500 mg", "http://www.whocc.no/atc")]

    # CONTROL: the HF patient's medications are MISSING — no entries AND no
    # "none known" assertion. Empty is not the same statement as absent.
    hf_meds = next(s for s in _card(fixtures_client, HF)["patientCard"]["epsSections"]
                   if s["sectionCode"] == "10160-0")
    assert hf_meds["entries"] == [] and hf_meds["absentAssertion"] is False
    assert hf_meds["narrative"] is None


@pytest.mark.parametrize("guid", fx.PATIENTS)
def test_card_demographics_without_a_source_are_absent_not_invented(fixtures_client, guid):
    r = fixtures_client.get(f"/dashboard/patients/{guid}")
    card = r.get_json()["patientCard"]
    info = fx.PATIENT_INFO[guid]
    # No source the card reads carries these: null, which the page shows as "—".
    assert card["sex"] is None
    assert card["personnummer"] is None
    assert card["organisation"] is None
    # ips DOES hold a personnummer (fixture EPS `patient`), and the card must
    # not have picked it up from there or anywhere else.
    assert info["personnummer"] not in r.get_data(as_text=True)
    # CONTROL: what the card HAS a source for (cdr1's patient index) is filled,
    # so the nulls above are not a card that fills nothing.
    assert card["name"] == info["name"]
    assert card["birthDate"] == info["birth_date"]


# ===========================================================================
# PLAN
# ===========================================================================

def test_plan_states_are_judged_against_the_clock(fixtures_app, fixtures_client):
    b = _plan(fixtures_client, T2D)
    a = _ds(fixtures_app).anchor
    now = _dt(b["now"])
    (plan,) = b["plans"]
    assert plan["carePlanGuid"] == fx.CP_T2D and plan["source"] == "care_plan"
    items = {i["title"]: i for i in plan["items"]}

    done = items["HbA1c"]
    assert done["state"] == "done"
    assert _dt(done["performedAt"]) == a - timedelta(days=100)   # the match's response_at

    overdue = items["Systoliskt blodtryck"]
    assert overdue["state"] == "overdue" and overdue["performedAt"] is None
    assert _dt(overdue["scheduledAt"]) == a - timedelta(days=14) < now

    planned = items["Självskattad hälsa (EQ VAS)"]
    assert planned["state"] == "planned"
    assert _dt(planned["scheduledAt"]) == a + timedelta(days=21) > now

    # The rule for every non-terminal item: overdue <=> due date passed.
    for i in plan["items"]:
        if i["state"] in ("done", "cancelled"):
            continue
        due = _dt(i["scheduledAt"]) if i["scheduledAt"] else None
        assert (i["state"] == "overdue") == (due is not None and due < now), i

    # CONTROL: an order with NO due date is planned however old it is (the
    # HF order was created 95 days ago) — "late" is undefined without a date.
    hf_cp = next(p for p in _plan(fixtures_client, HF)["plans"] if p["source"] == "care_plan")
    assert {i["state"] for i in hf_cp["items"]} == {"planned"}
    assert all(i["scheduledAt"] is None for i in hf_cp["items"])


def test_plan_shows_the_unclaimed_revoked_order_on_hf(fixtures_client):
    plans = _plan(fixtures_client, HF)["plans"]
    assert len(plans) == 2
    sr = next(p for p in plans if p["source"] == "service_request")
    assert sr["carePlanGuid"] is None                    # no CarePlan claims it
    assert sr["serviceRequestGuid"] == fx._g(fx.K_SR, 5)
    assert sr["status"] == "revoked"
    assert sr["planDefinitionName"] == "Ekokardiografi (fixtur)"
    assert [(i["title"], i["state"]) for i in sr["items"]] == [
        ("Ejektionsfraktion (LVEF)", "cancelled")]
    # CONTROL: the claimed order is folded into its CarePlan, not cancelled.
    cp = next(p for p in plans if p["source"] == "care_plan")
    assert cp["carePlanGuid"] == fx.CP_HF
    assert "cancelled" not in {i["state"] for i in cp["items"]}


def test_plan_counts_match_the_dataset(fixtures_app, fixtures_client):
    b = _plan(fixtures_client, T2D)
    obs = _ds(fixtures_app).patient_observations(T2D)
    expected: dict[str, int] = {}
    for o in obs:
        if o["transaction_guid"]:
            expected[o["transaction_guid"]] = expected.get(o["transaction_guid"], 0) + 1
    got = {tx: v["count"] for tx, v in b["observationsByTransaction"].items()}
    assert got == expected
    assert got[fx.TX["hba1c"]] == 3 and got[fx.TX["sys"]] == 7     # literal anchors
    assert b["observationsWithoutTransaction"] == sum(1 for o in obs if not o["transaction_guid"]) == 83
    # first/last are the true extremes of each transaction's observations
    hb = [o["at"] for o in obs if o["transaction_guid"] == fx.TX["hba1c"]]
    assert b["observationsByTransaction"][fx.TX["hba1c"]]["first"] == min(hb)
    assert b["observationsByTransaction"][fx.TX["hba1c"]]["last"] == max(hb)
    # Only the waist transaction is unordered; the planned ones are not listed.
    assert [(u["transactionGuid"], u["count"]) for u in b["unordered"]] == [(fx.TX["waist"], 3)]


def test_plan_counts_exclude_blocked_observations(fixtures_app, fixtures_client):
    """A count of blocked observations would disclose that they exist."""
    b = _plan(fixtures_client, HF)
    obs = _ds(fixtures_app).patient_observations(HF)
    blocked = _blocked_guids(fixtures_app, HF)
    assert len(blocked) == 2
    untagged_all = sum(1 for o in obs if not o["transaction_guid"])
    untagged_blocked = sum(1 for o in obs if not o["transaction_guid"]
                           and o["observation_guid"] in blocked)
    assert untagged_blocked == 2                    # control: the block bites here
    assert b["observationsWithoutTransaction"] == untagged_all - untagged_blocked
    assert b["hasBlockedSources"] is True and b["sparrChecked"] is True


# ===========================================================================
# EVENTS
# ===========================================================================

def test_events_are_newest_first_with_a_total_order(fixtures_app, fixtures_client):
    rows, _ = _walk(fixtures_client, T2D, limit=500)
    assert len(rows) == len(_ds(fixtures_app).patient_observations(T2D)) == 111
    keys = [(r["effectiveAt"], r["observationGuid"]) for r in rows]
    assert keys == sorted(keys, reverse=True)
    assert len({r["observationGuid"] for r in rows}) == len(rows)
    # CONTROL: the order is not trivially constant — times actually decrease.
    assert _dt(rows[0]["effectiveAt"]) > _dt(rows[-1]["effectiveAt"])


def test_events_cursor_pages_add_up_to_the_whole_list(fixtures_client):
    whole, _ = _walk(fixtures_client, T2D, limit=500)
    paged, pages = _walk(fixtures_client, T2D, limit=10)
    assert pages == 12                                         # 111 rows / 10
    assert [r["observationGuid"] for r in paged] == [r["observationGuid"] for r in whole]

    st, first = _get(fixtures_client, _events_url(T2D, limit=10))
    assert len(first["rows"]) == 10 and first["nextCursor"]
    # A page past the end has no cursor (control for "always a cursor").
    st, last = _get(fixtures_client, _events_url(T2D, limit=10, cursor="fx1:110"))
    assert len(last["rows"]) == 1 and last["nextCursor"] is None

    # Refusals by name, never an empty or unfiltered page.
    st, b = _get(fixtures_client, _events_url(T2D, cursor="garbage"))
    assert st == 400 and b["error"] == "invalid_cursor"
    st, b = _get(fixtures_client, _events_url(T2D, limit="abc"))
    assert st == 400 and b["error"] == "invalid_limit"
    st, b = _get(fixtures_client, _events_url(T2D, limit=100000))
    assert st == 200 and len(b["rows"]) == 111      # capped at 500, so all of 111


def test_events_concept_search_is_exact_over_the_whole_history(fixtures_client):
    """The page matches free text against the CONCEPT names and sends a code
    filter, so the result is the concept's whole history, not a page slice."""
    st, cs = _get(fixtures_client, f"/dashboard/patients/{T2D}/concepts")
    by_name = {c["displayName"]: c for c in cs["concepts"]}
    hba1c = by_name["HbA1c"]
    assert hba1c["code"] == fx.code_of("hba1c") and hba1c["count"] == 8

    rows, _ = _walk(fixtures_client, T2D, limit=3, code=hba1c["code"])
    assert len(rows) == hba1c["count"]
    assert {r["codeCanonical"] for r in rows} == {hba1c["code"]}

    # Two codes: the union, nothing else.
    sys_code = by_name["Systoliskt blodtryck"]["code"]
    rows2, _ = _walk(fixtures_client, T2D, limit=500, code=[hba1c["code"], sys_code])
    assert {r["codeCanonical"] for r in rows2} == {hba1c["code"], sys_code}
    assert len(rows2) == hba1c["count"] + by_name["Systoliskt blodtryck"]["count"]

    # CONTROL: unfiltered, other concepts are present.
    allrows, _ = _walk(fixtures_client, T2D, limit=500)
    assert len({r["codeCanonical"] for r in allrows}) > 2


def test_events_bare_date_filter_is_a_stockholm_calendar_day(fixtures_app, fixtures_client):
    a = _ds(fixtures_app).anchor
    day = (a - timedelta(days=14)).astimezone(STHLM).date()
    expected = {o["observation_guid"] for o in _ds(fixtures_app).patient_observations(T2D)
                if _dt(o["at"]).astimezone(STHLM).date() == day}
    assert 2 <= len(expected) < 111                 # control: a real, partial day
    rows, _ = _walk(fixtures_client, T2D, limit=500, **{"from": day.isoformat(),
                                                        "to": day.isoformat()})
    assert {r["observationGuid"] for r in rows} == expected


def test_events_instant_window_filter(fixtures_app, fixtures_client):
    a = _ds(fixtures_app).anchor
    frm, to = a - timedelta(days=60), a - timedelta(days=30)
    expected = {o["observation_guid"] for o in _ds(fixtures_app).patient_observations(T2D)
                if frm <= _dt(o["at"]) <= to}
    rows, _ = _walk(fixtures_client, T2D, limit=500, **{"from": _z(frm), "to": _z(to)})
    assert {r["observationGuid"] for r in rows} == expected and expected

    # Malformed bounds are a 400 by name, never a silently dropped filter.
    for bad in ({"from": "2026-13-45"}, {"from": "2026-11-01T10:00:00"},   # no zone
                {"from": _z(to), "to": _z(frm)}):                           # reversed
        st, b = _get(fixtures_client, _events_url(T2D, **bad))
        assert st == 400 and b["error"] == "invalid_filter", bad


def test_events_plan_filter_agrees_with_the_plan_panel(fixtures_client):
    plan = _plan(fixtures_client, T2D)
    tx = fx.TX["hba1c"]
    rows, _ = _walk(fixtures_client, T2D, limit=500, transaction=tx)
    assert {r["transactionGuid"] for r in rows} == {tx}
    assert len(rows) == plan["observationsByTransaction"][tx]["count"] == 3
    # CONTROL: the concept has more rows than the transaction (5 untagged).
    allhb, _ = _walk(fixtures_client, T2D, limit=500, code=fx.code_of("hba1c"))
    assert len(allhb) == 8


def test_events_rows_without_context_say_not_recorded(fixtures_app, fixtures_client):
    rows, _ = _walk(fixtures_client, T2D, limit=500)
    bare_ds = {o["observation_guid"] for o in _ds(fixtures_app).patient_observations(T2D)
               if not o["transaction_guid"] and not o["care_plan_guid"]}
    bare = [r for r in rows if r["observationGuid"] in bare_ds]
    assert len(bare) == len(bare_ds) > 50
    for r in bare:
        assert r["transactionGuid"] is None and r["carePlanGuid"] is None
        assert r["serviceRequestGuid"] is None and r["planActivityGuid"] is None
        assert r["tFromStartSeconds"] is None

    # The detail names EVERY context key, null, so the panel can say
    # "ej registrerat" field by field rather than omit the field.
    # #1014: an unmarked (healthcare) row, so data_origin is null too.
    plain = next(r for r in bare if r["dataOrigin"] is None)
    d = _detail(fixtures_client, plain["observationGuid"])
    ctx_keys = {"plan_definition_guid", "contract_guid", "provider_org_guid",
                "service_request_guid", "care_plan_guid", "transaction_guid",
                "plan_activity_guid", "author_org_guid", "requesting_org_guid",
                "data_origin"}
    assert set(d["context"]) == ctx_keys
    assert all(d["context"][k] is None for k in ctx_keys)
    for k in ("contractGuid", "providerOrgGuid", "planDefinitionName",
              "planDefinitionVersion", "patientOrgGuid"):
        assert k in d["detail"] and d["detail"][k] is None, k
    # CONTROL: a fully tagged row fills the same keys.
    tagged = _obs(fixtures_app, T2D, "hba1c", tagged=True)[0]
    dt_ = _detail(fixtures_client, tagged["observation_guid"])
    assert dt_["context"]["service_request_guid"] == tagged["service_request_guid"]
    assert dt_["detail"]["contractGuid"] and dt_["detail"]["providerOrgGuid"]


def test_events_hide_blocked_rows_and_raise_the_banner(fixtures_app, fixtures_client):
    blocked = _blocked_guids(fixtures_app, HF)
    assert len(blocked) == 2
    b = _get(fixtures_client, _events_url(HF, limit=500))[1]
    rows, _ = _walk(fixtures_client, HF, limit=500)
    got = {r["observationGuid"] for r in rows}
    assert not (got & blocked)
    assert len(rows) == len(_ds(fixtures_app).patient_observations(HF)) - 2
    assert b["hasBlockedSources"] is True and b["sparrChecked"] is True
    # CONTROL: the unblocked NT-proBNP results ARE listed.
    nt = {o["observation_guid"] for o in _obs(fixtures_app, HF, "ntprobnp")} - blocked
    assert len(nt) == 4 and nt <= got
    # CONTROL: a patient without blocks says so, and was still checked.
    t2d = _get(fixtures_client, _events_url(T2D, limit=1))[1]
    assert t2d["hasBlockedSources"] is False and t2d["sparrChecked"] is True

    # A blocked event's detail answers EXACTLY like one that does not exist.
    st_b, body_b = _get(fixtures_client, f"/dashboard/events/{sorted(blocked)[0]}")
    st_n, body_n = _get(fixtures_client, f"/dashboard/events/{fx._g(fx.K_OBS, 0xFFFFFF)}")
    assert (st_b, body_b) == (st_n, body_n) == (404, {"error": "not_found",
                                                      "message": "no such observation in your scope"})


# ===========================================================================
# EVENT DETAIL
# ===========================================================================

def test_detail_limit_is_the_care_plan_goal_and_says_so(fixtures_app, fixtures_client):
    o = _obs(fixtures_app, T2D, "hba1c", tagged=True, care_plan=True)[0]
    lim = _detail(fixtures_client, o["observation_guid"])["limit"]
    assert lim["status"] == "above" and lim["outside"] is True
    assert lim["comparedValue"] == o["value"] == 53.0
    # The GOAL (<= 52) wins over the PlanDefinition range (20–48) for this
    # transaction: the reported bound is 52, not 48.
    assert (lim["low"], lim["high"], lim["unit"]) == (None, 52.0, "mmol/mol")
    assert lim["sourceKind"] == "care_plan_goal" and lim["limitKind"] == "action"
    assert lim["setBy"] == "requester" and lim["description"] == "HbA1c högst 52"
    assert lim["carePlanGuid"] == fx.CP_T2D
    # CONTROL: an older HbA1c (49) is inside the same goal; under the PD
    # range it would have been above 48.
    older = next(x for x in _obs(fixtures_app, T2D, "hba1c") if x["value"] == 49.0)
    lim2 = _detail(fixtures_client, older["observation_guid"])["limit"]
    assert lim2["status"] == "inside" and lim2["high"] == 52.0


def test_detail_limit_from_a_plan_definition_range_says_recommended(fixtures_app, fixtures_client):
    nt = {o["value"]: o for o in _obs(fixtures_app, HF, "ntprobnp", tagged=True)}
    assert set(nt) == {310.0, 380.0, 520.0}
    lim = _detail(fixtures_client, nt[520.0]["observation_guid"])["limit"]
    assert lim["status"] == "above"
    assert (lim["low"], lim["high"], lim["unit"]) == (None, 400.0, "ng/L")
    assert lim["sourceKind"] == "care_plan_range"
    assert lim["limitKind"] == "recommended_normal" and lim["setBy"] == "plan_recommended"
    assert lim["planDefinitionName"] == "Hjärtsvikt, egenmonitorering (fixtur)"
    assert lim["planDefinitionVersion"] == "1.0" and lim["description"] is None
    # CONTROL: same range, value under it.
    assert _detail(fixtures_client, nt[310.0]["observation_guid"])["limit"]["status"] == "inside"
    # CONTROL: a range is per TRANSACTION — the untagged (unblocked) result
    # has no transaction and no goal, so it is not judged at all.
    untagged = [o for o in _obs(fixtures_app, HF, "ntprobnp", tagged=False)
                if o["org_guid"] != fx.ORG_BLOCKED]
    assert len(untagged) == 1
    assert _detail(fixtures_client, untagged[0]["observation_guid"])["limit"]["status"] == "no_limit"


def test_detail_weight_without_a_limit_says_no_limit(fixtures_app, fixtures_client):
    for o in (_obs(fixtures_app, T2D, "weight", tagged=True)[0],
              _obs(fixtures_app, T2D, "weight", tagged=False)[0]):
        lim = _detail(fixtures_client, o["observation_guid"])["limit"]
        assert lim["status"] == "no_limit" and lim["outside"] is False
        assert lim["low"] is None and lim["high"] is None and lim["sourceKind"] is None
        assert "no care plan" in lim["reason"]
    # CONTROL: the SAME concept on the HF patient is judged against its goal.
    hf = _obs(fixtures_app, HF, "weight")[0]
    lim = _detail(fixtures_client, hf["observation_guid"])["limit"]
    assert lim["status"] in ("inside", "above") and lim["high"] == 84.0
    assert lim["sourceKind"] == "care_plan_goal"


def test_detail_free_text_is_not_numeric(fixtures_app, fixtures_client):
    o = _obs(fixtures_app, T2D, "wellbeing")[0]
    d = _detail(fixtures_client, o["observation_guid"])
    assert d["detail"]["row"]["value"] == o["value_string"] and isinstance(o["value_string"], str)
    assert d["limit"]["status"] == "not_numeric" and d["limit"]["comparedValue"] is None
    # CONTROL: a numeric self-report next to it is judged (no limit, but judged).
    eq = _obs(fixtures_app, T2D, "eqvas")[0]
    assert _detail(fixtures_client, eq["observation_guid"])["limit"]["status"] == "no_limit"


def _t_plus_expected(app, obs):
    cp = next(c for c in _ds(app).care_plans if c["care_plan_guid"] == obs["care_plan_guid"])
    return int((_dt(obs["at"]) - _dt(cp["period_start"])).total_seconds())


def test_t_plus_is_given_where_a_care_plan_exists(fixtures_app, fixtures_client):
    rows = {r["observationGuid"]: r for r in _walk(fixtures_client, T2D, limit=500)[0]}
    with_cp = [o for o in _ds(fixtures_app).patient_observations(T2D) if o["care_plan_guid"]]
    assert len(with_cp) == 2
    for o in with_cp:
        assert rows[o["observation_guid"]]["tFromStartSeconds"] == _t_plus_expected(fixtures_app, o)
        assert rows[o["observation_guid"]]["tFromStartSeconds"] > 0
    # CONTROL: every other row has no T+ (no plan start to count from).
    others = [r for g, r in rows.items() if g not in {o["observation_guid"] for o in with_cp}]
    assert others and all(r["tFromStartSeconds"] is None for r in others)


def test_detail_t_plus_matches_the_list_where_a_care_plan_exists(fixtures_app, fixtures_client):
    o = next(o for o in _ds(fixtures_app).patient_observations(T2D) if o["care_plan_guid"])
    d = _detail(fixtures_client, o["observation_guid"])
    assert d["detail"]["row"]["tFromStartSeconds"] == _t_plus_expected(fixtures_app, o)


# ===========================================================================
# SERIES
# ===========================================================================

def _add_obs(app, patient, concept, value, unit, *, hours_ago):
    """Append one untagged observation to the fixture dataset (this test's
    app only — `fixtures_app` is built per test)."""
    ds = _ds(app)
    src = _obs(app, patient, concept)[0]
    o = copy.deepcopy(src)
    n = 0xF000 + len(ds.observations)
    at = ds.anchor - timedelta(hours=hours_ago)
    o.update(observation_guid=fx._g(fx.K_OBS, n), ingest_raw_guid=fx._g(fx.K_INGEST, n),
             value=value, unit=unit, value_string=None, at=at.isoformat(),
             received_at=at.isoformat(), transaction_guid=None, care_plan_guid=None,
             service_request_guid=None, plan_definition_guid=None,
             plan_activity_guid=None, contract_guid=None, provider_org_guid=None)
    ds.observations.append(o)
    ds.index()
    return o["observation_guid"]


def test_series_units_join_when_convertible_and_split_when_not(fixtures_app, fixtures_client):
    g_weight = _add_obs(fixtures_app, T2D, "weight", 91000.0, "g", hours_ago=30)
    odd_weight = _add_obs(fixtures_app, T2D, "weight", 5.0, "mmol/L", hours_ago=29)
    kpa = _add_obs(fixtures_app, T2D, "sys", 20.0, "kPa", hours_ago=28)
    pct = _add_obs(fixtures_app, T2D, "hba1c", 7.0, "%", hours_ago=27)

    # grams JOIN the kilogram series (the majority unit), converted.
    w = {s["unit"]: s for s in _series(fixtures_client, T2D, "weight")["series"]}
    assert set(w) == {"kg", "mmol/L"}
    p = next(p for p in w["kg"]["points"] if p["observationGuid"] == g_weight)
    assert p["value"] == pytest.approx(91.0) and p["unit"] == "kg"
    assert len(w["kg"]["points"]) == 25
    # a different dimension SPLITS rather than sharing an axis
    assert [p["observationGuid"] for p in w["mmol/L"]["points"]] == [odd_weight]

    # kPa joins mm[Hg], converted, and is JUDGED in the series unit: 150 > 135.
    b = _series(fixtures_client, T2D, "sys")
    (s,) = b["series"]
    assert s["unit"] == "mm[Hg]" and len(s["points"]) == 27
    p = next(p for p in s["points"] if p["observationGuid"] == kpa)
    assert p["value"] == pytest.approx(150.0, abs=0.1) and p["limitStatus"] == "above"

    # HbA1c % vs mmol/mol: on a cdr1 URN code there is no substance, so the
    # affine NGSP/IFCC bridge is NOT applied — the % value is its own series,
    # NOT judged against the mmol/mol goal (unit_mismatch), and gets no band.
    b = _series(fixtures_client, T2D, "hba1c")
    h = {s["unit"]: (s, band) for s, band in zip(b["series"], b["limitBands"])}
    assert set(h) == {"%", "mmol/mol"}
    pct_series, pct_band = h["%"]
    assert [(p["observationGuid"], p["limitStatus"]) for p in pct_series["points"]] == [
        (pct, "unit_mismatch")]
    assert pct_band["band"] is None
    # CONTROL: the mmol/mol series keeps its band and its 8 points.
    mm_series, mm_band = h["mmol/mol"]
    assert len(mm_series["points"]) == 8 and mm_band["band"]["high"] == 52.0


def test_series_untouched_by_extra_units_is_a_single_group(fixtures_client):
    """CONTROL for the test above: without the added rows, one series each."""
    for concept in ("weight", "sys", "hba1c"):
        assert len(_series(fixtures_client, T2D, concept)["series"]) == 1


def test_series_lttb_keeps_identity_endpoints_and_order(fixtures_app, fixtures_client):
    # The count and the outside-value guarantee are in the smoke tests; here:
    # the selection is made of REAL observations, in time order, ends kept.
    raw = sorted(_ds(fixtures_app).patient_observations(CGM), key=lambda o: o["at"])
    (s,) = _series(fixtures_client, CGM, "glucose")["series"]
    pts = s["points"]
    by_guid = {o["observation_guid"]: o for o in raw}
    assert all(p["observationGuid"] in by_guid for p in pts)
    assert all(p["value"] == by_guid[p["observationGuid"]]["value"] for p in pts)
    assert [p["effectiveAt"] for p in pts] == sorted(p["effectiveAt"] for p in pts)
    assert pts[0]["observationGuid"] == raw[0]["observation_guid"]
    assert pts[-1]["observationGuid"] == raw[-1]["observation_guid"]
    outside = sum(1 for o in raw if not 3.9 <= o["value"] <= 10.0)
    assert len(pts) <= 2000 + outside < len(raw)


def test_series_zoom_window_is_full_resolution(fixtures_app, fixtures_client):
    start = _ds(fixtures_app).anchor - timedelta(days=14)
    frm, to = start + timedelta(days=1), start + timedelta(days=2) - timedelta(seconds=1)
    b = _series(fixtures_client, CGM, "glucose", **{"from": _z(frm), "to": _z(to)})
    (s,) = b["series"]
    assert s["downsampledFrom"] is None and len(s["points"]) == 288   # one day of 5-min
    assert b["window"]["from"] and b["window"]["to"]
    # CONTROL: the unwindowed series IS downsampled.
    assert _series(fixtures_client, CGM, "glucose")["series"][0]["downsampledFrom"] == 4032


def _csv(c, guid, concept):
    r = c.get(f"/dashboard/patients/{guid}/series/{fx.code_of(concept)}?format=csv")
    assert r.status_code == 200
    text = r.get_data(as_text=True)
    comments = [ln for ln in text.splitlines() if ln.startswith("#")]
    body = list(csv.reader(io.StringIO("\n".join(
        ln for ln in text.splitlines() if not ln.startswith("#")))))
    return r, comments, body[0], body[1:]


_CSV_HEADER = ["code_canonical", "unit", "effective_at", "value",
               "observation_guid", "transaction_guid", "limit_status",
               "data_origin"]          # #1014: appended last, positions kept


def test_series_csv_export_carries_the_same_points_and_its_caveats(fixtures_app, fixtures_client):
    r, comments, header, rows = _csv(fixtures_client, CGM, "glucose")
    assert r.mimetype == "text/csv"
    disp = r.headers["Content-Disposition"]
    assert disp.startswith('attachment; filename="series_') and disp.endswith('.csv"')
    assert header == _CSV_HEADER

    js = _series(fixtures_client, CGM, "glucose")["series"][0]["points"]
    assert comments == [f"# mmol/L: showing {len(js)} of 4032 points (LTTB)"]
    assert [row[4] for row in rows] == [p["observationGuid"] for p in js]
    assert {row[0] for row in rows} == {fx.code_of("glucose")}
    assert {row[1] for row in rows} == {"mmol/L"}
    outside_total = sum(1 for o in _ds(fixtures_app).patient_observations(CGM)
                        if not 3.9 <= o["value"] <= 10.0)
    assert sum(1 for row in rows if row[6] in ("above", "below")) == outside_total
    assert {row[5] for row in rows} == {fx.TX["glucose"]}

    # CONTROL: a short series has no downsampling caveat and every point.
    _, c2, h2, rows2 = _csv(fixtures_client, T2D, "hba1c")
    assert c2 == [] and h2 == _CSV_HEADER and len(rows2) == 8
    assert {row[6] for row in rows2} == {"inside", "above"}
    # untagged rows export an empty transaction, not the string "None"
    assert "" in {row[5] for row in rows2} and "None" not in {row[5] for row in rows2}


def test_series_band_for_one_sided_and_two_sided_limits(fixtures_client):
    (hb,) = _series(fixtures_client, T2D, "hba1c")["limitBands"]
    assert hb["band"]["low"] is None and hb["band"]["high"] == 52.0
    assert hb["band"]["unit"] == "mmol/mol" and hb["band"]["limitKind"] == "action"

    (nt,) = _series(fixtures_client, HF, "ntprobnp")["limitBands"]
    assert nt["band"]["low"] is None and nt["band"]["high"] == 400.0
    assert nt["band"]["sourceKind"] == "care_plan_range"
    assert nt["band"]["limitKind"] == "recommended_normal"

    # CONTROL: a two-sided goal has both edges.
    (gl,) = _series(fixtures_client, CGM, "glucose")["limitBands"]
    assert (gl["band"]["low"], gl["band"]["high"]) == (3.9, 10.0)

    # CONTROL: no limit, no band — and the reason says why.
    (w,) = _series(fixtures_client, T2D, "weight")["limitBands"]
    assert w == {"band": None, "reason": "no judged point", "statuses": ["no_limit"]}
