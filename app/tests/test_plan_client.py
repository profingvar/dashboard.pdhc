"""Unit tests for `app.services.plan_client` — the read-only plan.pdhc client.

Mocked at the HTTP BOUNDARY (`requests.get`), never by patching the client's
own methods: patching `fetch_concept_index` to count calls would assert that
the test's own stub was called once, which is true however broken the client
is. The federation tests in this repo mock `requests.request` for the same
reason, and `feedback_integration_smoke_finds_what_units_cannot` is the
standing lesson — patch the boundary, not our own client.

Four things here are deliberately CONTROLS rather than coverage:

* `test_the_index_is_PAGED_past_the_200_cap` exists so the "one request"
  assertion cannot pass merely because paging is broken. One request must be
  one request because the registry fit, not because the client can only ever
  make one.
* `test_an_empty_registry_is_an_empty_map_and_NOT_an_error` and
  `test_an_empty_bundle_is_an_empty_list_not_a_404` pin the rule that an
  empty result means "plan answered and there is nothing".
* `test_not_configured_and_unreachable_are_DIFFERENT_types` asserts the thing
  a single boolean flag would have destroyed.
* `test_no_credential_is_invented_on_the_wire` asserts an ABSENCE, so it is
  paired with a positive control (the session header IS forwarded) — a test
  that only asserts absence passes against a client that sends no headers at
  all, or makes no request.
"""
from __future__ import annotations

import pytest
import requests
import sqlalchemy

from app import create_app
from app.dashboard import dto
from app.models import db
from app.services import plan_client as plan_mod


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

PLAN = "https://plan.pdhc.se"

G1 = "11111111-1111-4111-8111-111111111111"
G2 = "22222222-2222-4222-8222-222222222222"
G3 = "33333333-3333-4333-8333-333333333333"
UNKNOWN = "99999999-9999-4999-8999-999999999999"


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
def _no_ambient_plan_url(monkeypatch):
    """Clear `PLAN_BASE_URL` from the environment for every test here.

    Resolution is config-then-environment, so a developer with the variable
    exported would see every "unset" test below assert the opposite of what
    it reads (the trap `test_sparr_unconfigured` documents for ips).
    """
    monkeypatch.delenv("PLAN_BASE_URL", raising=False)


@pytest.fixture(autouse=True)
def _reset_index():
    plan_mod.invalidate_concepts()
    yield
    plan_mod.invalidate_concepts()


class _FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Recorder:
    """A `requests.get` stand-in that records every call it is handed."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[dict] = []

    def __call__(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "headers": headers or {},
                           "params": params or {}})
        return self.handler(url, params or {})

    @property
    def count(self) -> int:
        return len(self.calls)


def _concept_row(guid, name, display=None):
    """One row in the shape plan.pdhc's `Concept.to_dict()` emits."""
    return {
        "guid": guid,
        "concept_guid": guid,          # #294 D3 emits both
        "concept_name": name,
        "concept_display_text": display,
        "unit": None,
        "response_type": None,
    }


def _concept_page(rows, total=None, page=1):
    return {"items": rows, "total": total if total is not None else len(rows),
            "page": page, "per_page": plan_mod.CONCEPT_PAGE_SIZE}


def _plandef(fhir_id="hba1c-protocol", name="HbA1cProtocol",
             version="2.1.0", title="HbA1c follow-up", status="active"):
    return {
        "resourceType": "PlanDefinition",
        "id": fhir_id,
        "meta": {"versionId": "3"},
        "identifier": [{"system": "https://pdhc.se/plan-definitions",
                        "value": name}],
        "url": f"https://pdhc.se/fhir/PlanDefinition/{fhir_id}",
        "version": version,
        "name": name,
        "title": title,
        "status": status,
    }


# ---------------------------------------------------------------------------
# Configuration — an unset variable must refuse BY NAME
# ---------------------------------------------------------------------------

