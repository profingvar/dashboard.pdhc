"""Tests for `app.services.request_client` — the read-only request.pdhc client.

MOCKED AT THE HTTP BOUNDARY ONLY. Every test here patches
`app.services.request_client.requests.get` and nothing else — never a method
of the client under test. Patching `fetch_care_plans` to return a fixture
would assert that `build_plan_summary` can read a dict I wrote myself, which
is a test of the fixture; patching the socket asserts that the client can
read what request.pdhc actually sends. The bodies below are copied from
request.pdhc's own serialisers (`CarePlan.to_dict`,
`care_plans.care_plan_context`, `ServiceRequest.to_dict`,
`match_service.list_matches_for_request`) rather than invented.

THE TWO TESTS THAT MATTER MOST, and why they are written the way they are:

* `TestNullPeriodStartIsPassedThrough` — `CarePlan.period_start` is nullable
  in request.pdhc's own schema (`care_plan_models.py:54`) and it is the
  origin for every "T+ from start" time in the dashboard. A fallback to
  `created_at` would look harmless and would move every event on a clinical
  timeline. So the assertion is not only `is None`; it also names the three
  values a well-meaning fallback would reach for and asserts the field is
  none of them. An `is None` alone passes against a `period_end` fallback
  whenever `period_end` happens to be null too.

* `TestOverdueComesFromStatusAlone` — "result" on this platform cannot mean
  "an observation exists": `care_plan_guid` is populated on 0 of 1 949
  observations on cdr1, so an observation-keyed rule marks every item
  overdue. The tests therefore pin the rule to the status, including the
  case where a long-past schedule must NOT win over a terminal status.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
import requests as requests_lib
import sqlalchemy

from app import create_app
from app.models import db
from app.services import request_client as rc


# ---------------------------------------------------------------------------
# Fixtures / harness
# ---------------------------------------------------------------------------

def _app(**over):
    cfg = {
        "TESTING": True,
        "DATABASE_URL": "sqlite:///:memory:",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {
            "connect_args": {"check_same_thread": False},
            "poolclass": sqlalchemy.pool.StaticPool,
        },
        "AUTH_MODE": "off",
    }
    cfg.update(over)
    app = create_app(cfg)
    with app.app_context():
        db.create_all()
    return app


@pytest.fixture(autouse=True)
def _no_ambient_request_url(monkeypatch):
    """Clear `REQUEST_BASE_URL` from the environment for every test here.

    Resolution is config-then-environment, so a developer with the variable
    exported would see the "unset" tests silently assert the opposite of
    what they read — the trap `test_sparr_unconfigured.py` documents for
    `IPS_BASE_URL`.
    """
    monkeypatch.delenv(rc.ENV_BASE_URL, raising=False)


BASE = "https://request.example"
PATIENT = "11111111-1111-4111-8111-111111111111"
PLAN = "22222222-2222-4222-8222-222222222222"
PLANDEF = "33333333-3333-4333-8333-333333333333"
SR = "44444444-4444-4444-8444-444444444444"
TX_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
TX_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1"
ACT = "cccccccc-cccc-4ccc-8ccc-ccccccccccc1"

NOW = datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc)
PAST = (NOW - timedelta(days=3)).isoformat()
FUTURE = (NOW + timedelta(days=3)).isoformat()


class _FakeResp:
    """Minimal `requests.Response` stand-in — the house pattern from
    `test_federation.py`."""

    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, _NotJson):
            raise ValueError("no json")
        return self._body

    @property
    def text(self):
        return ""


class _NotJson:
    pass


def _router(routes, *, calls=None):
    """Build a `requests.get` side effect that dispatches on URL suffix.

    `routes` maps a path suffix to `(status, body)` or to a callable raising
    a `requests` exception. An unrouted URL is an explicit test failure
    rather than a silent 404, so a typo in a path shows up as a typo.
    """
    def _get(url, params=None, headers=None, timeout=None):
        if calls is not None:
            calls.append({"url": url, "params": params, "headers": headers})
        for suffix, outcome in routes.items():
            if url.endswith(suffix):
                if callable(outcome):
                    return outcome()
                return _FakeResp(*outcome)
        raise AssertionError(f"unrouted URL in test: {url}")
    return _get


# -- bodies, copied from request.pdhc's serialisers -------------------------

def _care_plan(period_start=None, **over):
    body = {
        "guid": PLAN,
        "care_plan_guid": PLAN,          # canonical alias per #294
        "patient_guid": PATIENT,
        "plan_definition_guid": PLANDEF,
        "status": "active",
        "intent": "plan",
        "title": "Diabetes uppföljning",
        "description": None,
        "period_start": period_start,
        "period_end": "2027-03-01T00:00:00+00:00",
        "plan_definition_snapshot": {
            "title": "Diabetes typ 2 — uppföljning",
            "name": "diabetes-t2-followup",
            "version": "2.1.0",
        },
        "goals": [],
        "care_team_user_guids": [],
        "created_by_user_guid": "u-1",
        "created_at": "2026-01-15T08:30:00+00:00",
        "updated_at": "2026-02-20T09:00:00+00:00",
    }
    body.update(over)
    return body


def _context(transactions=None):
    """`/careplans/<guid>/context` — note the absent status and absent date:
    the flattened transaction carries neither, which is exactly why item
    state has to come from a ServiceRequest."""
    return {
        "care_plan_guid": PLAN,
        "patient_guid": PATIENT,
        "plan_definition_guid": PLANDEF,
        "status": "active",
        "intent": "plan",
        "period_start": None,
        "period_end": None,
        "goals": [],
        "transactions": transactions if transactions is not None else [{
            "activity_guid": ACT,
            "transaction_guid": TX_A,
            "concept_guid": "concept-hba1c",
            "concept_name": "HbA1c",
            "range_min": 4.0,
            "range_max": 6.0,
            "requirement_type": "required",
        }],
    }


def _service_request(status="active", tx_guids=(TX_A,), period_start=PAST,
                     guid=SR, **over):
    body = {
        "guid": guid,
        "status": status,
        "intent": "order",
        "priority": "routine",
        "patient_guid": PATIENT,
        "patient_excerpt": None,
        "plan_definition_guid": PLANDEF,
        "plan_definition_snapshot": {
            "activities": [{
                "guid": ACT,
                "transactions": [{"guid": g, "concept_guid": f"c-{g[:4]}"}
                                 for g in tx_guids],
            }],
        },
        "fhir_resource": None,
        "contract_guid": "contract-1",
        "requester_user_guid": "u-1",
        "requester_user_name": "Dr A",
        "requester_org_guid": "org-1",
        "patient_org_guid": "org-1",
        "requester_org_name": "Vårdcentral",
        "requesting_org_guid": "org-1",
        "requesting_org_name": "Vårdcentral",
        "notes": None,
        "period_start": period_start,
        "period_end": None,
        "created_at": "2026-01-15T08:30:00+00:00",
        "updated_at": "2026-02-20T09:00:00+00:00",
        "forms": [],
        # NOTE: no `care_plan_guid`. `ServiceRequest.to_dict()` omits the
        # column, which is why this client joins on transaction guids.
    }
    body.update(over)
    return body


def _matches(status="completed", response_at="2026-02-19T10:00:00+00:00"):
    return {
        "service_request_guid": SR,
        "matches": [{
            "guid": "m-1",
            "service_request_guid": SR,
            "contract_guid": "contract-1",
            "provider_org_guid": "org-9",
            "provider_name": "Labb AB",
            "match_type": "push",
            "status": status,
            "sent_at": "2026-02-18T10:00:00+00:00",
            "response_at": response_at,
            "response_payload": None,
            "created_at": "2026-02-18T09:00:00+00:00",
            "updated_at": "2026-02-19T10:00:00+00:00",
        }],
    }


def _routes(care_plans, context, srs, matches=None):
    return {
        "/api/v1/careplans/%s/context" % PLAN: (200, context),
        "/api/v1/careplans": (200, {"total": len(care_plans),
                                    "careplans": care_plans}),
        "/matches": (200, matches if matches is not None else
                     {"service_request_guid": SR, "matches": []}),
        "/api/v1/ServiceRequest": (200, {"items": srs, "total": len(srs),
                                         "page": 1, "per_page": 200}),
    }


# ---------------------------------------------------------------------------
# Configuration — "unset reads as unset and refuses BY NAME"
# ---------------------------------------------------------------------------

class TestNotConfigured:

    def test_is_configured_false_when_unset(self):
        with _app().app_context():
            assert rc.is_configured() is False

    def test_is_configured_false_for_whitespace(self):
        # An unset variable arrives as "" and a half-filled .env line as
        # whitespace. Neither is a URL.
        with _app(REQUEST_BASE_URL="   ").app_context():
            assert rc.is_configured() is False

    def test_is_configured_does_not_raise_outside_an_app_context(self):
        assert rc.is_configured() is False

    def test_an_environment_only_url_is_askable(self, monkeypatch):
        # `create_app` loads CDR1/IPS/GATEWAY/SSO base URLs into the config
        # and NOT this one, so the environment path is the one production
        # uses. If only the config were read this would answer False while
        # the client worked — the #827 shape.
        monkeypatch.setenv(rc.ENV_BASE_URL, BASE)
        with _app().app_context():
            assert rc.is_configured() is True

    def test_there_is_no_fabricated_default_base_url(self):
        # xlate's module-level `DEFAULT_BASE_URL = "http://127.0.0.1:9017"`
        # made an unset variable look configured, so "nobody wired this up"
        # and "it blipped" became the same symptom (#833). Guard its absence.
        assert not hasattr(rc, "DEFAULT_BASE_URL")
        with _app().app_context():
            assert rc.resolve_base_url() == ""
            assert rc.RequestClient().base_url == ""

    def test_every_read_raises_RequestNotConfigured_naming_the_variable(self):
        app = _app()
        client = rc.RequestClient(base_url="")
        reads = [
            lambda: client.fetch_care_plans(PATIENT),
            lambda: client.fetch_care_plan(PLAN),
            lambda: client.fetch_care_plan_context(PLAN),
            lambda: client.fetch_service_request(SR),
            lambda: client.fetch_service_requests(),
            lambda: client.fetch_service_request_matches(SR),
            lambda: client.plan_summaries(PATIENT),
        ]
        with app.test_request_context():
            for read in reads:
                with pytest.raises(rc.RequestNotConfigured) as e:
                    read()
                assert rc.ENV_BASE_URL in str(e.value), (
                    "the refusal must name the key an operator has to set")

    def test_not_configured_does_not_open_a_socket(self):
        app = _app()
        with app.test_request_context():
            with patch("app.services.request_client.requests.get") as g:
                with pytest.raises(rc.RequestNotConfigured):
                    rc.RequestClient(base_url="").fetch_care_plans(PATIENT)
            assert g.call_count == 0

    def test_not_configured_is_NOT_the_same_type_as_unreachable(self):
        # Two types, not one flag: the operator action differs (set a
        # variable vs. find out why a service did not answer).
        assert not issubclass(rc.RequestNotConfigured, rc.RequestUnreachable)
        assert not issubclass(rc.RequestUnreachable, rc.RequestNotConfigured)
        assert issubclass(rc.RequestNotConfigured, rc.RequestClientError)
        assert issubclass(rc.RequestUnreachable, rc.RequestClientError)


# ---------------------------------------------------------------------------
# Unreachable — and an empty list never means failure
# ---------------------------------------------------------------------------

class TestUnreachable:

    def test_a_network_error_raises_RequestUnreachable(self):
        app = _app()

        def _boom(url, params=None, headers=None, timeout=None):
            raise requests_lib.ConnectionError("connection refused")

        with app.test_request_context():
            with patch("app.services.request_client.requests.get",
                       side_effect=_boom):
                with pytest.raises(rc.RequestUnreachable) as e:
                    rc.RequestClient(base_url=BASE).fetch_care_plans(PATIENT)
        assert "network" in str(e.value)

    def test_a_timeout_raises_RequestUnreachable(self):
        app = _app()

        def _slow(url, params=None, headers=None, timeout=None):
            raise requests_lib.Timeout("too slow")

        with app.test_request_context():
            with patch("app.services.request_client.requests.get",
                       side_effect=_slow):
                with pytest.raises(rc.RequestUnreachable):
                    rc.RequestClient(base_url=BASE).fetch_care_plans(PATIENT)

    @pytest.mark.parametrize("status", [401, 403, 500, 502, 503])
    def test_a_non_2xx_raises_rather_than_returning_empty(self, status):
        """An empty list must mean "request.pdhc answered and there is
        nothing". A 401 is an answer, but not an answer ABOUT DATA — so it
        must not arrive as emptiness."""
        app = _app()
        with app.test_request_context():
            with patch("app.services.request_client.requests.get",
                       side_effect=_router({"/api/v1/careplans": (status, {})})):
                with pytest.raises(rc.RequestUnreachable) as e:
                    rc.RequestClient(base_url=BASE).fetch_care_plans(PATIENT)
        assert e.value.status_code == status

    def test_a_non_json_body_raises(self):
        app = _app()
        with app.test_request_context():
            with patch("app.services.request_client.requests.get",
                       side_effect=_router(
                           {"/api/v1/careplans": (200, _NotJson())})):
                with pytest.raises(rc.RequestUnreachable):
                    rc.RequestClient(base_url=BASE).fetch_care_plans(PATIENT)

    def test_a_404_on_a_LIST_path_raises_and_is_not_emptiness(self):
        """A list route does not 404 in request.pdhc; a 404 there is a route
        that does not exist — a prefix or proxy mistake. Reading it as "no
        care plans" would publish a wrong clinical answer."""
        app = _app()
        with app.test_request_context():
            with patch("app.services.request_client.requests.get",
                       side_effect=_router({"/api/v1/careplans": (404, {})})):
                with pytest.raises(rc.RequestUnreachable):
                    rc.RequestClient(base_url=BASE).fetch_care_plans(PATIENT)

    def test_an_empty_list_from_a_200_is_a_real_empty_list(self):
        app = _app()
        with app.test_request_context():
            with patch("app.services.request_client.requests.get",
                       side_effect=_router({"/api/v1/careplans":
                                            (200, {"total": 0,
                                                   "careplans": []})})):
                out = rc.RequestClient(base_url=BASE).fetch_care_plans(PATIENT)
        assert out == []

    def test_plan_summaries_raises_rather_than_returning_an_empty_tuple(self):
        app = _app()

        def _boom(url, params=None, headers=None, timeout=None):
            raise requests_lib.ConnectionError("down")

        with app.test_request_context():
            with patch("app.services.request_client.requests.get",
                       side_effect=_boom):
                with pytest.raises(rc.RequestUnreachable):
                    rc.RequestClient(base_url=BASE).plan_summaries(PATIENT)


