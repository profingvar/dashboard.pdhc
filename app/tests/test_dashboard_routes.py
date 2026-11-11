"""The dashboard BFF's two built endpoints, and the two that are blocked.

NOTE WHAT `AUTH_MODE: off` DOES AND DOES NOT PROVE. The repo's route tests use
it, and under it the loaded blob is a dev SU admin — so those tests exercise
the UNRESTRICTED path and prove the SHAPE of a response, not the gate. CLAUDE.md
§11 records a 990-test suite that passed identically before and after a
security gate was written for exactly this reason.

So the gate is driven directly, with a real non-admin blob and no disabled
path to hide in: `test_a_refusal_reaches_the_wire_as_a_NAMED_error` below, and
the twenty tests in `test_dashboard_access.py`.
"""
from __future__ import annotations

import sqlalchemy
from flask import g

from app import create_app
from app.dashboard.access import UNKNOWN_ORGS
from app.models import db
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


class _FakeCdr1:
    base_url = "https://cdr.pdhc.se"

    def __init__(self, series=None, patients=None):
        self._series = series or []
        self._patients = patients if patients is not None else [{"guid": "p1"}]

    def list_org_patients(self, orgs, *, is_admin=False, reason=None):
        return self._patients

    def patient_series(self, guid, codes, frm, to, orgs, *, is_admin=False,
                       reason=None):
        return self._series


def _pt(code, value, unit, at):
    return {"code": code, "at": at, "value": value, "unit": unit,
            "value_string": None, "org_guid": "org-A"}


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

def test_all_four_routes_are_registered():
    rules = {r.rule for r in _app().url_map.iter_rules()}
    assert "/dashboard/patients/<guid>" in rules
    assert "/dashboard/patients/<guid>/series/<path:code_canonical>" in rules
    assert "/dashboard/patients/<guid>/events" in rules
    assert "/dashboard/events/<observation_guid>" in rules


def test_the_series_route_accepts_a_code_WITH_SLASHES():
    """`code_canonical` is a URI — `urn:pdhc:concept/<guid>` on cdr1 and a
    termbank URL elsewhere. A `<string>` converter would 404 on every real
    code, and the failure would look like 'that concept has no data'."""
    rules = [r.rule for r in _app().url_map.iter_rules()
             if "series" in r.rule and "dashboard" in r.rule]
    assert rules == ["/dashboard/patients/<guid>/series/<path:code_canonical>"]


# ---------------------------------------------------------------------------
# the organisation question — the ambiguity rule
# ---------------------------------------------------------------------------

def test_an_empty_patient_list_is_UNDETERMINED_not_a_denial(monkeypatch):
    """`list_org_patients` returns [] on ANY error as well as on a genuinely
    empty result, so empty is ambiguous BY CONSTRUCTION. It must resolve
    toward "could not ask" — an outage recorded as "this patient is not
    yours" sends a clinician after the wrong problem."""
    monkeypatch.setattr(dash.cdr1_client, "build_client",
                        lambda: _FakeCdr1(patients=[]))
    assert dash._patient_orgs("p1", ("org-A",), False) is UNKNOWN_ORGS


def test_an_unconfigured_cdr1_is_UNDETERMINED(monkeypatch):
    class _NoUrl:
        base_url = None
    monkeypatch.setattr(dash.cdr1_client, "build_client", lambda: _NoUrl())
    assert dash._patient_orgs("p1", ("org-A",), False) is UNKNOWN_ORGS


def test_a_hit_returns_the_callers_orgs_and_a_miss_returns_empty(monkeypatch):
    monkeypatch.setattr(dash.cdr1_client, "build_client",
                        lambda: _FakeCdr1(patients=[{"guid": "p1"},
                                                    {"guid": "p2"}]))
    assert dash._patient_orgs("p1", ("org-A",), False) == ("org-A",)
    # a miss is a REAL answer — the list was non-empty, so cdr1 did reply
    assert dash._patient_orgs("p9", ("org-A",), False) == ()


# ---------------------------------------------------------------------------
# the series endpoint
# ---------------------------------------------------------------------------

