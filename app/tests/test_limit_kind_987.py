"""#987: a limit says whether it is an ACTION limit (request.pdhc, per patient)
or plan's RECOMMENDED NORMAL range, and whose value it is."""
from app.dashboard import limits as L

PAT = "pat-1"


def _cp(goals=(), tx_extra=None):
    tx = {"guid": "tx-1", "concept_guid": "c-dia", "range_min": 60, "range_max": 90, "unit": "mm[Hg]"}
    tx.update(tx_extra or {})
    return {"guid": "cp-1", "patient_guid": PAT, "status": "active", "goals": list(goals),
            "plan_definition_snapshot": {"title": "BP", "version": "1.1.0",
                                         "activities": [{"transactions": [tx]}]}}


def _goal(**kw):
    g = {"concept_guid": "c-dia", "target_type": "range", "target_range_low": 55,
         "target_range_high": 95, "target_unit": "mm[Hg]"}
    g.update(kw)
    return g


def _verdict(cps, value=70):
    return L.evaluate(value, "mm[Hg]", "tx-1", L.limits_from_care_plans(cps, PAT), concept_guid="c-dia")


def test_a_goal_is_an_ACTION_limit_and_says_who_set_it():
    w = L.wire(_verdict([_cp([_goal(limit_source="requester")])]))
    assert (w["limitKind"], w["setBy"], w["low"]) == ("action", "requester", 55)


def test_a_goal_from_before_provenance_is_UNKNOWN_not_assumed():
    assert L.wire(_verdict([_cp([_goal()])]))["setBy"] == "unknown"


def test_goals_with_different_setters_are_mixed():
    cp = _cp([_goal(target_type="", target_operator=">=", target_quantity=55, limit_source="requester",
                    target_range_low=None, target_range_high=None),
              _goal(target_type="", target_operator="<=", target_quantity=95, limit_source="plan_recommended",
                    target_range_low=None, target_range_high=None)])
    assert L.wire(_verdict([cp]))["setBy"] == "mixed"


def test_the_template_range_is_RECOMMENDED_NORMAL_not_an_action_limit():
    w = L.wire(_verdict([_cp()]))
    assert (w["limitKind"], w["setBy"], w["low"]) == ("recommended_normal", "plan_recommended", 60)


def test_an_empty_verdict_still_has_the_keys():
    w = L.wire(L.Verdict("no_limit"))
    assert w["limitKind"] is None and w["setBy"] is None