# ---------------------------------------------------------------------------
# Auth — the measured door, and not an invented one
# ---------------------------------------------------------------------------

class TestHeaders:

    def test_it_sends_X_API_Key_and_never_X_Service_Key(self):
        """request.pdhc's `requires_auth` tries X-Provider-Token, then
        X-API-Key, then a session cookie. It never reads X-Service-Key —
        `requires_service_key` guards only `/internal/*` against
        INTERNAL_SERVICE_KEY, which dashboard does not hold. And the session
        branch reads `session['sso_token']`, not an Authorization header, so
        a Bearer from here would authenticate nothing."""
        app = _app()
        calls = []
        with app.test_request_context():
            with patch("app.services.request_client.requests.get",
                       side_effect=_router({"/api/v1/careplans":
                                            (200, {"careplans": []})},
                                           calls=calls)):
                rc.RequestClient(base_url=BASE, token="tok-abc"
                                 ).fetch_care_plans(PATIENT)
        headers = calls[0]["headers"]
        assert headers["X-API-Key"] == "tok-abc"
        assert "X-Service-Key" not in headers
        assert "Authorization" not in headers

    def test_it_forwards_the_operator_session_id(self):
        app = _app()
        calls = []
        with app.test_request_context(
                headers={"X-Operator-Session-Id": "sid-123"}):
            with patch("app.services.request_client.requests.get",
                       side_effect=_router({"/api/v1/careplans":
                                            (200, {"careplans": []})},
                                           calls=calls)):
                rc.RequestClient(base_url=BASE).fetch_care_plans(PATIENT)
        assert calls[0]["headers"]["X-Operator-Session-Id"] == "sid-123"


