"""Tests for `app.services.icd11_client` — the read-only icd11.pdhc client.

MOCKED AT THE HTTP BOUNDARY ONLY. Every test here patches
`app.services.icd11_client.requests.get` and nothing else — never a method of
the client under test. Patching `fetch_diagnosis_summary` to return a fixture
would assert that the parser can read a dict I wrote myself, which is a test
of the fixture; patching the socket asserts that the client can read what
icd11.pdhc actually sends. The bodies below are copied from icd11's own
serialisers (`app/services/fhir.py::problem_list_bundle` /
`_state_as_condition` / `condition`) and from the route that stamps the meta
(`app/api/entry.py::diagnosis_summary`), not invented.

THE TESTS THAT MATTER MOST, and why they are written as they are:

* `TestTheTwoEmptiesAreDifferentAnswers` — an empty problem list means either
  "this patient has no diagnoses" or "this patient has diagnoses you are not
  scoped to see", and returning the same thing for both sent a clinician
  hunting a bug that was not there on 2026-10-23. So the assertions are not
  "the scope note is carried"; they are that the two empty results are
  **unequal objects**, that the scoped one is NOT `is_definitively_empty`,
  and that the note arrives character for character. An `== []` test on the
  diagnoses passes happily while the distinction is destroyed.

* `TestAnEmptyBundleIsNotAnEmptyEntryArray` — icd11 states absence with ONE
  sentinel Condition (`absent-unknown-uv-ips#no-problem-info`), because IPS
  requires absence to be asserted. A parser that treats it as a diagnosis
  invents one; a parser that writes it off as malformed loses the assertion.
  Both are checked.

* `TestTheScopeNoteSurvivesANonEmptyList` — the easiest half of the defect to
  reintroduce. A scoped read that DID return diagnoses is still partial, so
  the note must come through there too, not only when the list is empty.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
import requests as requests_lib
import sqlalchemy

from app import create_app
from app.dashboard.dto import Diagnosis, PatientCard
from app.models import db
from app.services import icd11_client as ic


# ---------------------------------------------------------------------------
# Harness
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
def _no_ambient_icd11_url(monkeypatch):
    """Clear `ICD11_BASE_URL` from the environment for every test here.

    Resolution is config-then-environment, so a developer with the variable
    exported would see the "unset" tests silently assert the opposite of what
    they read — the trap `test_sparr_unconfigured.py` documents for
    `IPS_BASE_URL`.
    """
    monkeypatch.delenv(ic.ENV_BASE_URL, raising=False)


class _FakeResp:
    """Minimal `requests.Response` stand-in — the house pattern from
    `test_federation.py` / `test_request_client.py`."""

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

    An unrouted URL is an explicit test failure rather than a silent 404, so
    a typo in a path shows up as a typo — which is what pins the measured
    `/api/v1` prefix below.
    """
    def _get(url, params=None, headers=None, timeout=None):
        if calls is not None:
            calls.append({"url": url, "params": params, "headers": headers,
                          "timeout": timeout})
        for suffix, outcome in routes.items():
            if url.endswith(suffix):
                if callable(outcome):
                    return outcome()
                return _FakeResp(*outcome)
        raise AssertionError(f"unrouted URL in test: {url}")
    return _get


BASE = "https://icd11.example"
PATIENT = "11111111-1111-4111-8111-111111111111"
EP_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
EP_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1"
EP_C = "cccccccc-cccc-4ccc-8ccc-ccccccccccc1"
ORG = "dddddddd-dddd-4ddd-8ddd-ddddddddddd1"

#: The measured path. `app/api/entry.py:47` registers the blueprint with
#: `url_prefix="/api/v1"` and line 547 declares
#: `@bp.get("/patients/<patient_guid>/diagnosis-summary")`.
SUMMARY_PATH = f"/api/v1/patients/{PATIENT}/diagnosis-summary"

RELEASE = "2024-01"
AS_OF = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)

SCOPE_NOTE = (
    "filtered to your 2 organisation(s); a diagnosis recorded for another "
    "organisation is not shown. An admin read across organisations needs "
    "X-Admin-Read-Reason."
)


# -- bodies, copied from icd11's serialisers --------------------------------

def _condition(guid, code, display, *, clinical="active",
               verification="confirmed", onset="2024-03-02T00:00:00+00:00",
               release=RELEASE, **over):
    """`fhir._state_as_condition` — the problem-list projection.

    Note what is NOT here: no `recordedDate`, no `abatementDateTime`, no
    `encounter`. Those live on `fhir.condition()`, which the diagnosis-summary
    route does not use. Reproduced faithfully so the test cannot assert a
    field icd11 never sends.
    """
    body = {
        "resourceType": "Condition",
        "id": guid,
        "meta": {"security": [{"system": "urn:pdhc:source-org", "code": ORG}]},
        "clinicalStatus": {"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
            "code": clinical}]},
        "verificationStatus": {"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/condition-ver-status",
            "code": verification}]},
        "code": {
            "coding": [{"system": ic.MMS_SYSTEM, "version": release,
                        "code": code, "display": display}],
            "text": display,
        },
        "subject": {"reference": f"Patient/{PATIENT}"},
        "onsetDateTime": onset,
    }
    body.update(over)
    return body