class TestNotConfigured:
    """`xlate.pdhc`'s client had `DEFAULT_BASE_URL = "http://127.0.0.1:9017"`,
    so an unset variable produced a configured-looking URL and "nobody wired
    this up" was reported as a retryable 503 forever (#833). There is no
    module-level default here, and these tests are what keeps it that way."""

    def test_an_unset_PLAN_BASE_URL_raises_PlanNotConfigured_BY_NAME(self):
        with _app().app_context():
            with pytest.raises(plan_mod.PlanNotConfigured) as e:
                plan_mod.PlanClient().get_plan_definition("hba1c-protocol")
            assert "PLAN_BASE_URL" in str(e.value), (
                "the error must name the variable to set, not merely say "
                "'not configured'")

    def test_a_concept_batch_also_refuses_rather_than_returning_empty(self):
        with _app().app_context():
            with pytest.raises(plan_mod.PlanNotConfigured):
                plan_mod.concept_display_names([G1, G2])

    def test_it_makes_NO_request_when_it_has_nowhere_to_send_one(
            self, monkeypatch):
        rec = _Recorder(lambda url, params: _FakeResp(200, {}))
        monkeypatch.setattr(plan_mod.requests, "get", rec)
        with _app().app_context():
            with pytest.raises(plan_mod.PlanNotConfigured):
                plan_mod.concept_display_names([G1])
        assert rec.count == 0

    def test_a_WHITESPACE_url_is_not_a_url(self):
        # An unset variable arrives as "", a half-filled .env line as
        # whitespace. Neither is somewhere to send a GET.
        with _app(PLAN_BASE_URL="   ").app_context():
            assert plan_mod.is_configured() is False
            with pytest.raises(plan_mod.PlanNotConfigured):
                plan_mod.PlanClient().get_concept(G1)

    def test_is_configured_is_askable_without_provoking_an_exception(self):
        with _app().app_context():
            assert plan_mod.is_configured() is False
        with _app(PLAN_BASE_URL=PLAN).app_context():
            assert plan_mod.is_configured() is True

    def test_is_configured_does_not_raise_OUTSIDE_an_app_context(self):
        assert plan_mod.is_configured() is False

    def test_an_ENVIRONMENT_only_url_is_enough(self, monkeypatch):
        # Measured 2026-11-11: `create_app` loads CDR1_BASE_URL, IPS_BASE_URL,
        # GATEWAY_BASE_URL and SSO_BASE_URL into config and NOT
        # PLAN_BASE_URL — and this task must not edit `app/__init__.py`. So
        # production's `env_file: .env` reaches this client only through the
        # environment leg, and that leg is load-bearing, not a nicety.
        monkeypatch.setenv("PLAN_BASE_URL", PLAN)
        with _app().app_context():
            assert plan_mod.is_configured() is True
            assert plan_mod.build_client().base_url == PLAN

    def test_a_config_override_still_WINS_over_the_environment(
            self, monkeypatch):
        monkeypatch.setenv("PLAN_BASE_URL", PLAN)
        with _app(PLAN_BASE_URL="https://plan.example").app_context():
            assert plan_mod.resolve_base_url() == "https://plan.example"

    def test_the_CLIENT_and_the_QUESTION_resolve_the_SAME_url(
            self, monkeypatch):
        # Two resolution paths for one deployment fact was #827's defect.
        monkeypatch.setenv("PLAN_BASE_URL", PLAN + "/")
        with _app().app_context():
            assert plan_mod.is_configured() is True
            assert plan_mod.build_client().base_url == PLAN


# ---------------------------------------------------------------------------
# PlanDefinition
# ---------------------------------------------------------------------------

