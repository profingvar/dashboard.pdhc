"""Phase 1 step 8 — the dashboard's per-patient access gate.

WHY EVERY TEST HERE HAS A MUTATION RECORDED BESIDE IT. CLAUDE.md §11 records a
990-test suite that passed identically before and after a security gate was
written, because every test ran under `AUTH_DISABLED` — so a gate with no
failing test was indistinguishable from no gate. These tests drive
`assert_patient_open` directly, with a real blob, so there is no disabled path
to hide in.

THE TEST THIS FILE EXISTS FOR is `test_record_basis_does_not_overwrite_the_closed_enum`.
`plans/pdhc_data_shapes.md` §5 defines `purpose` and `access_basis` as closed
enums that four services populate, and a route's values WIN over the X1
defaults. Writing the coding plan's words — "care" / "secondary-use" — into
`access_basis` would silently corrupt that vocabulary while every other test
still passed.
"""
from __future__ import annotations

import pytest
from flask import Flask, g

from app.dashboard import access
from app.dashboard.access import (
    AccessDecision,
    PatientAccessRefused,
    assert_patient_open,
    record_basis,
    resolve_basis,
    sparr_state,
)


@pytest.fixture
def ctx():
    """A bare request context. No database, no SSO, no AUTH_DISABLED path."""
    app = Flask(__name__)
    with app.test_request_context("/dashboard/patients/p1"):
        yield


def _blob(**kw):
    """A professional's access blob, overridable per test."""
    base = {
        "user_guid": "u1",
        "user_type": "professional",
        "affiliations": [{"affiliation_guid": "a1", "role": "physician",
                          "care_unit_guid": "org-A", "role_guid": "r1"}],
        "session_phases": [],
    }
    base.update(kw)
    return base


def _set(blob):
    g.access_blob = blob


# --------------------------------------------------------------------------
# the basis
# --------------------------------------------------------------------------

def test_no_basis_is_refused_by_name(ctx):
    _set({"user_guid": "u1", "user_type": "patient"})
    with pytest.raises(PatientAccessRefused) as e:
        assert_patient_open("p1", ["org-A"])
    assert e.value.code == "no_basis"
    assert e.value.status == 403


def test_care_basis_with_matching_organisation_is_allowed(ctx):
    _set(_blob())
    d = assert_patient_open("p1", ["org-A", "org-B"])
    assert d.basis == "care"
    assert d.unrestricted is False
    assert "org-A" in d.caller_org_guids


def test_care_basis_in_another_organisation_is_refused(ctx):
    _set(_blob())
    with pytest.raises(PatientAccessRefused) as e:
        assert_patient_open("p1", ["org-Z"])
    assert e.value.code == "other_organisation"


def test_an_unassigned_patient_cannot_pass_the_care_check(ctx):
    """28 of 150 patients have no clinic assignment, so this is the common
    case, not an edge case. It must say so by name — an unexplained 403 sends
    a clinician looking for a permissions bug that is not there."""
    _set(_blob())
    with pytest.raises(PatientAccessRefused) as e:
        assert_patient_open("p1", None)
    assert e.value.code == "unassigned_patient"
    assert "secondary" in e.value.reason


def test_a_care_relationship_without_a_clinical_role_is_refused(ctx):
    _set(_blob(affiliations=[{"affiliation_guid": "a1", "role": "receptionist",
                              "care_unit_guid": "org-A"}]))
    with pytest.raises(PatientAccessRefused) as e:
        assert_patient_open("p1", ["org-A"])
    assert e.value.code == "wrong_role"


def test_the_care_basis_IMPLIES_a_non_empty_org_scope_for_a_non_admin(ctx):
    """The invariant that makes `unrestricted = _is_admin()` safe.

    THIS TEST REPLACES A DUD. The first version set a blob that already had a
    care unit, so the mutation it claimed to guard — deriving `unrestricted`
    from an empty org list — changed nothing and the test passed anyway.
    Tracing why showed the "hole" was not one: `has_care_delivery_access`
    admits a non-admin only when `scope_org_guids(blob)` is non-empty, so a
    care basis ALREADY implies a non-empty scope and both expressions were
    equivalent.

    So the thing worth pinning is the COUPLING, not the dead branch. If
    `has_care_delivery_access` is ever loosened to admit a professional with
    no affiliation, this fails — and at that moment deriving `unrestricted`
    from an empty list would let them read every patient on the platform.
    """
    from app.auth import has_care_delivery_access, scope_org_guids

    no_scope = {"user_guid": "u1", "user_type": "professional",
                "affiliations": [], "organization_ids": []}
    assert scope_org_guids(no_scope) == [] or not scope_org_guids(no_scope)
    assert has_care_delivery_access(no_scope) is False, (
        "a non-admin professional with no care unit must NOT hold the care "
        "basis; if this changes, `unrestricted = _is_admin()` becomes the only "
        "thing standing between them and every patient"
    )

    # control: the same blob WITH a care unit does hold the basis, so the
    # assertion above is about the scope and not about something else
    with_scope = {**no_scope,
                  "affiliations": [{"affiliation_guid": "a1",
                                    "role": "physician",
                                    "care_unit_guid": "org-A"}]}
    assert has_care_delivery_access(with_scope) is True


def test_an_admin_with_no_org_scope_is_unrestricted(ctx):
    """The one caller for whom an empty scope legitimately means
    'no restriction' — which is what `org_guids_for` documents, and why that
    rule must not be generalised to everyone else."""
    _set({"user_guid": "u1", "user_type": "professional",
          "affiliations": [], "is_su_admin": True})
    d = assert_patient_open("p1", None)
    assert d.unrestricted is True