#: The IPS absence sentinel, verbatim from `fhir.problem_list_bundle`.
ABSENT_ENTRY = {"resource": {
    "resourceType": "Condition",
    "code": {"coding": [{
        "system": "http://hl7.org/fhir/uv/ips/CodeSystem/absent-unknown-uv-ips",
        "code": "no-problem-info",
        "display": "No information about problems",
    }], "text": "Inga kända diagnoser"},
}}


def _bundle(entries, *, scope="unrestricted", scope_note=None,
            as_of_iso="2026-11-11T12:00:00+00:00", timestamp=None):
    """`problem_list_bundle` + the meta `diagnosis_summary` stamps on it."""
    bundle = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": list(entries),
        "meta": {"tag": [
            {"system": "urn:pdhc:as-of", "code": as_of_iso},
            {"system": "urn:pdhc:scope", "code": scope},
        ]},
    }
    if timestamp:
        bundle["timestamp"] = timestamp
    if scope_note is not None:
        bundle["meta"]["scopeNote"] = scope_note
    return bundle


def _three_diagnoses():
    """A precoordinated stem, a postcoordinated cluster, and a resolved one.

    The cluster expression and its rendered label are both icd11's own: the
    label is `DiagnosisCode.label`, NOT NULL, built by WHO's `describe` —
    which is why the display does not have to come from termbank (#965).
    """
    return [
        {"resource": _condition(EP_A, "5A11", "Diabetes mellitus typ 2")},
        {"resource": _condition(
            EP_B, "1A00&XN62R",
            "Kolera [Vibrio cholerae O1, biovar eltor]")},
        {"resource": _condition(
            EP_C, "CA40.0", "Bakteriell pneumoni",
            clinical="resolved", verification="confirmed",
            onset="2023-01-05T00:00:00+00:00")},
    ]


def _fetch(bundle_or_outcome, *, calls=None, base_url=BASE, token=None,
           app=None, **kwargs):
    """Run `problem_list()` over a mocked socket, inside a request context."""
    app = app or _app()
    with app.test_request_context():
        with patch("app.services.icd11_client.requests.get",
                   side_effect=_router({SUMMARY_PATH: bundle_or_outcome},
                                       calls=calls)):
            return ic.Icd11Client(base_url=base_url, token=token
                                  ).problem_list(PATIENT, **kwargs)


# ---------------------------------------------------------------------------
# The populated list
# ---------------------------------------------------------------------------