class TestPlanDefinitionRead:

    def test_a_successful_read_yields_name_and_version(self, monkeypatch):
        rec = _Recorder(lambda url, params: _FakeResp(200, _plandef()))
        monkeypatch.setattr(plan_mod.requests, "get", rec)
        with _app(PLAN_BASE_URL=PLAN).app_context():
            ref = plan_mod.PlanClient().get_plan_definition("hba1c-protocol")
        assert ref is not None
        assert ref.fhir_id == "hba1c-protocol"
        assert ref.name == "HbA1cProtocol"
        assert ref.version == "2.1.0"
        assert ref.title == "HbA1c follow-up"
        assert ref.status == "active"
        assert rec.count == 1
        assert rec.calls[0]["url"] == (
            f"{PLAN}/api/v1/PlanDefinition/hba1c-protocol")

    def test_those_two_fields_feed_dto_PlanSummary(self, monkeypatch):
        """The only reason this client reads a PlanDefinition at all. If the
        key names drift from `dto.PlanSummary`, that is a silent empty label
        in the plan panel — which is why dto uses dataclasses."""
        monkeypatch.setattr(plan_mod.requests, "get",
                            _Recorder(lambda url, params:
                                      _FakeResp(200, _plandef())))
        with _app(PLAN_BASE_URL=PLAN).app_context():
            ref = plan_mod.PlanClient().get_plan_definition("hba1c-protocol")
        summary = dto.PlanSummary(
            care_plan_guid="cp-1", **ref.as_plan_summary_fields())
        assert summary.plan_definition_name == "HbA1cProtocol"
        assert summary.plan_definition_version == "2.1.0"

    def test_a_404_is_None_and_NOT_an_exception(self, monkeypatch):
        monkeypatch.setattr(
            plan_mod.requests, "get",
            _Recorder(lambda url, params:
                      _FakeResp(404, {"error": "Not found"})))
        with _app(PLAN_BASE_URL=PLAN).app_context():
            assert plan_mod.PlanClient().get_plan_definition("nope") is None

    def test_a_404_is_DISTINGUISHABLE_from_an_empty_search(self, monkeypatch):
        """Both are "nothing", and they are different nothings: a 404 on a
        read means that id does not exist, an empty Bundle means plan holds
        no PlanDefinitions at all. A client that collapsed them would make
        the panel unable to say which."""
        def handler(url, params):
            if url.endswith("/PlanDefinition"):
                return _FakeResp(200, {"resourceType": "Bundle",
                                       "type": "searchset", "total": 0,
                                       "entry": []})
            return _FakeResp(404, {"error": "Not found"})

        monkeypatch.setattr(plan_mod.requests, "get", _Recorder(handler))
        with _app(PLAN_BASE_URL=PLAN).app_context():
            c = plan_mod.PlanClient()
            assert c.get_plan_definition("nope") is None
            assert c.list_plan_definitions() == []

    def test_an_empty_bundle_is_an_empty_list_not_a_404(self, monkeypatch):
        monkeypatch.setattr(
            plan_mod.requests, "get",
            _Recorder(lambda url, params:
                      _FakeResp(200, {"resourceType": "Bundle",
                                      "type": "searchset", "total": 0})))
        with _app(PLAN_BASE_URL=PLAN).app_context():
            assert plan_mod.PlanClient().list_plan_definitions() == []

    def test_the_searchset_is_parsed_into_refs(self, monkeypatch):
        bundle = {
            "resourceType": "Bundle", "type": "searchset", "total": 2,
            "entry": [
                {"resource": _plandef("a", "Alpha", "1.0.0")},
                {"resource": _plandef("b", "Beta", "2.0.0")},
            ],
        }
        rec = _Recorder(lambda url, params: _FakeResp(200, bundle))
        monkeypatch.setattr(plan_mod.requests, "get", rec)
        with _app(PLAN_BASE_URL=PLAN).app_context():
            refs = plan_mod.PlanClient().list_plan_definitions(status="active")
        assert [(r.fhir_id, r.name, r.version) for r in refs] == [
            ("a", "Alpha", "1.0.0"), ("b", "Beta", "2.0.0")]
        # FHIR paging params, NOT /concepts' page/per_page — measured.
        assert rec.calls[0]["params"]["_count"] == "200"
        assert rec.calls[0]["params"]["status"] == "active"

    def test_a_malformed_bundle_entry_is_SKIPPED_not_fabricated(self):
        # Pure parse, no network: an entry without an id would otherwise
        # become a ref with an empty identity and a blank label in the panel.
        refs = plan_mod.parse_plan_definition_bundle({"entry": [
            {"resource": {"resourceType": "PlanDefinition"}},     # no id
            {"resource": {"resourceType": "Patient", "id": "p1"}},  # wrong
            {"resource": _plandef("c", "Gamma", "3.0.0")},
            "not-a-dict",
        ]})
        assert [r.fhir_id for r in refs] == ["c"]


# ---------------------------------------------------------------------------
# Concept names — the batch
# ---------------------------------------------------------------------------