def test_series_returns_grouped_points_and_BOTH_sparr_fields(monkeypatch):
    app = _app()
    monkeypatch.setattr(dash.cdr1_client, "build_client", lambda: _FakeCdr1(
        series=[_pt("c1", 70.0, "kg", "2026-01-01T00:00:00Z"),
                _pt("c1", 71.0, "kg", "2026-02-01T00:00:00Z")]))
    monkeypatch.setattr(dash.ips_client, "is_configured", lambda: True)
    monkeypatch.setattr(dash.ips_client, "get_active_blocks", lambda *a, **k: [])
    with app.test_client() as c:
        r = c.get("/dashboard/patients/p1/series/c1")
    assert r.status_code == 200
    body = r.get_json()
    assert len(body["series"]) == 1
    assert [p["value"] for p in body["series"][0]["points"]] == [70.0, 71.0]
    # THE #827 PAIR. `hasBlockedSources` alone cannot distinguish "no block"
    # from "never asked"; both fields must be present.
    assert body["hasBlockedSources"] is False
    assert body["sparrChecked"] is True
    assert body["provenanceAvailable"] is False      # #983


def test_series_reports_sparrChecked_FALSE_when_ips_is_unconfigured(monkeypatch):
    app = _app()
    monkeypatch.setattr(dash.cdr1_client, "build_client",
                        lambda: _FakeCdr1(series=[]))
    monkeypatch.setattr(dash.ips_client, "is_configured", lambda: False)
    with app.test_client() as c:
        r = c.get("/dashboard/patients/p1/series/c1")
    assert r.get_json()["sparrChecked"] is False


def test_hba1c_in_BOTH_units_comes_back_as_ONE_series(monkeypatch):
    """End-to-end proof of the operator's decision, through the real route.

    % and mmol/mol are joined by a NAMED AFFINE function, not by a factor:
    7 % is 53 mmol/mol, and the tempting ratio would say 70.
    """
    app = _app()
    code = "http://loinc.org/4548-4"
    monkeypatch.setattr(dash.cdr1_client, "build_client", lambda: _FakeCdr1(
        series=[_pt(code, 7.0, "%", "2026-01-01T00:00:00Z"),
                _pt(code, 53.0, "mmol/mol", "2026-02-01T00:00:00Z")]))
    monkeypatch.setattr(dash.ips_client, "is_configured", lambda: True)
    monkeypatch.setattr(dash.ips_client, "get_active_blocks", lambda *a, **k: [])
    with app.test_client() as c:
        body = c.get(f"/dashboard/patients/p1/series/{code}").get_json()
    assert len(body["series"]) == 1
    s = body["series"][0]
    assert s["unit"] == "mmol/mol"
    assert [round(p["value"]) for p in s["points"]] == [53, 53]


def test_a_spaerr_LIFT_exposure_is_AUDITED_not_discarded(monkeypatch):
    """`filter_blocked_points` returns (kept, exposures) and the second half
    carries an audit obligation (#472). Dropping it makes the trail untrue
    rather than merely thin — which is how my first version of this route was
    written, and why this test exists."""
    app = _app()
    seen = {}
    monkeypatch.setattr(dash.cdr1_client, "build_client", lambda: _FakeCdr1(
        series=[_pt("c1", 1.0, "kg", "2026-01-01T00:00:00Z")]))
    monkeypatch.setattr(dash.ips_client, "is_configured", lambda: True)
    monkeypatch.setattr(dash.ips_client, "get_active_blocks",
                        lambda *a, **k: [Block.from_dict({
                            "guid": "b1", "patient_guid": "p1",
                            "source_scope_type": "clinic",
                            "source_scope_id": "org-A", "is_active": True})])
    monkeypatch.setattr(dash.ips_client, "filter_blocked_points",
                        lambda pts, blocks: ([], [("pt", "lift")]))
    monkeypatch.setattr("app.routes.charts._lift_exposure_audit",
                        lambda exps: seen.setdefault("audited", exps) or "AUDITED")
    with app.test_client() as c:
        c.get("/dashboard/patients/p1/series/c1")
    assert seen.get("audited") == [("pt", "lift")], (
        "the lift exposures were discarded instead of audited"
    )


# ---------------------------------------------------------------------------
# the two that are blocked
# ---------------------------------------------------------------------------