class TestAPopulatedProblemList:

    def test_it_carries_every_diagnosis(self):
        result = _fetch((200, _bundle(_three_diagnoses())))
        assert len(result.diagnoses) == 3
        assert all(isinstance(d, Diagnosis) for d in result.diagnoses)
        assert [d.code for d in result.diagnoses] == [
            "5A11", "1A00&XN62R", "CA40.0"]

    def test_the_display_comes_from_the_bundle_not_from_termbank(self):
        """termbank holds no ICD-11 concepts (#965), so a `$lookup` there
        returns nothing. icd11 stores the label itself and sends it."""
        result = _fetch((200, _bundle(_three_diagnoses())))
        assert result.diagnoses[0].display == "Diabetes mellitus typ 2"
        assert result.diagnoses[1].display == (
            "Kolera [Vibrio cholerae O1, biovar eltor]")

    def test_the_system_is_the_WHO_MMS_URI(self):
        result = _fetch((200, _bundle(_three_diagnoses())))
        assert all(d.system == "http://id.who.int/icd/release/11/mms"
                   for d in result.diagnoses)

    def test_a_postcoordinated_cluster_carries_the_full_cluster(self):
        """`postcoordination` is the whole expression, stem and qualifier —
        it is what the clinician entered, and `DiagnosisCodePart.label` makes
        each part renderable from icd11 alone."""
        cluster = _fetch((200, _bundle(_three_diagnoses()))).diagnoses[1]
        assert cluster.code == "1A00&XN62R"
        assert cluster.postcoordination == "1A00&XN62R"

    def test_a_bare_stem_is_NOT_called_postcoordinated(self):
        """The field would mean nothing if every code filled it."""
        stem = _fetch((200, _bundle(_three_diagnoses()))).diagnoses[0]
        assert stem.postcoordination is None

    @pytest.mark.parametrize("code,expected", [
        ("5A11", False),
        ("1A00&XN62R", True),
        ("NA00&XJ652&XA20Q1", True),
        ("5A11/BD54.0", True),      # two stems in one statement
        (None, False),
        ("", False),
    ])
    def test_the_postcoordination_rule(self, code, expected):
        assert ic.is_postcoordinated(code) is expected

    def test_an_inactive_diagnosis_keeps_its_real_status(self):
        """`episode.problem_list` filters resolved/inactive out unless
        `include_inactive=true`, so when one DOES arrive its status must
        survive rather than being normalised to active."""
        resolved = _fetch((200, _bundle(_three_diagnoses()))).diagnoses[2]
        assert resolved.clinical_status == "resolved"
        assert resolved.verification_status == "confirmed"

    def test_include_inactive_is_the_literal_string_icd11_tests_for(self):
        """icd11 does `request.args.get("include_inactive") == "true"`, so a
        Python bool stringified to "True" would silently do nothing."""
        calls = []
        _fetch((200, _bundle(_three_diagnoses())), calls=calls,
               include_inactive=True)
        assert calls[0]["params"]["include_inactive"] == "true"

    def test_it_is_not_sent_when_not_asked_for(self):
        calls = []
        _fetch((200, _bundle(_three_diagnoses())), calls=calls)
        assert "include_inactive" not in (calls[0]["params"] or {})

    def test_onset_is_carried_and_the_absent_fields_are_None(self):
        """Finding 3: the problem-list projection emits `onsetDateTime` and
        NOT `recordedDate` / `abatementDateTime` / `encounter`. None here
        means "icd11 does not send it on this endpoint", never "no value" —
        and nothing is substituted for them."""
        d = _fetch((200, _bundle(_three_diagnoses()))).diagnoses[0]
        assert d.onset == "2024-03-02T00:00:00+00:00"
        assert d.recorded_date is None
        assert d.abatement is None
        assert d.encounter_ref is None
        assert d.foundation_uri is None

    def test_the_same_parser_reads_the_richer_condition_projection(self):
        """`fhir.condition()` — used by `/episodes/<guid>/history` — DOES
        carry recordedDate and abatementDateTime. One parser, so a future
        widening of the summary endpoint needs no change here."""
        rich = _condition(EP_A, "5A11", "Diabetes mellitus typ 2",
                          clinical="resolved")
        rich["recordedDate"] = "2024-03-05T09:00:00+00:00"
        rich["abatementDateTime"] = "2025-02-01T00:00:00+00:00"
        rich["encounter"] = {"reference": "Encounter/enc-77"}
        d = ic.parse_diagnosis(rich)
        assert d.recorded_date == "2024-03-05T09:00:00+00:00"
        assert d.abatement == "2025-02-01T00:00:00+00:00"
        assert d.encounter_ref == "Encounter/enc-77"

    def test_the_release_is_kept_even_though_the_dto_has_no_field(self):
        """Rule 28 in icd11: never the code without the release. `releases`
        is parallel to `diagnoses`."""
        result = _fetch((200, _bundle(_three_diagnoses())))
        assert result.releases == (RELEASE, RELEASE, RELEASE)

    def test_a_precoordinated_equivalent_does_not_replace_the_cluster(self):
        """`fhir.condition` appends `simplified_to` as a SECOND MMS coding —
        "additional information, not a replacement". The first coding is what
        the clinician entered and is what the chip must show."""
        res = _condition(EP_B, "GC08&XN6P4", "Kluster-etiketten")
        res["code"]["coding"].append({
            "system": ic.MMS_SYSTEM, "version": RELEASE,
            "code": "GC08.0", "display": "Förenklad kategori"})
        d = ic.parse_diagnosis(res)
        assert d.code == "GC08&XN6P4"
        assert d.display == "Kluster-etiketten"


# ---------------------------------------------------------------------------
# The absence sentinel
# ---------------------------------------------------------------------------

class TestAnEmptyBundleIsNotAnEmptyEntryArray:
    """`problem_list_bundle` emits ONE Condition when there are no states,
    carrying the IPS `absent-unknown-uv-ips#no-problem-info` pattern, because
    IPS requires absence to be STATED. Two failure modes are possible and
    both are checked: parsing it as a diagnosis invents one, and writing it
    off as malformed loses the assertion."""

    def test_the_sentinel_is_not_a_diagnosis(self):
        result = _fetch((200, _bundle([ABSENT_ENTRY])))
        assert result.diagnoses == ()
        assert not any(d.code == "no-problem-info"
                       for d in result.diagnoses)

    def test_the_sentinel_is_recorded_as_an_explicit_assertion(self):
        result = _fetch((200, _bundle([ABSENT_ENTRY])))
        assert result.absent_assertion is True

    def test_the_sentinel_is_not_counted_as_a_skipped_entry(self):
        """It is a recognised answer, not something this client dropped."""
        result = _fetch((200, _bundle([ABSENT_ENTRY])))
        assert result.skipped_entries == 0

    def test_a_bundle_with_no_entry_array_at_all_is_still_readable(self):
        bundle = _bundle([])
        del bundle["entry"]
        result = _fetch((200, bundle))
        assert result.diagnoses == ()
        assert result.absent_assertion is False