class TestConceptNameBatch:
    """The events list resolves hundreds of names. A request per row is an
    explicit failure in the coding plan, so these tests count requests."""

    def test_many_guids_are_resolved_in_ONE_request(self, monkeypatch):
        rows = [_concept_row(G1, "hba1c", "HbA1c"),
                _concept_row(G2, "crp", "CRP"),
                _concept_row(G3, "weight", None)]
        rec = _Recorder(lambda url, params: _FakeResp(200, _concept_page(rows)))
        monkeypatch.setattr(plan_mod.requests, "get", rec)
        with _app(PLAN_BASE_URL=PLAN).app_context():
            names = plan_mod.concept_display_names([G1, G2, G3])
        assert rec.count == 1, (
            f"{rec.count} requests for 3 guids — an N+1 per row is the thing "
            "this client exists to avoid")
        assert names == {G1: "HbA1c", G2: "CRP", G3: "weight"}

    def test_three_hundred_guids_are_still_one_request_per_PAGE(
            self, monkeypatch):
        """The realistic events-list case: many more guids than pages. The
        request count must track the registry SIZE, never the question size."""
        rows = [_concept_row(f"g{i:04d}", f"c{i}") for i in range(150)]
        rec = _Recorder(lambda url, params: _FakeResp(200, _concept_page(rows)))
        monkeypatch.setattr(plan_mod.requests, "get", rec)
        asked = [f"g{i:04d}" for i in range(150)] * 2  # 300 lookups, dupes
        with _app(PLAN_BASE_URL=PLAN).app_context():
            names = plan_mod.concept_display_names(asked)
        assert rec.count == 1
        assert len(names) == 150

    def test_the_index_is_PAGED_past_the_200_cap(self, monkeypatch):
        """Control for the two tests above: "one request" must be because the
        registry fit in one page, not because the client cannot page. plan
        caps `per_page` at 200, so a single-page client silently drops every
        concept past the cap."""
        page1 = [_concept_row(f"a{i:04d}", f"a{i}") for i in range(200)]
        page2 = [_concept_row(f"b{i:04d}", f"b{i}") for i in range(50)]

        def handler(url, params):
            return _FakeResp(200, _concept_page(
                page1 if params.get("page") == "1" else page2,
                total=250, page=int(params.get("page", 1))))

        rec = _Recorder(handler)
        monkeypatch.setattr(plan_mod.requests, "get", rec)
        with _app(PLAN_BASE_URL=PLAN).app_context():
            names = plan_mod.concept_display_names(["a0000", "b0049"])
        assert rec.count == 2, "it must follow `total` past the server cap"
        assert names == {"a0000": "a0", "b0049": "b49"}
        assert rec.calls[0]["params"]["per_page"] == "200"

    def test_a_second_batch_is_served_from_the_CACHE(self, monkeypatch):
        rows = [_concept_row(G1, "hba1c", "HbA1c")]
        rec = _Recorder(lambda url, params: _FakeResp(200, _concept_page(rows)))
        monkeypatch.setattr(plan_mod.requests, "get", rec)
        with _app(PLAN_BASE_URL=PLAN).app_context():
            plan_mod.concept_display_names([G1])
            plan_mod.concept_display_names([G1])
            plan_mod.concept_display_name(G1)
        assert rec.count == 1

    def test_a_guid_plan_does_not_know_does_NOT_re_ask(self, monkeypatch):
        """A per-guid cache would miss on an unknown guid and re-ask for every
        unmapped row on the page — an N+1 wearing a cache. The whole index is
        cached, so an unknown guid is answered from a fresh index."""
        rows = [_concept_row(G1, "hba1c", "HbA1c")]
        rec = _Recorder(lambda url, params: _FakeResp(200, _concept_page(rows)))
        monkeypatch.setattr(plan_mod.requests, "get", rec)
        with _app(PLAN_BASE_URL=PLAN).app_context():
            first = plan_mod.concept_display_names([G1, UNKNOWN])
            for _ in range(5):
                plan_mod.concept_display_names([UNKNOWN])
        assert rec.count == 1
        assert first == {G1: "HbA1c"}, (
            "an unknown guid must be OMITTED, not given a fabricated label")

    def test_an_empty_guid_list_asks_NOTHING(self, monkeypatch):
        rec = _Recorder(lambda url, params: _FakeResp(200, _concept_page([])))
        monkeypatch.setattr(plan_mod.requests, "get", rec)
        with _app(PLAN_BASE_URL=PLAN).app_context():
            assert plan_mod.concept_display_names([]) == {}
            assert plan_mod.concept_display_names([None, ""]) == {}
        assert rec.count == 0

    def test_an_empty_registry_is_an_empty_map_and_NOT_an_error(
            self, monkeypatch):
        """plan.pdhc answered; it holds no concepts. That is a result, and it
        must be distinguishable from the two failures below."""
        monkeypatch.setattr(
            plan_mod.requests, "get",
            _Recorder(lambda url, params:
                      _FakeResp(200, _concept_page([], total=0))))
        with _app(PLAN_BASE_URL=PLAN).app_context():
            assert plan_mod.concept_display_names([G1]) == {}

    def test_display_text_wins_over_name_and_name_is_the_FALLBACK(self):
        # Pure. `concept_display_text` is nullable, `concept_name` is not.
        assert plan_mod.display_name_of(
            _concept_row(G1, "hba1c", "HbA1c")) == "HbA1c"
        assert plan_mod.display_name_of(
            _concept_row(G1, "hba1c", None)) == "hba1c"
        assert plan_mod.display_name_of(
            _concept_row(G1, "hba1c", "   ")) == "hba1c"
        assert plan_mod.display_name_of({"guid": G1}) is None

    def test_a_row_carrying_only_concept_guid_is_still_indexed(
            self, monkeypatch):
        """#294 RFC D3 emits `guid` AND `concept_guid` and says `guid` will be
        dropped after a release cycle. Reading both means that drop is not a
        silent outage here."""
        row = {"concept_guid": G2, "concept_name": "crp"}
        monkeypatch.setattr(
            plan_mod.requests, "get",
            _Recorder(lambda url, params: _FakeResp(200, _concept_page([row]))))
        with _app(PLAN_BASE_URL=PLAN).app_context():
            assert plan_mod.concept_display_names([G2]) == {G2: "crp"}


