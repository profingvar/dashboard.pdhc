"""Smoke tests for the synthetic fixture layer (#974, Phase 6 step 22).

Every later dashboard test leans on `app/dashboard/fixtures.py`, so this file
proves the layer does what it claims, and each assertion carries a CONTROL —
the same question asked where the answer must differ — because a fixture that
returns nothing passes every "nothing is wrong" assertion vacuously.
"""
from __future__ import annotations

import pytest
import sqlalchemy

from app.dashboard import fixtures as fx

ALL = fx.PATIENTS


def _get(c, url):
    r = c.get(url)
    return r.status_code, r.get_json()


def _first_code(c, guid):
    _, b = _get(c, f"/dashboard/patients/{guid}/concepts")
    return b["concepts"][0]["code"]


# ---------------------------------------------------------------------------
# every endpoint, every patient
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("guid", ALL)
def test_every_bff_endpoint_answers_200_with_fixture_data(fixtures_client, guid):
    c = fixtures_client
    assert c.get(f"/dashboard/view/{guid}").status_code == 200

    st, card = _get(c, f"/dashboard/patients/{guid}")
    assert st == 200 and card["unavailable"] == []
    assert card["patientCard"]["name"].startswith("Fixtur")
    assert card["patientCard"]["diagnoses"], "a card with no chips proves nothing"
    assert card["sparrChecked"] is True

    st, plan = _get(c, f"/dashboard/patients/{guid}/plan")
    assert st == 200 and plan["unavailable"] == [] and plan["plans"]
    assert plan["observationCountsComplete"] is True

    st, concepts = _get(c, f"/dashboard/patients/{guid}/concepts")
    assert st == 200 and concepts["concepts"] and concepts["unavailable"] == []
    # names came from plan's fake, not the raw code
    assert all(not x["displayName"].startswith("urn:") for x in concepts["concepts"])

    code = _first_code(c, guid)
    st, series = _get(c, f"/dashboard/patients/{guid}/series/{code}")
    assert st == 200 and series["series"] and series["series"][0]["points"]

    st, events = _get(c, f"/dashboard/patients/{guid}/events")
    assert st == 200 and events["rows"] and events["unavailable"] == []

    obs = events["rows"][0]["observationGuid"]
    st, detail = _get(c, f"/dashboard/events/{obs}")
    assert st == 200 and detail["detail"]["row"]["observationGuid"] == obs


def test_the_page_renders_the_shell_with_the_patient_guid(fixtures_client):
    html = fixtures_client.get(f"/dashboard/view/{fx.PATIENT_T2D}").get_data(as_text=True)
    assert fx.PATIENT_T2D in html


# ---------------------------------------------------------------------------
# "not recorded" — the untidy data is the point
# ---------------------------------------------------------------------------

def test_events_without_transaction_or_care_plan_reach_the_page_as_such(fixtures_client):
    c = fixtures_client
    _, ev = _get(c, f"/dashboard/patients/{fx.PATIENT_T2D}/events?limit=500")
    bare = [r for r in ev["rows"] if r["transactionGuid"] is None and r["carePlanGuid"] is None]
    tagged = [r for r in ev["rows"] if r["transactionGuid"]]
    assert bare, "the not-recorded path was tidied away"
    assert tagged, "control: some rows DO carry a transaction"
    # Most real rows are bare; keep the fixture shaped like that.
    assert len(bare) > len(tagged)

    _, d = _get(c, f"/dashboard/events/{bare[0]['observationGuid']}")
    assert d["contextFound"] is False
    row = d["detail"]["row"]
    assert row["transactionGuid"] is None and row["carePlanGuid"] is None
    assert d["detail"]["planDefinitionName"] is None

    # CONTROL: a tagged row's detail is filled, so the nulls above are real.
    hba1c_tagged = next(r for r in tagged if r["codeCanonical"] == fx.code_of("hba1c"))
    _, d2 = _get(c, f"/dashboard/events/{hba1c_tagged['observationGuid']}")
    assert d2["contextFound"] is True
    assert d2["detail"]["row"]["transactionGuid"] == fx.TX["hba1c"]
    assert d2["detail"]["planDefinitionName"].endswith("(fixtur)")