# ---------------------------------------------------------------------------
# period_start present
# ---------------------------------------------------------------------------

class TestPeriodStartPresent:

    def test_a_plan_with_a_period_start_carries_it_verbatim(self):
        app = _app()
        start = "2026-02-01T00:00:00+00:00"
        routes = _routes([_care_plan(period_start=start)], _context(),
                         [_service_request()])
        with app.test_request_context():
            with patch("app.services.request_client.requests.get",
                       side_effect=_router(routes)):
                plans = rc.RequestClient(base_url=BASE).plan_summaries(
                    PATIENT, now=NOW)
        assert len(plans) == 1
        p = plans[0]
        assert p.period_start == start, "the string must not be reformatted"
        assert p.period_end == "2027-03-01T00:00:00+00:00"
        assert p.care_plan_guid == PLAN
        assert p.care_plan_title == "Diabetes uppföljning"
        assert p.status == "active"
        assert p.plan_definition_name == "Diabetes typ 2 — uppföljning"
        assert p.plan_definition_version == "2.1.0"


# ---------------------------------------------------------------------------
# period_start NULL — the falsification guard
# ---------------------------------------------------------------------------

class TestNullPeriodStartIsPassedThrough:
    """`CarePlan.period_start` is `nullable=True` in request.pdhc
    (`care_plan_models.py:54`). It is the origin for every "T+ from start"
    time in the dashboard, so a substituted value silently moves every event
    on a clinical timeline — which is why this is a test and not a comment."""

    def _plans(self, app):
        routes = _routes([_care_plan(period_start=None)], _context(),
                         [_service_request()])
        with app.test_request_context():
            with patch("app.services.request_client.requests.get",
                       side_effect=_router(routes)):
                return rc.RequestClient(base_url=BASE).plan_summaries(
                    PATIENT, now=NOW)

    def test_None_arrives_as_None(self):
        plans = self._plans(_app())
        assert plans[0].period_start is None

    def test_nothing_is_substituted_for_it(self):
        """`is None` alone is a weak assertion: it also passes against a
        `period_end` fallback whenever `period_end` is null too. Name the
        values a well-meaning fallback would reach for and exclude each."""
        plans = self._plans(_app())
        cp = _care_plan(period_start=None)
        forbidden = {
            "created_at": cp["created_at"],
            "updated_at": cp["updated_at"],
            "period_end": cp["period_end"],
        }
        for name, value in forbidden.items():
            assert plans[0].period_start != value, (
                f"period_start was filled in from {name} — that falsifies "
                "every T+ time under this plan")

    def test_the_REST_of_the_plan_still_loads(self):
        """The null must not be handled by dropping the plan: a plan with no
        origin is still a plan, and its items are still real."""
        plans = self._plans(_app())
        assert len(plans) == 1
        assert plans[0].care_plan_guid == PLAN
        assert len(plans[0].items) == 1

    def test_an_absent_key_is_also_None_not_an_error(self):
        # A rolling deploy or an older serialiser can omit the key entirely.
        cp = _care_plan()
        cp.pop("period_start")
        summary = rc.build_plan_summary(cp, _context(), [], now=NOW)
        assert summary.period_start is None