class TestSingleConceptRead:

    def test_one_concept_is_read_by_guid(self, monkeypatch):
        body = _concept_row(G1, "hba1c", "HbA1c")
        rec = _Recorder(lambda url, params: _FakeResp(200, body))
        monkeypatch.setattr(plan_mod.requests, "get", rec)
        with _app(PLAN_BASE_URL=PLAN).app_context():
            got = plan_mod.PlanClient().get_concept(G1)
        assert got["concept_display_text"] == "HbA1c"
        assert rec.calls[0]["url"] == f"{PLAN}/api/v1/concepts/{G1}"

    def test_a_404_concept_is_None_not_an_empty_dict(self, monkeypatch):
        """`{}` is falsy and so would read as "a concept with no fields",
        which is not what a 404 said."""
        monkeypatch.setattr(
            plan_mod.requests, "get",
            _Recorder(lambda url, params:
                      _FakeResp(404, {"error": "Not found"})))
        with _app(PLAN_BASE_URL=PLAN).app_context():
            assert plan_mod.PlanClient().get_concept(UNKNOWN) is None


# ---------------------------------------------------------------------------
# Failure types
# ---------------------------------------------------------------------------

class TestUnreachable:

    def test_an_unreachable_host_raises_PlanUnreachable(self, monkeypatch):
        def boom(url, headers=None, params=None, timeout=None):
            raise requests.ConnectionError("name or service not known")

        monkeypatch.setattr(plan_mod.requests, "get", boom)
        with _app(PLAN_BASE_URL=PLAN).app_context():
            with pytest.raises(plan_mod.PlanUnreachable):
                plan_mod.PlanClient().get_plan_definition("hba1c-protocol")
            with pytest.raises(plan_mod.PlanUnreachable):
                plan_mod.concept_display_names([G1])

    def test_a_timeout_raises_PlanUnreachable(self, monkeypatch):
        def boom(url, headers=None, params=None, timeout=None):
            raise requests.Timeout("timed out")

        monkeypatch.setattr(plan_mod.requests, "get", boom)
        with _app(PLAN_BASE_URL=PLAN).app_context():
            with pytest.raises(plan_mod.PlanUnreachable):
                plan_mod.concept_display_names([G1])

    def test_a_500_raises_rather_than_returning_an_empty_map(
            self, monkeypatch):
        monkeypatch.setattr(
            plan_mod.requests, "get",
            _Recorder(lambda url, params: _FakeResp(500, {"error": "boom"})))
        with _app(PLAN_BASE_URL=PLAN).app_context():
            with pytest.raises(plan_mod.PlanUnreachable):
                plan_mod.concept_display_names([G1])

    def test_a_401_raises_too_so_a_future_gate_is_LOUD(self, monkeypatch):
        """These reads are unauthenticated by necessity (#931). If plan ever
        gates them, this client must say so rather than quietly render every
        row with no name."""
        monkeypatch.setattr(
            plan_mod.requests, "get",
            _Recorder(lambda url, params: _FakeResp(401, {"error": "auth"})))
        with _app(PLAN_BASE_URL=PLAN).app_context():
            with pytest.raises(plan_mod.PlanUnreachable) as e:
                plan_mod.concept_display_names([G1])
            assert "401" in str(e.value)

    def test_a_non_JSON_body_raises_rather_than_crashing_the_caller(
            self, monkeypatch):
        # An nginx error page is HTML, and `r.json()` raises ValueError.
        monkeypatch.setattr(
            plan_mod.requests, "get",
            _Recorder(lambda url, params: _FakeResp(200, ValueError("html"))))
        with _app(PLAN_BASE_URL=PLAN).app_context():
            with pytest.raises(plan_mod.PlanUnreachable):
                plan_mod.PlanClient().get_plan_definition("x")

    def test_not_configured_and_unreachable_are_DIFFERENT_types(
            self, monkeypatch):
        """The contract. One is an operator's missing `.env` line, the other
        is a service that is down, and a panel says different things about
        them. A single flag — or an empty list for both — is what #827 cost."""
        assert not issubclass(plan_mod.PlanNotConfigured,
                              plan_mod.PlanUnreachable)
        assert not issubclass(plan_mod.PlanUnreachable,
                              plan_mod.PlanNotConfigured)

        def boom(url, headers=None, params=None, timeout=None):
            raise requests.ConnectionError("down")

        monkeypatch.setattr(plan_mod.requests, "get", boom)
        with _app(PLAN_BASE_URL=PLAN).app_context():
            with pytest.raises(plan_mod.PlanUnreachable):
                plan_mod.concept_display_names([G1])
        plan_mod.invalidate_concepts()
        with _app().app_context():
            with pytest.raises(plan_mod.PlanNotConfigured):
                plan_mod.concept_display_names([G1])

    def test_a_failure_is_NOT_cached_as_an_empty_index(self, monkeypatch):
        """Caching a failed fetch would turn one blip into five minutes of
        nameless rows, and the second call would not even raise."""
        state = {"fail": True}

        def handler(url, params):
            if state["fail"]:
                raise requests.ConnectionError("down")
            return _FakeResp(200, _concept_page(
                [_concept_row(G1, "hba1c", "HbA1c")]))

        monkeypatch.setattr(plan_mod.requests, "get", _Recorder(handler))
        with _app(PLAN_BASE_URL=PLAN).app_context():
            with pytest.raises(plan_mod.PlanUnreachable):
                plan_mod.concept_display_names([G1])
            state["fail"] = False
            assert plan_mod.concept_display_names([G1]) == {G1: "HbA1c"}