def test_the_plan_panel_counts_observations_without_a_transaction(fixtures_client):
    _, b = _get(fixtures_client, f"/dashboard/patients/{fx.PATIENT_T2D}/plan")
    assert b["observationsWithoutTransaction"] > 0
    assert b["observationsByTransaction"], "control: tagged ones are counted"
    assert [u["display"] for u in b["unordered"]] == ["Midjeomfång"]


def test_plan_items_are_done_planned_AND_overdue(fixtures_client):
    _, b = _get(fixtures_client, f"/dashboard/patients/{fx.PATIENT_T2D}/plan")
    states = {i["state"] for p in b["plans"] for i in p["items"]}
    assert {"done", "planned", "overdue"} <= states
    _, hf = _get(fixtures_client, f"/dashboard/patients/{fx.PATIENT_HF}/plan")
    assert any(p["source"] == "service_request" and p["status"] == "revoked"
               for p in hf["plans"]), "an order no CarePlan claims"


# ---------------------------------------------------------------------------
# limits
# ---------------------------------------------------------------------------

def test_limits_mark_values_outside_and_name_the_unjudged(fixtures_client):
    c = fixtures_client
    _, ev = _get(c, f"/dashboard/patients/{fx.PATIENT_T2D}/events?limit=500")
    by = {}
    for r in ev["rows"]:
        by.setdefault(r["codeCanonical"], set()).add(r["limit"]["status"])
    assert "above" in by[fx.code_of("hba1c")]
    assert "inside" in by[fx.code_of("hba1c")], "control: not everything is outside"
    assert by[fx.code_of("weight")] == {"no_limit"}     # a concept with no limit
    assert by[fx.code_of("wellbeing")] == {"not_numeric"}

    hba1c = next(r for r in ev["rows"] if r["codeCanonical"] == fx.code_of("hba1c"))
    assert hba1c["limit"]["low"] is None and hba1c["limit"]["high"] == 52   # one-sided goal
    assert hba1c["limit"]["sourceKind"] == "care_plan_goal"

    _, out = _get(c, f"/dashboard/patients/{fx.PATIENT_T2D}/events?outside=1")
    assert out["rows"] and all(r["limit"]["outside"] for r in out["rows"])


def test_a_one_sided_plan_definition_range_judges_nt_probnp(fixtures_client):
    _, s = _get(fixtures_client,
                f"/dashboard/patients/{fx.PATIENT_HF}/series/{fx.code_of('ntprobnp')}")
    statuses = [p["limitStatus"] for p in s["series"][0]["points"]]
    assert "above" in statuses and "inside" in statuses


# ---------------------------------------------------------------------------
# downsampling
# ---------------------------------------------------------------------------

def test_the_cgm_series_is_downsampled_and_keeps_every_outside_point(fixtures_client):
    c = fixtures_client
    _, s = _get(c, f"/dashboard/patients/{fx.PATIENT_CGM}/series/{fx.code_of('glucose')}")
    (series,) = s["series"]
    assert series["downsampledFrom"] == 14 * 288
    assert 2000 <= len(series["points"]) < series["downsampledFrom"]
    assert s["complete"] is True
    outside_total = sum(1 for o in fx.build_dataset().patient_observations(fx.PATIENT_CGM)
                        if not 3.9 <= o["value"] <= 10.0)
    kept = sum(1 for p in series["points"] if p["limitStatus"] in ("above", "below"))
    assert outside_total > 0 and kept == outside_total
    # CONTROL: a sparse series is not downsampled.
    _, h = _get(c, f"/dashboard/patients/{fx.PATIENT_T2D}/series/{fx.code_of('hba1c')}")
    assert h["series"][0]["downsampledFrom"] is None