# ---------------------------------------------------------------------------
# The four states, from status alone
# ---------------------------------------------------------------------------

class TestOverdueComesFromStatusAlone:
    """`derive_item_state` is pure, so the rule can be read without the wire.

    The platform constraint it encodes: `care_plan_guid` is populated on 0 of
    1 949 observations on cdr1, so "has an observation" is NULL for every
    item and an observation-keyed overdue rule marks the entire plan overdue.
    """

    @pytest.mark.parametrize("status,scheduled,expected", [
        # terminal statuses — the schedule is irrelevant
        ("archived", PAST, "done"),
        ("archived", FUTURE, "done"),
        ("completed", PAST, "done"),
        ("revoked", PAST, "cancelled"),
        ("entered-in-error", PAST, "cancelled"),
        # live statuses — the schedule decides
        ("active", FUTURE, "planned"),
        ("draft", FUTURE, "planned"),
        ("active", PAST, "overdue"),
        ("draft", PAST, "overdue"),
        # no schedule -> "late" is undefined
        ("active", None, "planned"),
        # no ServiceRequest at all
        (None, None, "planned"),
    ])
    def test_the_mapping(self, status, scheduled, expected):
        assert rc.derive_item_state(status, scheduled, now=NOW) == expected

    def test_a_terminal_status_beats_a_long_past_schedule(self):
        """The ordering matters and is easy to get backwards: a finished item
        whose due date is in the past is DONE, not overdue."""
        ancient = "2020-01-01T00:00:00+00:00"
        assert rc.derive_item_state("archived", ancient, now=NOW) == "done"
        assert rc.derive_item_state("revoked", ancient, now=NOW) == "cancelled"

    def test_status_is_case_and_whitespace_insensitive(self):
        assert rc.derive_item_state("  ARCHIVED ", PAST, now=NOW) == "done"

    def test_an_unparseable_schedule_is_not_overdue(self):
        """A date we cannot read is not a date that has passed. Guessing
        either way would be a clinical claim; `planned` is the one that does
        not assert the patient missed something."""
        assert rc.derive_item_state("active", "not-a-date", now=NOW) == "planned"
        assert rc.derive_item_state("active", "", now=NOW) == "planned"

    def test_a_naive_schedule_still_compares(self):
        """`ServiceRequest.period_start` is a NAIVE `db.DateTime`, so it
        serialises with no offset while `CarePlan.period_start` carries one.
        A naive-vs-aware comparison would raise TypeError on live data."""
        assert rc.derive_item_state(
            "active", "2026-11-08T00:00:00", now=NOW) == "overdue"
        assert rc.derive_item_state(
            "active", "2026-11-14T00:00:00", now=NOW) == "planned"

    def test_the_rule_never_consults_an_observation(self):
        """A structural guard, not a text match: the function's signature
        takes a status and a time and nothing else, so there is no argument
        through which an observation could reach it."""
        import inspect
        params = list(inspect.signature(rc.derive_item_state).parameters)
        assert params == ["service_request_status", "scheduled_at", "now"]