# ---------------------------------------------------------------------------
# Headers — no credential is invented (#931)
# ---------------------------------------------------------------------------

class TestHeaders:

    def test_no_credential_is_invented_on_the_wire(self, monkeypatch):
        """plan.pdhc has no machine-auth door that admits this service: its
        `KNOWN_SERVICES` holds only `sim.pdhc` and that key is unset (#931).
        So the read is unauthenticated BY NECESSITY, and a header that merely
        LOOKS like a credential is worse than none — request.pdhc's
        `_headers()` is already read that way by mistake."""
        rec = _Recorder(lambda url, params:
                        _FakeResp(200, _concept_page([])))
        monkeypatch.setattr(plan_mod.requests, "get", rec)
        with _app(PLAN_BASE_URL=PLAN).app_context():
            plan_mod.concept_display_names([G1])
        sent = rec.calls[0]["headers"]
        for forbidden in ("Authorization", "X-Service-Key", "X-API-Key",
                          "X-Source-Service", "X-SSO-Client-Secret"):
            assert forbidden not in sent, (
                f"{forbidden} was sent; plan has no door for it (#931)")
        # Positive control: the request WAS made and DID carry headers, so
        # the absences above are evidence rather than an empty dict.
        assert sent.get("Accept") == "application/json"

    def test_the_operator_session_IS_forwarded(self, monkeypatch):
        """X2 / #408 — session propagation for PDL kontroller. This is the
        header the client does send, and it authenticates nothing."""
        rec = _Recorder(lambda url, params:
                        _FakeResp(200, _concept_page([])))
        monkeypatch.setattr(plan_mod.requests, "get", rec)
        app = _app(PLAN_BASE_URL=PLAN)
        with app.test_request_context(
                "/", headers={"X-Operator-Session-Id": "sid-abc"}):
            plan_mod.concept_display_names([G1])
        assert rec.calls[0]["headers"].get("X-Operator-Session-Id") == "sid-abc"

    def test_a_missing_session_sends_NO_empty_header(self, monkeypatch):
        rec = _Recorder(lambda url, params:
                        _FakeResp(200, _concept_page([])))
        monkeypatch.setattr(plan_mod.requests, "get", rec)
        with _app(PLAN_BASE_URL=PLAN).app_context():
            plan_mod.concept_display_names([G1])
        assert "X-Operator-Session-Id" not in rec.calls[0]["headers"]