def test_events_and_detail_refuse_BY_NAME_and_cite_the_ticket():
    """A half-built event list that looks like it works until someone clicks
    a row is worse than an honest refusal."""
    app = _app()
    with app.test_client() as c:
        for path in ("/dashboard/patients/p1/events",
                     "/dashboard/events/obs-1"):
            r = c.get(path)
            assert r.status_code == 501, path
            body = r.get_json()
            assert body["error"] == "upstream_projection_incomplete"
            assert body["ticket"] == "#983"
            assert "observation" in body["message"]


# ---------------------------------------------------------------------------
# the gate, driven WITHOUT the disabled path
# ---------------------------------------------------------------------------

def test_a_refusal_reaches_the_wire_as_a_NAMED_error(monkeypatch):
    """Not `AUTH_MODE: off`: a real non-admin blob, so the gate actually runs.

    An unexplained 403 sends a clinician looking for a permissions bug. The
    code must name which refusal it was.
    """
    app = _app()
    monkeypatch.setattr(dash.cdr1_client, "build_client",
                        lambda: _FakeCdr1(patients=[{"guid": "someone-else"}]))
    with app.test_request_context("/dashboard/patients/p1"):
        g.access_blob = {
            "user_guid": "u1", "user_type": "professional",
            "affiliations": [{"affiliation_guid": "a1", "role": "physician",
                              "care_unit_guid": "org-A"}],
        }
        resp, status = dash.patient_dashboard.__wrapped__(guid="p1")
        assert status == 403
        assert resp.get_json()["error"] == "other_organisation"


# ---------------------------------------------------------------------------
# the diagnosis chips, and the distinction that must survive the BFF
# ---------------------------------------------------------------------------

class _FakeProblems:
    def __init__(self, diagnoses=(), note=None, definitively_empty=True):
        self._d = diagnoses
        self._n = note
        self.is_definitively_empty = definitively_empty

    def patient_card_fields(self):
        return {"diagnoses": self._d, "diagnosis_scope_note": self._n}


def _wire_icd11(monkeypatch, problems=None, raises=None):
    import app.services.icd11_client as icd
    class _C:
        def problem_list(self, guid, **kw):
            if raises:
                raise raises
            return problems
    monkeypatch.setattr(icd, "build_client", lambda: _C())


def test_the_scope_note_reaches_the_WIRE(monkeypatch):
    """icd11 distinguishes "no diagnoses" from "none you may see" and says so
    in `meta.scopeNote`. The BFF must not be where that is dropped — it exists
    because the two were once indistinguishable and a clinician went looking
    for a bug that was not there (2026-10-23)."""
    app = _app()
    _wire_icd11(monkeypatch, _FakeProblems(
        note="filtered to your 2 organisation(s)", definitively_empty=False))
    monkeypatch.setattr(dash.cdr1_client, "build_client", lambda: _FakeCdr1())
    monkeypatch.setattr(dash.ips_client, "is_configured", lambda: True)
    monkeypatch.setattr(dash.ips_client, "get_active_blocks", lambda *a, **k: [])
    with app.test_client() as c:
        body = c.get("/dashboard/patients/p1").get_json()
    assert body["patientCard"]["diagnosisScopeNote"] == (
        "filtered to your 2 organisation(s)")
    assert body["diagnosesDefinitivelyEmpty"] is False


def test_an_UNREACHABLE_icd11_is_not_an_empty_chip_list(monkeypatch):
    """"Unknown" and "none" are different statements about a patient."""
    app = _app()
    _wire_icd11(monkeypatch, raises=RuntimeError("down"))
    monkeypatch.setattr(dash.cdr1_client, "build_client", lambda: _FakeCdr1())
    monkeypatch.setattr(dash.ips_client, "is_configured", lambda: True)
    monkeypatch.setattr(dash.ips_client, "get_active_blocks", lambda *a, **k: [])
    with app.test_client() as c:
        body = c.get("/dashboard/patients/p1").get_json()
    assert body["patientCard"]["diagnoses"] == []
    assert any(u.startswith("icd11:") for u in body["unavailable"]), body["unavailable"]
    # and the three-way flag must NOT claim emptiness it never established
    assert body["diagnosesDefinitivelyEmpty"] is None