class TestTheFourStatesEndToEnd:
    """The same four states through the HTTP boundary, so the mapping is
    reached by the code a route would actually run."""

    def _states(self, app, sr_status, scheduled):
        ctx = _context([
            {"activity_guid": ACT, "transaction_guid": TX_A,
             "concept_guid": "concept-hba1c", "concept_name": "HbA1c"},
        ])
        routes = _routes(
            [_care_plan(period_start="2026-02-01T00:00:00+00:00")], ctx,
            [_service_request(status=sr_status, period_start=scheduled)],
            matches=_matches(),
        )
        with app.test_request_context():
            with patch("app.services.request_client.requests.get",
                       side_effect=_router(routes)):
                plans = rc.RequestClient(base_url=BASE).plan_summaries(
                    PATIENT, now=NOW)
        return plans[0].items

    @pytest.mark.parametrize("sr_status,scheduled,expected", [
        ("archived", PAST, "done"),
        ("active", FUTURE, "planned"),
        ("active", PAST, "overdue"),
        ("revoked", PAST, "cancelled"),
    ])
    def test_state_over_the_wire(self, sr_status, scheduled, expected):
        items = self._states(_app(), sr_status, scheduled)
        assert len(items) == 1
        assert items[0].state == expected

    def test_an_item_carries_its_service_request_and_times(self):
        items = self._states(_app(), "archived", PAST)
        it = items[0]
        assert it.service_request_guid == SR
        assert it.transaction_guid == TX_A
        assert it.plan_activity_guid == ACT, "the VISIT (#820)"
        assert it.concept_guid == "concept-hba1c"
        assert it.title == "HbA1c"
        assert it.scheduled_at == PAST
        # `performed_at` is the provider's response_at on a finished contract
        # match — the nearest measured fact. Never `updated_at`.
        assert it.performed_at == "2026-02-19T10:00:00+00:00"

    def test_performed_at_is_None_when_no_match_finished(self):
        app = _app()
        ctx = _context()
        routes = _routes([_care_plan()], ctx, [_service_request()],
                         matches=_matches(status="pending",
                                          response_at=None))
        with app.test_request_context():
            with patch("app.services.request_client.requests.get",
                       side_effect=_router(routes)):
                plans = rc.RequestClient(base_url=BASE).plan_summaries(
                    PATIENT, now=NOW)
        assert plans[0].items[0].performed_at is None

    def test_performed_at_is_never_taken_from_updated_at(self):
        app = _app()
        routes = _routes([_care_plan()], _context(),
                         [_service_request(status="archived")],
                         matches={"service_request_guid": SR, "matches": []})
        with app.test_request_context():
            with patch("app.services.request_client.requests.get",
                       side_effect=_router(routes)):
                plans = rc.RequestClient(base_url=BASE).plan_summaries(
                    PATIENT, now=NOW)
        it = plans[0].items[0]
        assert it.state == "done"
        assert it.performed_at is None, (
            "a done item with no provider response has no performed time; "
            "updated_at dates the row's last edit, not the care")

    def test_an_item_with_no_service_request_is_planned_and_unlinked(self):
        app = _app()
        ctx = _context([
            {"activity_guid": ACT, "transaction_guid": TX_B,
             "concept_guid": "concept-bp", "concept_name": "Blodtryck"},
        ])
        # The only SR froze TX_A, so TX_B has none.
        routes = _routes([_care_plan()], ctx, [_service_request(tx_guids=(TX_A,))])
        with app.test_request_context():
            with patch("app.services.request_client.requests.get",
                       side_effect=_router(routes)):
                plans = rc.RequestClient(base_url=BASE).plan_summaries(
                    PATIENT, now=NOW)
        it = plans[0].items[0]
        assert it.service_request_guid is None
        assert it.scheduled_at is None
        assert it.state == "planned"