# ---------------------------------------------------------------------------
# THE defect: the two empties
# ---------------------------------------------------------------------------

class TestTheTwoEmptiesAreDifferentAnswers:
    """2026-10-23: a diagnosis recorded against an organisation the reader
    was not affiliated with simply vanished, and the clinician went looking
    for a bug that was not there. icd11's fix was `meta.scopeNote`; this
    client's job is to carry it, never to swallow it."""

    @staticmethod
    def _unscoped():
        return _fetch((200, _bundle([ABSENT_ENTRY], scope="unrestricted")))

    @staticmethod
    def _scoped():
        return _fetch((200, _bundle([ABSENT_ENTRY], scope="organisation",
                                    scope_note=SCOPE_NOTE)))

    def test_both_have_no_diagnoses(self):
        """The premise. If this ever fails the rest proves nothing."""
        assert self._unscoped().diagnoses == ()
        assert self._scoped().diagnoses == ()

    def test_the_two_results_are_NOT_equal(self):
        """The whole point, in one assertion: a caller holding these two
        objects can tell them apart without knowing which field to look at.
        `== []` on the diagnoses passes while the distinction is destroyed."""
        assert self._unscoped() != self._scoped()

    def test_the_scope_note_arrives_verbatim(self):
        """Character for character. It names the number of organisations and
        the break-glass header; a paraphrase would be this service's claim
        rather than icd11's."""
        assert self._scoped().scope_note == SCOPE_NOTE

    def test_the_unscoped_empty_carries_no_note(self):
        assert self._unscoped().scope_note is None

    def test_only_the_unscoped_empty_is_definitively_empty(self):
        assert self._unscoped().is_definitively_empty is True
        assert self._scoped().is_definitively_empty is False

    def test_the_scope_tag_itself_is_carried(self):
        assert self._unscoped().scope == "unrestricted"
        assert self._scoped().scope == "organisation"
        assert self._scoped().is_scoped is True

    def test_may_be_incomplete_tracks_the_scope_and_not_the_length(self):
        assert self._unscoped().may_be_incomplete is False
        assert self._scoped().may_be_incomplete is True

    def test_an_absent_scope_tag_is_not_read_as_unrestricted(self):
        """A bundle with no `meta` at all is an UNKNOWN scope. Treating it as
        unrestricted would assert an authority icd11 never claimed."""
        bundle = _bundle([ABSENT_ENTRY])
        del bundle["meta"]
        result = _fetch((200, bundle))
        assert result.scope is None
        assert result.is_definitively_empty is False
        assert result.may_be_incomplete is True

    def test_the_note_reaches_dto_PatientCard_through_one_named_mapping(self):
        """`patient_card_fields()` is the single place `dto.py`'s two field
        names are spelled, so a rename there breaks in one place — and a
        caller who only wanted chips cannot forget the note."""
        card = PatientCard(patient_guid=PATIENT,
                           **self._scoped().patient_card_fields())
        assert card.diagnoses == ()
        assert card.diagnosis_scope_note == SCOPE_NOTE

        clean = PatientCard(patient_guid=PATIENT,
                            **self._unscoped().patient_card_fields())
        assert clean.diagnosis_scope_note is None
        assert card != clean


class TestTheScopeNoteSurvivesANonEmptyList:
    """The easiest half of the defect to reintroduce: a scoped read that DID
    return diagnoses is still partial, so the note must come through there
    too — not only on the empty path."""

    def test_a_scoped_list_with_diagnoses_still_carries_the_note(self):
        result = _fetch((200, _bundle(_three_diagnoses(),
                                      scope="organisation",
                                      scope_note=SCOPE_NOTE)))
        assert len(result.diagnoses) == 3
        assert result.scope_note == SCOPE_NOTE
        assert result.may_be_incomplete is True
        assert result.is_definitively_empty is False

    def test_an_unscoped_list_with_diagnoses_carries_no_note(self):
        result = _fetch((200, _bundle(_three_diagnoses())))
        assert result.scope_note is None
        assert result.may_be_incomplete is False


# ---------------------------------------------------------------------------
# as_of — the time travel
# ---------------------------------------------------------------------------

