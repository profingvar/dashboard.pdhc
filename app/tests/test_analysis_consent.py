"""M0 #415 — analysis consent joins + roles-hack removal.

Covers:
  - research_project_guids() blob derivation (union across affiliations)
  - role_guards._roles() reform derivation (affiliations[].role first,
    legacy roles[] fallback)
  - the service-key blob carries no roles / no admin bit → role guards deny
  - IpsClient.analysis_filter() parse + fail-closed (IpsUnreachable)
  - _apply_research_consent(): members filtered by ips verdict; 503 when
    ips is unreachable (research reads fail closed)
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app import create_app
from app.auth import research_project_guids, _service_blob
from app.services.ips_client import IpsClient, IpsUnreachable


# ---------------------------------------------------------------------------
# research_project_guids
# ---------------------------------------------------------------------------

def test_research_projects_union_across_affiliations():
    blob = {"affiliations": [
        {"care_unit_guid": "u1", "role": "researcher",
         "research_project_guids": ["p1", "p2"]},
        {"care_unit_guid": "u2", "role": "researcher",
         "research_project_guids": ["p2", "p3"]},
        {"care_unit_guid": "u3", "role": "nurse"},
    ]}
    assert research_project_guids(blob) == ["p1", "p2", "p3"]


def test_research_projects_empty():
    assert research_project_guids({}) == []
    assert research_project_guids({"affiliations": [{"role": "nurse"}]}) == []


# ---------------------------------------------------------------------------
# role_guards reform derivation + service blob lockdown
# ---------------------------------------------------------------------------

@pytest.fixture
def app_with_blob():
    app = create_app({
        "TESTING": True,
        "AUTH_MODE": "off",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "CDR_ENDPOINTS": [],
    })
    holder = {"blob": {}}

    with app.app_context():
        from app.models import db
        db.create_all()

    @app.before_request
    def _set():
        from flask import g
        g.access_blob = holder["blob"]

    return app, holder


def test_roles_derive_from_affiliations(app_with_blob):
    app, holder = app_with_blob
    holder["blob"] = {
        "is_su_admin": False,
        "affiliations": [{"care_unit_guid": "u1", "role": "Researcher"}],
    }
    client = app.test_client()
    # researcher route passes on affiliation role (case-insensitive)…
    assert client.get("/api/cohort").status_code == 200
    # …nurse route still denies
    assert client.get("/api/nurse/patient/x").status_code == 403


def test_roles_affiliations_take_precedence_over_legacy(app_with_blob):
    app, holder = app_with_blob
    holder["blob"] = {
        "is_su_admin": False,
        "affiliations": [{"care_unit_guid": "u1", "role": "nurse"}],
        "roles": ["researcher"],  # legacy list must NOT win
    }
    client = app.test_client()
    assert client.get("/api/cohort").status_code == 403


def test_legacy_roles_fallback_still_works(app_with_blob):
    app, holder = app_with_blob
    holder["blob"] = {"is_su_admin": False, "roles": ["researcher"],
                      "organization_ids": ["o1"]}
    client = app.test_client()
    assert client.get("/api/cohort").status_code == 200


def test_service_blob_has_no_roles_and_no_admin(app_with_blob):
    app, _ = app_with_blob
    with app.test_request_context("/"):
        blob = _service_blob("gateway.pdhc")
    assert blob["is_su_admin"] is False
    assert "roles" not in blob
    assert blob["affiliations"] == []
    assert blob["service_source"] == "gateway.pdhc"


def test_service_blob_denied_on_clinical_routes(app_with_blob):
    app, holder = app_with_blob
    with app.test_request_context("/"):
        holder["blob"] = _service_blob("gateway.pdhc")
    client = app.test_client()
    assert client.get("/api/cohort").status_code == 403
    assert client.get("/api/nurse/patient/x").status_code == 403


# ---------------------------------------------------------------------------
# IpsClient.analysis_filter
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def test_analysis_filter_parses_verdict():
    c = IpsClient(base_url="http://ips.test")
    with patch("app.services.ips_client.requests.post",
               return_value=_Resp(200, {
                   "allowed": ["a"],
                   "excluded": [{"patient_guid": "b", "reason": "ehds_opt_out"}],
               })) as post:
        v = c.analysis_filter(["a", "b"], "research", ["p1"])
    assert v == {"allowed": ["a"],
                 "excluded": [{"patient_guid": "b", "reason": "ehds_opt_out"}]}
    sent = post.call_args.kwargs["json"]
    assert sent == {"patient_guids": ["a", "b"], "purpose": "research",
                    "research_project_guids": ["p1"]}


def test_analysis_filter_empty_input_short_circuits():
    c = IpsClient(base_url="http://ips.test")
    assert c.analysis_filter([], "research") == {"allowed": [], "excluded": []}


def test_analysis_filter_fails_closed_on_error_status():
    c = IpsClient(base_url="http://ips.test")
    with patch("app.services.ips_client.requests.post",
               return_value=_Resp(500, {})):
        with pytest.raises(IpsUnreachable):
            c.analysis_filter(["a"], "research")


def test_analysis_filter_fails_closed_without_base_url():
    c = IpsClient(base_url="")
    with pytest.raises(IpsUnreachable):
        c.analysis_filter(["a"], "research")


# ---------------------------------------------------------------------------
# _apply_research_consent (route-level join)
# ---------------------------------------------------------------------------

def test_apply_research_consent_filters_members(app_with_blob):
    app, holder = app_with_blob
    holder["blob"] = {
        "affiliations": [{"care_unit_guid": "u1", "role": "researcher",
                          "research_project_guids": ["p1"]}],
    }
    from app.routes import researcher as r
    fake = type("C", (), {"analysis_filter": staticmethod(
        lambda guids, purpose, projects: {
            "allowed": ["pat-1"],
            "excluded": [{"patient_guid": "pat-2", "reason": "ehds_opt_out"},
                         {"patient_guid": "pat-3",
                          "reason": "no_research_consent"}],
        })})()
    with app.test_request_context("/"):
        from flask import g
        g.access_blob = holder["blob"]
        with patch.object(r, "_ips_client", return_value=fake):
            members, summary = r._apply_research_consent(
                {"pat-1", "pat-2", "pat-3"})
    assert members == {"pat-1"}
    assert summary["checked"] == 3 and summary["excluded"] == 2
    assert summary["reasons"] == {"ehds_opt_out": 1, "no_research_consent": 1}


def test_apply_research_consent_503_when_ips_down(app_with_blob):
    app, holder = app_with_blob
    from app.routes import researcher as r
    fake = type("C", (), {"analysis_filter": staticmethod(
        lambda *a, **kw: (_ for _ in ()).throw(IpsUnreachable("down")))})()
    with app.test_request_context("/"):
        from flask import g
        g.access_blob = {}
        with patch.object(r, "_ips_client", return_value=fake):
            from werkzeug.exceptions import ServiceUnavailable
            with pytest.raises(ServiceUnavailable):
                r._apply_research_consent({"pat-1"})


def test_apply_research_consent_empty_set_skips_ips(app_with_blob):
    app, _ = app_with_blob
    from app.routes import researcher as r
    with app.test_request_context("/"):
        members, summary = r._apply_research_consent(set())
    assert members == set() and summary["checked"] == 0