# ---------------------------------------------------------------------------
# spärr and diagnoses
# ---------------------------------------------------------------------------

def test_the_hf_patients_block_hides_points_and_raises_the_banner(fixtures_client):
    c = fixtures_client
    _, hf = _get(c, f"/dashboard/patients/{fx.PATIENT_HF}/series/{fx.code_of('ntprobnp')}")
    assert hf["hasBlockedSources"] is True
    assert len(hf["series"][0]["points"]) == 4          # 6 recorded, 2 blocked
    _, t2d = _get(c, f"/dashboard/patients/{fx.PATIENT_T2D}")
    assert t2d["hasBlockedSources"] is False            # control


def test_diagnosis_chips_carry_icd11_codes_and_icd11_text(fixtures_app, fixtures_client):
    seen = {}
    for g in ALL:
        _, b = _get(fixtures_client, f"/dashboard/patients/{g}")
        for d in b["patientCard"]["diagnoses"]:
            seen[d["code"]] = d["display"]
    assert {"5A11", "BD10", "BA00"} <= set(seen)
    assert [c for c in seen if "&" in c] == ["5A11&XT8W"]
    with fixtures_app.test_request_context():
        problems = fx.fake_client("icd11").problem_list(fx.PATIENT_CGM)
    (dx,) = problems.diagnoses
    assert dx.postcoordination == "5A11&XT8W" and dx.display == seen["5A11&XT8W"]


# ---------------------------------------------------------------------------
# synthetic identity (#405)
# ---------------------------------------------------------------------------

def test_every_personnummer_has_an_INVALID_luhn_checksum():
    pnrs = [i["personnummer"] for i in fx.PATIENT_INFO.values()]
    assert len(pnrs) == 3
    assert not any(fx.luhn_valid(p) for p in pnrs)
    # CONTROL: the checker accepts a valid number, and exactly one check digit
    # completes each stem — so "invalid" above is a real verdict.
    assert fx.luhn_valid("811218-9876")
    for p in pnrs:
        stem = p.replace("-", "")[2:11]
        assert sum(fx.luhn_valid(stem + str(d)) for d in range(10)) == 1


def test_every_guid_is_marked_fake():
    ds = fx.build_dataset()
    guids = ([o["observation_guid"] for o in ds.observations] + list(fx.PATIENTS)
             + [c["care_plan_guid"] for c in ds.care_plans]
             + [s["guid"] for s in ds.service_requests])
    assert guids and all(g.startswith("fa4e0974-") for g in guids)


# ---------------------------------------------------------------------------
# the switch
# ---------------------------------------------------------------------------

def _make(**cfg):
    from app import create_app
    return create_app({"TESTING": True, "DATABASE_URL": "sqlite:///:memory:",
                       "SQLALCHEMY_ENGINE_OPTIONS": {
                           "connect_args": {"check_same_thread": False},
                           "poolclass": sqlalchemy.pool.StaticPool}, **cfg})


def test_fixtures_with_auth_ON_refuse_at_startup():
    with pytest.raises(fx.FixturesRefused):
        _make(AUTH_MODE="sso", DASHBOARD_FIXTURES="1")
    # CONTROLS: auth on WITHOUT fixtures starts; fixtures with auth off install.
    assert fx.state_of(_make(AUTH_MODE="sso")) is None
    assert fx.state_of(_make(AUTH_MODE="off", DASHBOARD_FIXTURES="1")) is not None


def test_an_app_without_fixtures_keeps_the_real_clients(fixtures_app):
    from app.services import cdr1_client, plan_client
    plain = _make(AUTH_MODE="off")
    with plain.app_context():
        assert type(cdr1_client.build_client()) is cdr1_client.Cdr1Client
        assert type(plan_client.build_client()) is plan_client.PlanClient
    with fixtures_app.app_context():                      # control
        assert isinstance(cdr1_client.build_client(), fx.FakeCdr1Client)
        assert fixtures_app.config["CDR1_BASE_URL"] == "fixture://cdr1"


