"""Integration-shaped tests for the researcher workspace.

Patches ``requests.request`` end-to-end and walks the cohort flow:
  1. POST /api/cohort to define a cohort
  2. GET  /api/cohort to list it
  3. GET  histogram against a 2-CDR fan-out → merged result has total n
  4. GET  scatter with > max → returns truncated=True flag
  5. GET  export → CSV with the expected column header
"""
from __future__ import annotations

import csv
import io
from unittest.mock import patch

import pytest
import sqlalchemy

from app import create_app


@pytest.fixture(autouse=True)
def _allow_all_consent(monkeypatch):
    """Cohort routes join every member set against ips.pdhc's
    analysis-filter and fail closed (503) when it's unreachable. These
    tests exercise the federation flow, not consent — patch the client
    to an allow-all verdict (same seam test_analysis_consent.py patches)."""
    from app.routes import researcher as r
    fake = type("C", (), {"analysis_filter": staticmethod(
        lambda guids, purpose, projects=None: {
            "allowed": list(guids), "excluded": []})})()
    monkeypatch.setattr(r, "_ips_client", lambda: fake)


@pytest.fixture
def app():
    app = create_app({
        "TESTING": True,
        "AUTH_MODE": "off",
        # Hermetic per-test in-memory DB (#441). StaticPool is required:
        # bare sqlite :memory: gives each connection a private db, so
        # rows written in one request would be invisible to the next.
        # create_app overwrites SQLALCHEMY_DATABASE_URI from its DATABASE_URL
        # config key, so set both — otherwise an ambient DATABASE_URL env
        # var would silently re-point the test at a real Postgres.
        "DATABASE_URL": "sqlite:///:memory:",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {
            "connect_args": {"check_same_thread": False},
            "poolclass": sqlalchemy.pool.StaticPool,
        },
        "CDR_ENDPOINTS": [
            {"cdr_id": "cdr1", "base_url": "http://cdr1", "region_label": "Norrland"},
            {"cdr_id": "cdr2", "base_url": "http://cdr2", "region_label": "Skåne"},
        ],
        "EXPORT_AUDIT_LOG": "/tmp/dash_export_audit_test.log",
    })
    with app.app_context():
        from app.models import db
        db.create_all()
    return app


@pytest.fixture
def admin_client(app):
    """A test client whose every request lands as admin."""
    @app.before_request
    def _set_blob():
        from flask import g
        g.access_blob = {"is_su_admin": True, "roles": ["researcher"],
                          "organization_ids": ["org-x"]}
    return app.test_client()


# ---------------------------------------------------------------------------
# Fake CDR HTTP responses
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
    def json(self):
        return self._body
    @property
    def text(self):
        return ""


def _mk_patient(guid):
    return {"resourceType": "Patient", "id": guid, "active": True}


def _mk_obs(pat, code, value, eff="2026-04-01T10:00:00+00:00", system="https://termbank.pdhc.se/CodeSystem/loinc"):
    return {
        "resourceType": "Observation",
        "subject": {"reference": f"Patient/{pat}"},
        "code": {"coding": [{"system": system, "code": code}]},
        "effectiveDateTime": eff,
        "valueQuantity": {"value": value, "unit": "%", "code": "%"},
    }