class TestAsOfIsPassedThrough:

    def test_a_datetime_is_sent_as_ISO_8601(self):
        calls = []
        _fetch((200, _bundle([ABSENT_ENTRY])), calls=calls, as_of=AS_OF)
        assert calls[0]["params"]["as_of"] == "2026-06-01T00:00:00+00:00"

    def test_a_string_is_sent_unchanged(self):
        calls = []
        _fetch((200, _bundle([ABSENT_ENTRY])), calls=calls,
               as_of="2025-06-01T00:00:00Z")
        assert calls[0]["params"]["as_of"] == "2025-06-01T00:00:00Z"

    def test_no_as_of_sends_no_parameter(self):
        """Absence must read as absence: icd11 defaults to now, and sending
        an empty `as_of` would make it refuse with a parse error."""
        calls = []
        _fetch((200, _bundle([ABSENT_ENTRY])), calls=calls)
        assert "as_of" not in (calls[0]["params"] or {})

    def test_the_effective_as_of_comes_back_on_every_bundle(self):
        """icd11 stamps `urn:pdhc:as-of` even when none was asked for, so the
        same question is answerable identically later."""
        result = _fetch((200, _bundle([ABSENT_ENTRY],
                                      as_of_iso="2026-06-01T00:00:00+00:00")))
        assert result.as_of == "2026-06-01T00:00:00+00:00"

    def test_the_tag_is_preferred_over_Bundle_timestamp(self):
        """`Bundle.timestamp` is set only when an explicit `as_of` was given;
        the tag is always there. Reading the tag means one code path."""
        result = _fetch((200, _bundle(
            [ABSENT_ENTRY], as_of_iso="2026-06-01T00:00:00+00:00",
            timestamp="2026-06-01T00:00:00+00:00")))
        assert result.as_of == "2026-06-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Not configured — refusing BY NAME
# ---------------------------------------------------------------------------

class TestNotConfigured:

    def test_is_configured_false_when_unset(self):
        app = _app()
        with app.app_context():
            assert ic.is_configured() is False

    def test_is_configured_does_not_raise_outside_an_app_context(self):
        assert ic.is_configured() is False

    def test_there_is_no_fabricated_default_base_url(self):
        """xlate's module-level `DEFAULT_BASE_URL` manufactured a
        configured-looking URL out of an unset variable, so "nobody wired
        this up" was reported as a retryable 503 forever (#833). Unset must
        read as unset.

        Asserted on the parse tree, not on the text. The module's own
        docstring quotes xlate's default in order to warn about it, so a
        `"127.0.0.1" not in source` check would fail on the COMMENT
        explaining the guard — the instrument error CLAUDE.md §7 records
        four times in one day. `ast` sees assignments; it does not see
        prose."""
        import ast
        tree = ast.parse(open(ic.__file__, encoding="utf-8").read())
        assigned = {
            t.id
            for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))
            for t in ([node.target] if isinstance(node, ast.AnnAssign) else node.targets)
            if isinstance(t, ast.Name)
        }
        assert "DEFAULT_BASE_URL" not in assigned
        assert not hasattr(ic, "DEFAULT_BASE_URL")

    def test_the_guard_above_would_notice_a_default(self):
        """The control. Without it the assertion is satisfied by any module
        whose top level this parser fails to read at all."""
        import ast
        tree = ast.parse('DEFAULT_BASE_URL = "http://127.0.0.1:9017"\n')
        assigned = {t.id for node in tree.body
                    if isinstance(node, ast.Assign) for t in node.targets
                    if isinstance(t, ast.Name)}
        assert "DEFAULT_BASE_URL" in assigned

    def test_the_refusal_NAMES_the_variable(self):
        app = _app()
        with app.app_context():
            client = ic.Icd11Client(base_url="")
            with pytest.raises(ic.Icd11NotConfigured) as exc:
                client.problem_list(PATIENT)
        assert "ICD11_BASE_URL" in str(exc.value)

    def test_the_refusal_says_UNKNOWN_is_not_ABSENT(self):
        """The message is read by whoever is wondering why the card is
        blank; it must not invite them to read it as "no diagnoses"."""
        app = _app()
        with app.app_context():
            with pytest.raises(ic.Icd11NotConfigured) as exc:
                ic.Icd11Client(base_url="").problem_list(PATIENT)
        text = str(exc.value).lower()
        assert "unknown" in text and "absent" in text

    def test_not_configured_does_not_open_a_socket(self):
        app = _app()
        with app.app_context():
            with patch("app.services.icd11_client.requests.get") as g:
                with pytest.raises(ic.Icd11NotConfigured):
                    ic.Icd11Client(base_url="").problem_list(PATIENT)
                assert g.call_count == 0

    def test_not_configured_is_NOT_the_same_type_as_unreachable(self):
        """One flag, or an empty list, makes a deployment omission and an
        outage indistinguishable — which is how every spärr lookup in this
        service came back "no blocks" without asking anyone (#827)."""
        assert not issubclass(ic.Icd11NotConfigured, ic.Icd11Unreachable)
        assert not issubclass(ic.Icd11Unreachable, ic.Icd11NotConfigured)
        assert issubclass(ic.Icd11NotConfigured, ic.Icd11ClientError)
        assert issubclass(ic.Icd11Unreachable, ic.Icd11ClientError)

    def test_an_unconfigured_read_never_degrades_to_an_empty_list(self):
        app = _app()
        with app.app_context():
            with pytest.raises(ic.Icd11ClientError):
                ic.Icd11Client(base_url="").fetch_diagnosis_summary(PATIENT)