# ---------------------------------------------------------------------------
# One frozen ServiceRequest
# ---------------------------------------------------------------------------

class TestFrozenServiceRequestLookup:

    def test_it_returns_the_frozen_resource(self):
        app = _app()
        body = _service_request(status="archived")
        with app.test_request_context():
            with patch("app.services.request_client.requests.get",
                       side_effect=_router(
                           {f"/api/v1/ServiceRequest/{SR}": (200, body)})):
                sr = rc.RequestClient(base_url=BASE).fetch_service_request(SR)
        assert sr["guid"] == SR
        assert sr["status"] == "archived"
        assert sr["plan_definition_guid"] == PLANDEF
        # The frozen snapshot is the point of the lookup — it is the
        # provenance fallback, so it must survive the read intact.
        assert rc.service_request_transaction_guids(sr) == {TX_A}

    def test_it_hits_the_measured_path(self):
        app = _app()
        calls = []
        with app.test_request_context():
            with patch("app.services.request_client.requests.get",
                       side_effect=_router(
                           {f"/api/v1/ServiceRequest/{SR}":
                            (200, _service_request())}, calls=calls)):
                rc.RequestClient(base_url=BASE).fetch_service_request(SR)
        assert calls[0]["url"] == f"{BASE}/api/v1/ServiceRequest/{SR}"

    def test_a_404_is_an_ANSWER_and_returns_None(self):
        """404 here means "unknown guid" OR "spärr hides it"
        (`is_sr_visible`, #228) — deliberately indistinguishable upstream,
        because the row's existence is itself the disclosure. Either way it
        is an answer, not a failure, so it is None and not an exception."""
        app = _app()
        with app.test_request_context():
            with patch("app.services.request_client.requests.get",
                       side_effect=_router(
                           {f"/api/v1/ServiceRequest/{SR}":
                            (404, {"code": "not_found"})})):
                sr = rc.RequestClient(base_url=BASE).fetch_service_request(SR)
        assert sr is None

    def test_a_500_still_raises(self):
        app = _app()
        with app.test_request_context():
            with patch("app.services.request_client.requests.get",
                       side_effect=_router(
                           {f"/api/v1/ServiceRequest/{SR}": (500, {})})):
                with pytest.raises(rc.RequestUnreachable):
                    rc.RequestClient(base_url=BASE).fetch_service_request(SR)

    def test_the_list_filters_by_patient_CLIENT_side(self):
        """`list_all` accepts no `patient_guid`, so the filter is ours. The
        request must therefore NOT pretend to send one."""
        app = _app()
        calls = []
        other = _service_request(guid="other-sr",
                                 patient_guid="99999999-9999-4999-8999-999999999999")
        with app.test_request_context():
            with patch("app.services.request_client.requests.get",
                       side_effect=_router(
                           {"/api/v1/ServiceRequest":
                            (200, {"items": [_service_request(), other],
                                   "total": 2})}, calls=calls)):
                rows = rc.RequestClient(base_url=BASE).fetch_service_requests(
                    patient_guid=PATIENT)
        assert [r["guid"] for r in rows] == [SR]
        assert "patient_guid" not in (calls[0]["params"] or {})


