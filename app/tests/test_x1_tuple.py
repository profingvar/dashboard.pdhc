"""X1 (#407) — extended access-log tuple on DashboardAudit."""
from app.services.audit import x1_tuple

BLOB = {
    "user_guid": "person-1",
    "is_su_admin": False,
    "active_affiliation_guid": "a2",
    "affiliations": [
        {"affiliation_guid": "a1", "role": "nurse", "role_guid": "role-nurse"},
        {"affiliation_guid": "a2", "role": "researcher",
         "role_guid": "role-researcher"},
    ],
}


def test_role_guid_from_active_affiliation():
    t = x1_tuple(BLOB, "GET /patient/<guid>")
    assert t["person_guid"] == "person-1"
    assert t["role_guid"] == "role-researcher"


def test_sole_affiliation_fallback():
    blob = {"user_guid": "p", "affiliations": [
        {"affiliation_guid": "a1", "role_guid": "role-nurse"}]}
    assert x1_tuple(blob, "GET /")["role_guid"] == "role-nurse"


def test_purpose_and_basis_by_route_class():
    assert x1_tuple(BLOB, "POST /api/cohort") == {
        "person_guid": "person-1", "role_guid": "role-researcher",
        "purpose": "research", "access_basis": "research_consent"}
    care = x1_tuple(BLOB, "GET /patient/<guid>")
    assert (care["purpose"], care["access_basis"]) == ("care", "same_unit")
    adm = x1_tuple(BLOB, "GET /admin/audit")
    assert adm["purpose"] == "administration"


def test_su_admin_basis_wins():
    blob = {**BLOB, "is_su_admin": True}
    assert x1_tuple(blob, "GET /patient/<guid>")["access_basis"] == "su_admin"


def test_machine_blob_yields_null_role():
    t = x1_tuple({"user_guid": "svc", "affiliations": []}, "GET /api/v1/observations")
    assert t["role_guid"] is None