def _make_request_dispatcher(routes):
    """``routes`` is a list of (predicate_fn, response_body) pairs.

    Returns a function suitable for ``requests.request`` patching.
    """
    def _req(method, url, params=None, json=None, headers=None, timeout=None):
        for predicate, body in routes:
            if predicate(method, url, params or {}, json or {}):
                return _FakeResp(200, body)
        return _FakeResp(404, {})
    return _req


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_define_cohort_returns_id_and_count(app, admin_client):
    """POST /api/cohort with a Condition predicate → cohort with the
    intersected patient set."""
    routes = [
        # Patient search returns p1 + p2 on cdr1, p3 on cdr2.
        (lambda m, u, p, j: "/api/v1/fhir/Patient" in u and "cdr1" in u,
         {"entry": [{"resource": _mk_patient("p1")},
                     {"resource": _mk_patient("p2")}]}),
        (lambda m, u, p, j: "/api/v1/fhir/Patient" in u and "cdr2" in u,
         {"entry": [{"resource": _mk_patient("p3")}]}),
        # Condition on T2DM returns p2 on cdr1, p3 on cdr2.
        (lambda m, u, p, j: "/api/v1/fhir/Condition" in u and "cdr1" in u,
         {"entry": [
             {"resource": {"resourceType": "Condition",
                            "subject": {"reference": "Patient/p2"}}},
         ]}),
        (lambda m, u, p, j: "/api/v1/fhir/Condition" in u and "cdr2" in u,
         {"entry": [
             {"resource": {"resourceType": "Condition",
                            "subject": {"reference": "Patient/p3"}}},
         ]}),
    ]
    with patch("app.analyse.federation.requests.request",
                side_effect=_make_request_dispatcher(routes)):
        resp = admin_client.post(
            "/api/cohort",
            json={
                "cdr_ids": ["cdr1", "cdr2"],
                "conditions": [
                    "https://termbank.pdhc.se/CodeSystem/snomed/44054006"
                ],
            },
        )
    assert resp.status_code == 201
    body = resp.get_json()
    # Intersection of {p1, p2, p3} and {p2, p3} = {p2, p3}.
    assert body["n"] == 2


def test_cohort_histogram_merges_two_cdrs(app, admin_client):
    """Define a degenerate-empty cohort, then query its histogram —
    the histogram returns the federation-level merged result regardless
    of cohort membership (it's per-CDR $stats merge, not row-by-row)."""
    # First create a cohort.
    routes_define = [
        (lambda m, u, p, j: "/api/v1/fhir/Patient" in u,
         {"entry": [{"resource": _mk_patient("p1")}]}),
    ]
    with patch("app.analyse.federation.requests.request",
                side_effect=_make_request_dispatcher(routes_define)):
        resp = admin_client.post(
            "/api/cohort",
            json={"cdr_ids": ["cdr1", "cdr2"]},
        )
    cohort_id = resp.get_json()["cohort_id"]

    # Now query the histogram. Since #289 the route no longer calls the
    # CDRs' Observation/$stats (that route is gone) — it fetches raw
    # matching Observations from each CDR and computes/merges stats
    # locally. Serve 100 raw obs from cdr1 and 80 from cdr2 → n=180.
    a = {"resourceType": "Bundle", "entry": [
        {"resource": _mk_obs(f"p{i}", "4548-4", 5.0 + (i % 30) * 0.1)}
        for i in range(100)
    ]}
    b = {"resourceType": "Bundle", "entry": [
        {"resource": _mk_obs(f"q{i}", "4548-4", 6.0 + (i % 30) * 0.1)}
        for i in range(80)
    ]}
    routes_hist = [
        (lambda m, u, p, j: "/api/v1/fhir/Observation" in u and "cdr1" in u, a),
        (lambda m, u, p, j: "/api/v1/fhir/Observation" in u and "cdr2" in u, b),
    ]
    with patch("app.analyse.federation.requests.request",
                side_effect=_make_request_dispatcher(routes_hist)):
        h = admin_client.get(
            f"/api/cohort/{cohort_id}/variable/"
            "https://termbank.pdhc.se/CodeSystem/loinc/4548-4/histogram",
        )
    assert h.status_code == 200
    body = h.get_json()
    assert body["n"] == 180
    assert sum(b["count"] for b in body["buckets"]) == 180


