"""M0 #415 — analysis consent joins + roles-hack removal.

Covers:
  - research_project_guids() blob derivation (union across affiliations)
  - role_guards._roles() reform derivation (affiliations[].role first,
    legacy roles[] fallback)
  - the service-key blob carries no roles / no admin bit → role guards deny
  - IpsClient.analysis_filter() parse + fail-closed (IpsUnreachable)

The group/population research-consent join (_apply_research_consent, the
/api/cohort route surface) moved to analyse.pdhc with #543; its tests
were removed here. The ips analysis-filter + blob-derivation coverage
that also backs the nurse care-delivery fold stays.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app import create_app
from app.auth import (
    research_project_guids, _service_blob, has_care_delivery_access,
)
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
    from flask import g
    from app.services.role_guards import _roles
    holder["blob"] = {
        "is_su_admin": False,
        "affiliations": [{"care_unit_guid": "u1", "role": "Nurse"}],
    }
    # roles derive from affiliations[].role, case-insensitive…
    with app.test_request_context("/"):
        g.access_blob = holder["blob"]
        assert "nurse" in _roles()
    # …and a bare-affiliation blob (no care-unit user_type) has no
    # care-delivery access, so the #546-folded nurse views (app-level
    # care-delivery gate, not a per-route role guard) stay closed to it.
    assert has_care_delivery_access(holder["blob"]) is False


def test_roles_affiliations_take_precedence_over_legacy(app_with_blob):
    app, holder = app_with_blob
    from flask import g
    from app.services.role_guards import _roles
    holder["blob"] = {
        "is_su_admin": False,
        "affiliations": [{"care_unit_guid": "u1", "role": "nurse"}],
        "roles": ["admin"],  # legacy list must NOT win
    }
    with app.test_request_context("/"):
        g.access_blob = holder["blob"]
        # affiliation role wins; the legacy flat roles[] is ignored.
        assert _roles() == {"nurse"}


def test_legacy_roles_fallback_still_works(app_with_blob):
    app, holder = app_with_blob
    from flask import g
    from app.services.role_guards import _roles
    holder["blob"] = {"is_su_admin": False, "roles": ["nurse"],
                      "organization_ids": ["o1"]}
    with app.test_request_context("/"):
        g.access_blob = holder["blob"]
        # no affiliations → fall back to the legacy flat roles[] list.
        assert "nurse" in _roles()


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
    # /api/nurse is a care-delivery clinical route (#546): a machine
    # service blob (no admin, user_type=service, no affiliations) has no
    # care-delivery access, so the app-level gate keeps it out.
    assert has_care_delivery_access(holder["blob"]) is False


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