class TestBaseUrlResolution:

    def test_config_wins(self):
        app = _app(ICD11_BASE_URL=BASE)
        with app.app_context():
            assert ic.resolve_base_url() == BASE
            assert ic.is_configured() is True

    def test_the_ENVIRONMENT_leg_is_load_bearing(self, monkeypatch):
        """`create_app` loads CDR1_BASE_URL, IPS_BASE_URL, GATEWAY_BASE_URL
        and SSO_BASE_URL into the config and does NOT load this key, and this
        module may not edit `app/__init__.py`. So in production — where the
        value arrives through compose's `env_file: .env` — the environment is
        the ONLY path. Pinned, because a later "tidy-up" that drops the
        environment leg would break production and no other test."""
        app = _app()
        with app.app_context():
            assert app.config.get(ic.ENV_BASE_URL) in (None, "")
            monkeypatch.setenv(ic.ENV_BASE_URL, BASE)
            assert ic.resolve_base_url() == BASE
            assert ic.is_configured() is True
            assert ic.Icd11Client().base_url == BASE

    def test_whitespace_is_not_configuration(self):
        app = _app(ICD11_BASE_URL="   ")
        with app.app_context():
            assert ic.is_configured() is False

    def test_a_trailing_slash_does_not_double_up(self):
        calls = []
        app = _app()
        with app.test_request_context():
            with patch("app.services.icd11_client.requests.get",
                       side_effect=_router({SUMMARY_PATH: (200, _bundle([]))},
                                           calls=calls)):
                ic.Icd11Client(base_url=BASE + "/").problem_list(PATIENT)
        assert calls[0]["url"] == BASE + SUMMARY_PATH


# ---------------------------------------------------------------------------
# Unreachable — the other type
# ---------------------------------------------------------------------------

class TestUnreachable:

    def test_a_network_error_raises_Icd11Unreachable(self):
        app = _app()
        def boom():
            raise requests_lib.ConnectionError("refused")
        with app.test_request_context():
            with patch("app.services.icd11_client.requests.get",
                       side_effect=_router({SUMMARY_PATH: boom})):
                with pytest.raises(ic.Icd11Unreachable):
                    ic.Icd11Client(base_url=BASE).problem_list(PATIENT)

    def test_a_timeout_raises_Icd11Unreachable(self):
        app = _app()
        def boom():
            raise requests_lib.Timeout("too slow")
        with app.test_request_context():
            with patch("app.services.icd11_client.requests.get",
                       side_effect=_router({SUMMARY_PATH: boom})):
                with pytest.raises(ic.Icd11Unreachable):
                    ic.Icd11Client(base_url=BASE).problem_list(PATIENT)

    @pytest.mark.parametrize("status", [401, 403, 404, 500, 503])
    def test_a_non_2xx_raises_rather_than_returning_an_empty_list(self, status):
        """Including 404: icd11 answers an unknown PATIENT with a 200 and the
        absence sentinel, so a 404 on this path means the route is not where
        we think it is — a prefix or proxy mistake, never emptiness."""
        app = _app()
        with app.test_request_context():
            with patch("app.services.icd11_client.requests.get",
                       side_effect=_router({SUMMARY_PATH: (status, {})})):
                with pytest.raises(ic.Icd11Unreachable) as exc:
                    ic.Icd11Client(base_url=BASE).problem_list(PATIENT)
        assert exc.value.status_code == status

    def test_a_non_json_body_raises(self):
        app = _app()
        with app.test_request_context():
            with patch("app.services.icd11_client.requests.get",
                       side_effect=_router({SUMMARY_PATH: (200, _NotJson())})):
                with pytest.raises(ic.Icd11Unreachable):
                    ic.Icd11Client(base_url=BASE).problem_list(PATIENT)

    def test_the_operation_outcome_diagnostic_is_carried_into_the_message(self):
        """icd11's 403s are facts about the OPERATOR's sso account
        (PASSWORD_CHANGE_REQUIRED #949, PHASE_NOT_PERMITTED). A bare "403"
        sends the reader to the wrong service."""
        oo = {"resourceType": "OperationOutcome", "issue": [{
            "severity": "error", "code": "security",
            "details": {"text": "your PDHC password must be changed"},
            "diagnostics": "PASSWORD_CHANGE_REQUIRED"}]}
        app = _app()
        with app.test_request_context():
            with patch("app.services.icd11_client.requests.get",
                       side_effect=_router({SUMMARY_PATH: (403, oo)})):
                with pytest.raises(ic.Icd11Unreachable) as exc:
                    ic.Icd11Client(base_url=BASE).problem_list(PATIENT)
        assert "PASSWORD_CHANGE_REQUIRED" in str(exc.value)

    def test_an_empty_diagnosis_list_from_a_200_means_icd11_ANSWERED(self):
        """The contract, stated as a test: `()` is never a failure."""
        result = _fetch((200, _bundle([ABSENT_ENTRY])))
        assert result.diagnoses == ()
        assert result.absent_assertion is True