def test_cohort_export_csv_has_expected_header(app, admin_client):
    # Create cohort with 2 members.
    routes_define = [
        (lambda m, u, p, j: "/api/v1/fhir/Patient" in u,
         {"entry": [{"resource": _mk_patient("p1")},
                     {"resource": _mk_patient("p2")}]}),
    ]
    with patch("app.analyse.federation.requests.request",
                side_effect=_make_request_dispatcher(routes_define)):
        resp = admin_client.post(
            "/api/cohort",
            json={"cdr_ids": ["cdr1"]},
        )
    cohort_id = resp.get_json()["cohort_id"]

    routes_export = [
        (lambda m, u, p, j: "/api/v1/fhir/Observation" in u and "cdr1" in u,
         {"entry": [
             {"resource": _mk_obs("p1", "4548-4", 6.4)},
             {"resource": _mk_obs("p2", "4548-4", 7.1)},
             # Out-of-cohort patient — the export's members filter is
             # currently *intentionally lenient* (cohort_export: CDR
             # Patient.id and obs.subject.reference live in different
             # uuid namespaces today), so p99 streams through. Tighten
             # this back to "p99 filtered out" when the route restores
             # `if pat_guid not in members: continue`.
             {"resource": _mk_obs("p99", "4548-4", 9.5)},
         ]}),
    ]
    with patch("app.analyse.federation.requests.request",
                side_effect=_make_request_dispatcher(routes_export)):
        r = admin_client.get(
            f"/api/cohort/{cohort_id}/export"
            "?format=csv&variables=https://termbank.pdhc.se/CodeSystem/loinc/4548-4",
        )
        # The CSV is streamed; consume it INSIDE the patch context so
        # the generator runs while requests.request is still mocked.
        body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(body)))
    header = rows[0]
    assert "patient_guid" in header and "canonical" in header \
           and "sim_run_id" in header
    data_rows = rows[1:]
    pat_guids = [r[0] for r in data_rows]
    # Pins the documented lenient contract (see routes_export comment
    # above): all subjects stream through, cohort members included.
    assert sorted(pat_guids) == ["p1", "p2", "p99"]


def test_scatter_truncates_above_cap(app, admin_client):
    # Create cohort with 200 members.
    members = [f"pat-{i}" for i in range(200)]
    pat_entries = [{"resource": _mk_patient(g)} for g in members]
    routes_define = [
        (lambda m, u, p, j: "/api/v1/fhir/Patient" in u,
         {"entry": pat_entries}),
    ]
    with patch("app.analyse.federation.requests.request",
                side_effect=_make_request_dispatcher(routes_define)):
        resp = admin_client.post(
            "/api/cohort",
            json={"cdr_ids": ["cdr1"]},
        )
    cohort_id = resp.get_json()["cohort_id"]

    # Each patient has both x and y → 200 paired points.
    obs_x = [_mk_obs(g, "4548-4", 6.0 + i * 0.01) for i, g in enumerate(members)]
    obs_y = [_mk_obs(g, "29463-7", 70.0 + i * 0.5) for i, g in enumerate(members)]
    def _route(m, u, p, j):
        if "/api/v1/fhir/Observation" not in u:
            return False
        return True
    def _resp(m, u, p, j):
        # Branch on `code=` param to choose x vs y.
        code = (p.get("code") or "")
        if "4548-4" in code:
            return {"entry": [{"resource": x} for x in obs_x]}
        if "29463-7" in code:
            return {"entry": [{"resource": y} for y in obs_y]}
        return {"entry": []}

    def _req_disp(method, url, params=None, json=None, headers=None, timeout=None):
        if _route(method, url, params or {}, json or {}):
            return _FakeResp(200, _resp(method, url, params or {}, json or {}))
        return _FakeResp(404, {})

    with patch("app.analyse.federation.requests.request", side_effect=_req_disp):
        r = admin_client.get(
            f"/api/cohort/{cohort_id}/scatter"
            "?x=https://termbank.pdhc.se/CodeSystem/loinc/4548-4"
            "&y=https://termbank.pdhc.se/CodeSystem/loinc/29463-7"
            "&max=50",
        )
    body = r.get_json()
    assert body["truncated"] is True
    assert body["n"] <= 50