# ---------------------------------------------------------------------------
# The join request.pdhc does not give us
# ---------------------------------------------------------------------------

class TestServiceRequestToCarePlanJoin:

    def test_care_plan_guid_is_absent_from_the_SR_body(self):
        """Pinned because the whole join strategy depends on it. If
        request.pdhc ever starts emitting `care_plan_guid` on an SR, this
        test is the note that a better edge became available."""
        assert "care_plan_guid" not in _service_request()

    def test_transaction_guids_come_out_of_the_frozen_snapshot(self):
        sr = _service_request(tx_guids=(TX_A, TX_B))
        assert rc.service_request_transaction_guids(sr) == {TX_A, TX_B}

    def test_a_snapshotless_SR_yields_no_transaction_guids(self):
        assert rc.service_request_transaction_guids(
            _service_request(plan_definition_snapshot=None)) == set()

    def test_an_SR_for_a_DIFFERENT_patient_is_not_linked(self):
        """The join is patient + transaction guid. A transaction guid collides
        across patients by design — it identifies the TEMPLATE row — so
        without the patient check one patient's SR would set another's item
        state."""
        other = _service_request(
            patient_guid="99999999-9999-4999-8999-999999999999",
            status="archived")
        summary = rc.build_plan_summary(_care_plan(), _context(), [other],
                                        now=NOW)
        assert summary.items[0].service_request_guid is None
        assert summary.items[0].state == "planned"

    def test_a_precise_transaction_match_beats_the_plan_wide_fallback(self):
        lossy = _service_request(guid="lossy-sr", status="revoked",
                                 plan_definition_snapshot={})
        precise = _service_request(guid="precise-sr", status="archived",
                                   tx_guids=(TX_A,))
        summary = rc.build_plan_summary(_care_plan(), _context(),
                                        [lossy, precise], now=NOW)
        assert summary.items[0].service_request_guid == "precise-sr"
        assert summary.items[0].state == "done"

    def test_a_lossy_snapshot_still_links_by_plan_definition(self):
        lossy = _service_request(guid="lossy-sr", status="archived",
                                 plan_definition_snapshot={})
        summary = rc.build_plan_summary(_care_plan(), _context(), [lossy],
                                        now=NOW)
        assert summary.items[0].service_request_guid == "lossy-sr"