def test_an_admin_is_unrestricted(ctx):
    _set(_blob(is_su_admin=True))
    d = assert_patient_open("p1", None)
    assert d.unrestricted is True
    assert d.basis == "care"


def test_the_secondary_use_basis_is_not_organisation_scoped(ctx):
    _set({"user_guid": "u1", "user_type": "professional",
          "affiliations": [], "session_phases": ["analysis"]})
    d = assert_patient_open("p1", ["org-Z"])
    assert d.basis == "secondary-use"


def test_phases_read_session_phases_FIRST(ctx):
    """#409: `effective_phases` is on sso's kill-switch list and is staged to
    stop being emitted. A gate reading only the legacy field sees an empty
    phase list the day that flag flips, and then refuses every professional on
    the platform. All three sibling services read `session_phases` first."""
    assert resolve_basis({"user_guid": "u", "session_phases": ["analysis"]}) \
        == "secondary-use"
    # control: the legacy field alone still works, so this is a preference and
    # not a hard dependency on the new name
    assert resolve_basis({"user_guid": "u", "effective_phases": ["analysis"]}) \
        == "secondary-use"
    assert resolve_basis({"user_guid": "u"}) is None


# --------------------------------------------------------------------------
# spärr — the #827 distinction
# --------------------------------------------------------------------------

def test_sparr_unconfigured_reports_checked_FALSE(ctx, monkeypatch):
    monkeypatch.setattr(access.ips_client, "is_configured", lambda: False)
    s = sparr_state("p1")
    assert s.checked is False
    assert s.blocks == ()


def test_sparr_configured_reports_checked_TRUE(ctx, monkeypatch):
    monkeypatch.setattr(access.ips_client, "is_configured", lambda: True)
    monkeypatch.setattr(access.ips_client, "get_active_blocks", lambda *a, **k: [])
    s = sparr_state("p1")
    assert s.checked is True


def test_an_unreachable_sparr_authority_is_UNDETERMINED_not_no_blocks(ctx, monkeypatch):
    """The whole point of #827, one level up.

    An empty block list means "no blocks". An unreachable authority must not
    produce the same answer, because that serves the data unchecked while
    claiming it was checked.
    """
    monkeypatch.setattr(access.ips_client, "is_configured", lambda: True)

    def boom(*a, **k):
        raise access.ips_client.IpsUnreachable("down")
    monkeypatch.setattr(access.ips_client, "get_active_blocks", boom)

    with pytest.raises(PatientAccessRefused) as e:
        sparr_state("p1")
    assert e.value.code == "undetermined"
    assert e.value.status == 503


# --------------------------------------------------------------------------
# the audit vocabulary — the test this file exists for
# --------------------------------------------------------------------------

PURPOSE_ENUM = {"care", "care_coordination", "research", "quality_registry",
                "quality_assurance", "statistics", "patient_access",
                "administration"}
BASIS_ENUM = {"same_unit", "same_org", "cross_org_consent", "research_consent",
              "quality_registry_basis", "emergency", "patient_self", "su_admin"}


def test_record_basis_does_not_overwrite_the_closed_enum(ctx):
    """`plans/pdhc_data_shapes.md` §5 defines both enums, four services
    populate them, and a route's values WIN over the X1 defaults. So the
    coding plan's words must be MAPPED, never written in."""
    _set(_blob())
    record_basis(AccessDecision("care", "p1", ("org-A",), False))
    snap = g._audit_payload_snapshot
    assert snap["purpose"] in PURPOSE_ENUM
    assert snap.get("access_basis", "same_unit") in BASIS_ENUM
    assert snap["purpose"] != "care-basis"
    assert snap.get("access_basis") not in ("care", "secondary-use")
    # the dashboard's own determination lives under its own key
    assert snap["dashboard_basis"] == "care"


def test_a_secondary_use_read_is_NOT_logged_as_care(ctx):
    """`x1_tuple` classifies purpose by ROUTE, so every patient-facing path is
    `care`. An SU caller reading this dashboard would be logged as a care
    read. §5's own derivation maps SU to `administration`."""
    _set(_blob())
    record_basis(AccessDecision("secondary-use", "p1", (), False))
    snap = g._audit_payload_snapshot
    assert snap["purpose"] == "administration"
    assert snap["access_basis"] in BASIS_ENUM
    assert snap["dashboard_basis"] == "secondary-use"


def test_an_unknown_zone_leaves_the_derived_basis_ALONE(ctx):
    """§5 says access_basis is "the zone basis". Asserting `same_unit` for a
    read whose zone was never evaluated overstates what is known, so when the
    zone is unknown we add nothing and let X1's value stand."""
    _set(_blob())
    record_basis(AccessDecision("care", "p1", ("org-A",), False), zone=None)
    assert "access_basis" not in g._audit_payload_snapshot


def test_a_known_zone_is_recorded(ctx):
    _set(_blob())
    record_basis(AccessDecision("care", "p1", ("org-A",), False),
                 zone="cross_org_consent")
    snap = g._audit_payload_snapshot
    assert snap["access_basis"] == "cross_org_consent"
    assert snap["zone"] == "cross_org_consent"


def test_an_admin_care_read_is_su_admin(ctx):
    _set(_blob(is_su_admin=True))
    record_basis(AccessDecision("care", "p1", (), True))
    assert g._audit_payload_snapshot["access_basis"] == "su_admin"


def test_record_basis_preserves_what_a_route_already_set(ctx):
    _set(_blob())
    g._audit_payload_snapshot = {"filters": {"q": "x"}}
    record_basis(AccessDecision("care", "p1", ("org-A",), False))
    assert g._audit_payload_snapshot["filters"] == {"q": "x"}