# ---------------------------------------------------------------------------
# Malformed entries — skipped, never fabricated
# ---------------------------------------------------------------------------

class TestAMalformedEntryIsSkipped:
    """Every field required below is NOT NULL in icd11's own schema and is
    emitted unconditionally by `fhir._state_as_condition`, so an entry
    missing one did not come from that projection. `dto.Diagnosis` types all
    four as a plain `str`, so the alternative to skipping is to invent a
    code, a label or a clinical status — and an invented `clinical_status` is
    a clinical claim nobody made."""

    def _one_bad(self, bad):
        good = _three_diagnoses()
        return _fetch((200, _bundle(good + [{"resource": bad}])))

    def test_a_Condition_with_no_code_is_skipped(self):
        bad = _condition(EP_A, "5A11", "x")
        bad["code"] = {"text": "nothing machine readable"}
        result = self._one_bad(bad)
        assert len(result.diagnoses) == 3
        assert result.skipped_entries == 1

    def test_a_Condition_with_no_display_anywhere_is_skipped(self):
        bad = _condition(EP_A, "5A11", "x")
        bad["code"] = {"coding": [{"system": ic.MMS_SYSTEM, "code": "5A11"}]}
        result = self._one_bad(bad)
        assert len(result.diagnoses) == 3
        assert result.skipped_entries == 1

    def test_a_Condition_with_no_clinical_status_is_skipped(self):
        bad = _condition(EP_A, "5A11", "Diabetes")
        del bad["clinicalStatus"]
        result = self._one_bad(bad)
        assert len(result.diagnoses) == 3
        assert all(d.clinical_status for d in result.diagnoses)

    def test_a_coding_in_another_system_is_not_an_ICD11_diagnosis(self):
        """An ICD-10-SE coding is a real code and is not what this chip row
        claims to show, so it is skipped rather than relabelled MMS."""
        bad = _condition(EP_A, "E11.9", "Diabetes")
        bad["code"]["coding"] = [{
            "system": "https://termbank.pdhc.se/CodeSystem/icd10",
            "code": "E11.9", "display": "Diabetes"}]
        result = self._one_bad(bad)
        assert len(result.diagnoses) == 3
        assert "E11.9" not in [d.code for d in result.diagnoses]

    @pytest.mark.parametrize("junk", [None, "a string", 42, [], {},
                                      {"resourceType": "Patient"}])
    def test_junk_is_skipped_and_never_becomes_a_Diagnosis(self, junk):
        assert ic.parse_diagnosis(junk) is None

    def test_a_skip_is_COUNTED_so_zero_diagnoses_can_be_audited(self):
        """Dropping a diagnosis in silence is how "0 diagnoses" becomes
        untrustworthy. The count is on the result, and it is the only honest
        way to tell a parse failure from a genuinely empty list."""
        bad = _condition(EP_A, "5A11", "x")
        bad["code"] = {}
        result = _fetch((200, _bundle([{"resource": bad}])))
        assert result.diagnoses == ()
        assert result.skipped_entries == 1
        assert result.absent_assertion is False

    def test_a_display_only_in_code_text_is_still_usable(self):
        """`code.text` is icd11's IPS narrative, the same NOT NULL label —
        a legitimate second home for the display, not a fallback over
        nothing. Skipping it would LOSE a diagnosis."""
        res = _condition(EP_A, "5A11", "Diabetes mellitus typ 2")
        res["code"]["coding"][0].pop("display")
        d = ic.parse_diagnosis(res)
        assert d is not None
        assert d.display == "Diabetes mellitus typ 2"


# ---------------------------------------------------------------------------
# Auth — the measured door, and not an invented one
# ---------------------------------------------------------------------------

class TestHeaders:

    def test_it_sends_Authorization_Bearer_and_nothing_invented(self):
        """icd11's `require_auth` -> `_sso_context` reads
        `Authorization: Bearer`, else `session['sso_token']`. It has no
        KNOWN_SERVICES and reads neither X-Service-Key nor X-API-Key on any
        inbound route, so either would authenticate nothing — the mistake
        three services made against ips (#783), and the opposite of
        request.pdhc, which reads X-API-Key and ignores Authorization."""
        calls = []
        _fetch((200, _bundle([ABSENT_ENTRY])), calls=calls, token="tok-abc")
        headers = calls[0]["headers"]
        assert headers["Authorization"] == "Bearer tok-abc"
        assert "X-Service-Key" not in headers
        assert "X-API-Key" not in headers

    def test_it_never_sends_the_break_glass_header(self):
        """`X-Admin-Read-Reason` makes an SU admin's read UNSCOPED. Widening
        a clinical read is a decision a human makes and states; a BFF doing
        it on their behalf would also leave the audit row with no reason
        worth the name."""
        calls = []
        _fetch((200, _bundle([ABSENT_ENTRY])), calls=calls, token="tok-abc")
        assert "X-Admin-Read-Reason" not in calls[0]["headers"]
        # Checked on the WIRE, not on the source: the module docstring names
        # the header in order to explain why it is not sent, so a text search
        # would flag the explanation (CLAUDE.md §7).
        assert not any("admin-read-reason" in k.lower()
                       for k in calls[0]["headers"])

    def test_it_forwards_the_operator_session_id(self):
        """X2 session propagation (#408) — NOT a credential."""
        app = _app()
        calls = []
        with app.test_request_context(
                headers={"X-Operator-Session-Id": "sid-123"}):
            with patch("app.services.icd11_client.requests.get",
                       side_effect=_router({SUMMARY_PATH: (200, _bundle([]))},
                                           calls=calls)):
                ic.Icd11Client(base_url=BASE).problem_list(PATIENT)
        assert calls[0]["headers"]["X-Operator-Session-Id"] == "sid-123"

    def test_no_token_sends_no_Authorization_header(self):
        """Unset reads as unset: an empty `Bearer ` would be refused with a
        confusing message rather than an honest one."""
        calls = []
        _fetch((200, _bundle([ABSENT_ENTRY])), calls=calls, token=None)
        assert "Authorization" not in calls[0]["headers"]