# ---------------------------------------------------------------------------
# Shape tolerance
# ---------------------------------------------------------------------------

class TestEmptyAndMissingShapes:

    def test_a_context_with_no_transactions_gives_a_plan_with_no_items(self):
        summary = rc.build_plan_summary(_care_plan(), _context([]), [], now=NOW)
        assert summary.items == ()
        assert summary.care_plan_guid == PLAN

    def test_a_missing_context_gives_a_plan_with_no_items(self):
        # `/context` 404s like any item path; the plan itself is still real.
        summary = rc.build_plan_summary(_care_plan(), None, [], now=NOW)
        assert summary.items == ()

    def test_an_absent_snapshot_leaves_plandef_name_and_version_None(self):
        summary = rc.build_plan_summary(
            _care_plan(plan_definition_snapshot=None), _context(), [], now=NOW)
        assert summary.plan_definition_name is None
        assert summary.plan_definition_version is None

    def test_name_falls_back_to_the_snapshot_name_when_there_is_no_title(self):
        # request.pdhc's own read is `title or name`
        # (`routes/service_requests.py:454`).
        summary = rc.build_plan_summary(
            _care_plan(plan_definition_snapshot={"name": "only-a-name"}),
            _context(), [], now=NOW)
        assert summary.plan_definition_name == "only-a-name"

    def test_a_patient_with_no_care_plans_gives_an_empty_tuple(self):
        app = _app()
        with app.test_request_context():
            # No CarePlans AND no ServiceRequests. Since 2026-11-12 an empty
            # careplan list no longer ends the read: production has 0
            # CarePlans and the orders carry the plans.
            with patch("app.services.request_client.requests.get",
                       side_effect=_router(_routes([], None, []))):
                plans = rc.RequestClient(base_url=BASE).plan_summaries(PATIENT)
        assert plans == ()

    def test_with_no_care_plans_EACH_ORDER_IS_A_PLAN(self):
        """Production, 2026-11-12: 0 CarePlans, 28 ServiceRequests. Before
        this, every patient's plan panel was empty."""
        app = _app()
        with app.test_request_context():
            with patch("app.services.request_client.requests.get",
                       side_effect=_router(_routes(
                           [], None, [_service_request(status="archived",
                                                       tx_guids=(TX_A, TX_B))]))):
                plans = rc.RequestClient(base_url=BASE).plan_summaries(PATIENT)
        assert len(plans) == 1
        (p,) = plans
        assert p.source == "service_request" and p.care_plan_guid is None
        assert p.service_request_guid == SR
        assert p.ordered_at == "2026-01-15T08:30:00+00:00"
        assert [i.transaction_guid for i in p.items] == [TX_A, TX_B]
        assert {i.state for i in p.items} == {"done"}
        assert {i.plan_activity_guid for i in p.items} == {ACT}

    def test_an_order_is_NOT_a_T_plus_origin(self):
        """`ordered_at` is created_at. It must never become period_start."""
        p = rc.build_order_summary(_service_request(period_start=None))
        assert p.period_start is None
        assert p.ordered_at is not None

    def test_an_order_with_no_date_is_planned_however_old(self):
        p = rc.build_order_summary(_service_request(period_start=None))
        assert {i.state for i in p.items} == {"planned"}

    def test_an_SR_a_care_plan_already_claims_is_not_listed_twice(self):
        app = _app()
        with app.test_request_context():
            with patch("app.services.request_client.requests.get",
                       side_effect=_router(_routes([_care_plan()], _context(),
                                                   [_service_request()]))):
                plans = rc.RequestClient(base_url=BASE).plan_summaries(PATIENT)
        assert [p.source for p in plans] == ["care_plan"]

    def test_include_performed_False_skips_the_matches_calls(self):
        app = _app()
        calls = []
        routes = _routes([_care_plan()], _context(), [_service_request()],
                         matches=_matches())
        with app.test_request_context():
            with patch("app.services.request_client.requests.get",
                       side_effect=_router(routes, calls=calls)):
                plans = rc.RequestClient(base_url=BASE).plan_summaries(
                    PATIENT, now=NOW, include_performed=False)
        assert not any("/matches" in c["url"] for c in calls)
        assert plans[0].items[0].performed_at is None