def test_the_env_knob_and_a_typo_in_it(monkeypatch):
    monkeypatch.setenv("DASHBOARD_FIXTURES_FAIL", "ips, plan")
    assert fx.state_of(_make(AUTH_MODE="off", DASHBOARD_FIXTURES="1")).failing == {"ips", "plan"}
    monkeypatch.setenv("DASHBOARD_FIXTURES_FAIL", "ipz")
    with pytest.raises(ValueError):
        _make(AUTH_MODE="off", DASHBOARD_FIXTURES="1")


# ---------------------------------------------------------------------------
# resilience: one source down, the rest still renders and says so BY NAME
# ---------------------------------------------------------------------------

#: source -> (endpoint suffix, expected `unavailable` entry prefix)
_DEGRADES = {
    "cdr1": ("/plan", "cdr1:Cdr1Unreachable"),
    # ips is no longer a degrade-in-place source: spärr cannot be verified, so
    # every patient read fails CLOSED (test_concepts_sparr_974, #974).
    "request": ("/plan", "request:RequestUnreachable"),
    "plan": ("/concepts", "plan:PlanUnreachable"),
}


@pytest.mark.parametrize("source", sorted(_DEGRADES))
def test_a_failing_source_is_named_in_unavailable_and_the_panel_still_answers(
        fixtures_app, fixtures_client, source):
    suffix, entry = _DEGRADES[source]
    url = f"/dashboard/patients/{fx.PATIENT_T2D}{suffix}"
    st, up = _get(fixtures_client, url)
    assert st == 200 and not any(u.startswith(entry) for u in up["unavailable"])  # control
    with fx.failing(fixtures_app, source):
        st, down = _get(fixtures_client, url)
    assert st == 200
    assert any(u.startswith(entry) for u in down["unavailable"]), down["unavailable"]
    # and it recovers when the source comes back
    assert not any(u.startswith(entry) for u in _get(fixtures_client, url)[1]["unavailable"])


def test_failing_request_leaves_limits_unknown_not_absent(fixtures_app, fixtures_client):
    url = f"/dashboard/patients/{fx.PATIENT_CGM}/series/{fx.code_of('glucose')}"
    with fx.failing(fixtures_app, "request"):
        st, b = _get(fixtures_client, url)
    assert st == 200 and "limits:RequestUnreachable" in b["unavailable"]
    assert {p["limitStatus"] for p in b["series"][0]["points"]} == {"unknown"}


def test_icd11_is_not_read_by_the_bff_so_its_failure_changes_nothing(
        fixtures_app, fixtures_client):
    """Since #984 the card reads diagnoses from the EPS; no BFF route asks
    icd11. Recorded here so the day one does, this test says to add a row."""
    from app.services.icd11_client import Icd11Unreachable
    with fx.failing(fixtures_app, "icd11"):
        with fixtures_app.test_request_context():
            with pytest.raises(Icd11Unreachable):        # control: the knob works
                fx.fake_client("icd11").problem_list(fx.PATIENT_T2D)
        for suffix in ("", "/plan", "/concepts", "/events"):
            st, b = _get(fixtures_client, f"/dashboard/patients/{fx.PATIENT_T2D}{suffix}")
            assert st == 200 and not any("icd11" in u for u in b["unavailable"])


def test_cdr1_down_makes_the_event_list_503_by_name_not_empty(fixtures_app, fixtures_client):
    """The event list has no partial form: a dead cdr1 is a named 503."""
    with fx.failing(fixtures_app, "cdr1"):
        st, b = _get(fixtures_client, f"/dashboard/patients/{fx.PATIENT_T2D}/events")
    assert st == 503 and b["error"] == "cdr1_unreachable"