# ---------------------------------------------------------------------------
# The measured path
# ---------------------------------------------------------------------------

class TestTheMeasuredPath:

    def test_it_hits_the_api_v1_prefixed_route(self):
        """`app/api/entry.py:47` registers the blueprint with
        `url_prefix="/api/v1"`; line 547 declares the route. The bare
        `/patients/...` in the plan is a ROUTE, not a URL."""
        calls = []
        _fetch((200, _bundle([ABSENT_ENTRY])), calls=calls)
        assert calls[0]["url"] == (
            f"{BASE}/api/v1/patients/{PATIENT}/diagnosis-summary")

    def test_the_episode_history_path(self):
        app = _app()
        calls = []
        path = f"/api/v1/episodes/{EP_A}/history"
        with app.test_request_context():
            with patch("app.services.icd11_client.requests.get",
                       side_effect=_router(
                           {path: (200, {"resourceType": "Bundle",
                                         "type": "history", "entry": []})},
                           calls=calls)):
                body = ic.Icd11Client(base_url=BASE).fetch_episode_history(EP_A)
        assert calls[0]["url"] == BASE + path
        assert body["type"] == "history"

    def test_a_404_on_the_history_is_an_ANSWER_and_returns_None(self):
        app = _app()
        path = f"/api/v1/episodes/{EP_A}/history"
        with app.test_request_context():
            with patch("app.services.icd11_client.requests.get",
                       side_effect=_router({path: (404, {})})):
                assert ic.Icd11Client(
                    base_url=BASE).fetch_episode_history(EP_A) is None

    def test_a_403_on_the_history_is_NOT_flattened_to_None(self):
        """icd11 answers an out-of-scope episode with 403 "outside your
        organisation". Returning None there would assert the episode does not
        exist — the same two-meanings-one-answer defect in miniature."""
        app = _app()
        path = f"/api/v1/episodes/{EP_A}/history"
        with app.test_request_context():
            with patch("app.services.icd11_client.requests.get",
                       side_effect=_router({path: (403, {})})):
                with pytest.raises(ic.Icd11Unreachable):
                    ic.Icd11Client(base_url=BASE).fetch_episode_history(EP_A)

    def test_the_raw_bundle_is_available_unparsed(self):
        """A caller forwarding the IPS artefact must send icd11's own JSON,
        not a re-serialisation of these dataclasses."""
        app = _app()
        bundle = _bundle(_three_diagnoses())
        with app.test_request_context():
            with patch("app.services.icd11_client.requests.get",
                       side_effect=_router({SUMMARY_PATH: (200, bundle)})):
                raw = ic.Icd11Client(
                    base_url=BASE).fetch_diagnosis_summary(PATIENT)
        assert raw == bundle


# ---------------------------------------------------------------------------
# Pure parsing, no socket
# ---------------------------------------------------------------------------

class TestPureParsing:

    def test_parse_problem_list_bundle_needs_no_app_context(self):
        result = ic.parse_problem_list_bundle(
            _bundle(_three_diagnoses(), scope="organisation",
                    scope_note=SCOPE_NOTE))
        assert len(result.diagnoses) == 3
        assert result.scope_note == SCOPE_NOTE

    def test_a_non_bundle_body_is_an_UNKNOWN_scope_not_an_empty_answer(self):
        result = ic.parse_problem_list_bundle("not a bundle")
        assert result.diagnoses == ()
        assert result.scope is None
        assert result.is_definitively_empty is False

    def test_the_absence_sentinel_is_recognised_in_isolation(self):
        assert ic.is_absent_assertion(ABSENT_ENTRY["resource"]) is True
        assert ic.is_absent_assertion(
            _condition(EP_A, "5A11", "Diabetes")) is False
