"""Access control and audit for the dashboard's per-patient reads (plan step 8).

NOTHING HERE IS NEW POLICY. The platform already owns every decision this
module needs; the job is to compose them and to record WHICH basis applied.
Reimplementing any of them would create a second source of truth for a
security decision, which is the failure this module exists to avoid.

  the care-delivery / analysis basis   app.auth (#463, route-aware)
  roles                                app.services.role_guards (#415)
  spärr blocks and filtering           app.services.ips_client
  the PDL kontroller log               app.services.audit (#211, #214, #407)
  the composed zone rule               ips.pdhc care_access_policy (#406)

TWO DISTINCTIONS THIS MODULE REFUSES TO COLLAPSE
================================================

1.  "NO BLOCK" vs "NEVER ASKED". `ips_client.get_active_blocks` returns an
    empty list in both cases, and #827 was filed because a banner read the
    first meaning off the second. `sparr_state()` therefore returns the
    `checked` flag beside the blocks, and every response built here must carry
    it — the existing six render/jsonify sites in `routes/charts.py` and
    `routes/nurse.py` do exactly that. A new endpoint that returns only
    "no blocks" reintroduces the defect and will look correct.

2.  "DENIED" vs "COULD NOT DETERMINE". If the authority cannot be reached the
    answer is not "no". We FAIL CLOSED — the caller gets no data — but the
    refusal carries a different code and reason, so an outage is not recorded
    as a policy denial in the audit log, and the UI can say "could not verify"
    instead of "you may not see this patient".

A MEASURED CORRECTION TO THE CODING PLAN
========================================

The plan's step 8 says to compare the caller's organisation with the
patient's, resolving the patient's organisation from ips's clinic assignment
because `patient_org_guid` is not populated (#774).

ips does have an authority for this — `POST /api/v1/patients/<guid>/care-access-check`,
which evaluates the composed zone rule and returns `{allowed, zone,
access_basis, reason}`, auditing every check INCLUDING denials.

**But it requires `author_clinic_guid`: the clinic that AUTHORED the data.**
So it answers "may this reader see data authored by that clinic" — a
PER-DATAPOINT question — and it cannot be the patient-level gate, which asks
"may this reader open this patient at all". Using it that way would require
inventing an author clinic, and inventing an input to a security decision is
worse than not calling it.

So the two levels are kept apart, deliberately:

  PATIENT OPEN   this module: basis + role + organisation overlap, fail closed
  PER DATAPOINT  ips's care-access-check / care-access-filter, and the
                 existing `filter_blocked_points` spärr path

`care_access_check` is exposed on the ips client for the second level. It is
not called from the gate here, and the docstring says why.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
from typing import Any, Literal

from flask import g, jsonify

from app.auth import has_care_delivery_access, scope_org_guids
from app.services import ips_client
from app.services.audit import audit_read
from app.services.role_guards import _is_admin, _roles

#: The two bases the plan allows. Anything else is refused.
AccessBasis = Literal["care", "secondary-use"]

#: The clinical roles that may open a patient under the care basis. The plan
#: names treating physician and nurse; admin satisfies any role check, as it
#: does in role_guards.
CARE_ROLES = frozenset({"physician", "nurse", "doctor", "lakare", "läkare",
                        "sjukskoterska", "sjuksköterska"})


class PatientAccessRefused(Exception):
    """Refusal with a machine code, so the UI can say which kind it was.

    `code` is one of:
      no_basis            neither care-delivery nor the SU phase
      wrong_role          has a basis, but not a treating clinical role
      no_care_unit        right role, but no active care-unit affiliation
      other_organisation  right role, patient not in the caller's organisations
      unassigned_patient  the patient has no clinic assignment at all, so the
                          care check CANNOT pass — 28 of 150 patients today.
                          Reachable under secondary use only.
      undetermined        the authority could not be reached. NOT a denial.
    """

    def __init__(self, code: str, reason: str, status: int = 403):
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.status = status


@dataclass(frozen=True)
class SparrState:
    """Blocks, plus whether the question could be asked at all (#827)."""
    checked: bool
    blocks: tuple = ()

    @property
    def has_any_block(self) -> bool:
        return bool(self.blocks) and ips_client.has_any_active_block(self.blocks)


@dataclass(frozen=True)
class AccessDecision:
    basis: AccessBasis
    patient_guid: str
    caller_org_guids: tuple[str, ...]
    #: True when the caller is unrestricted (admin / SU), so an empty
    #: `caller_org_guids` means "no restriction" rather than "no organisation".
    unrestricted: bool


def resolve_basis(blob: Any) -> AccessBasis | None:
    """Which of the plan's two bases this caller holds, or None.

    Care is checked FIRST and deliberately: a treating clinician must reach a
    patient without the analysis phase, which is the whole point of #463's
    route-aware gate. A caller holding both is recorded as `care`, because
    that is the basis they are actually acting under at the point of care.
    """
    if has_care_delivery_access(blob if isinstance(blob, dict) else None):
        return "care"
    phases = _phases(blob)
    if "analysis" in phases:
        return "secondary-use"
    return None


def _phases(blob: Any) -> set[str]:
    """Phases from the blob, reading `session_phases` FIRST.

    `effective_phases` is on sso's LEGACY_BLOB_FIELDS kill-switch list (#409)
    and is staged to stop being emitted. A gate reading only the legacy field
    sees an empty phase list the day that flag flips and then refuses every
    professional on the platform. The three sibling services all read
    `session_phases` first for exactly this reason.
    """
    if isinstance(blob, dict):
        for key in ("session_phases", "effective_phases"):
            v = blob.get(key)
            if v:
                return {str(p) for p in v}
        return set()
    for key in ("session_phases", "effective_phases"):
        v = getattr(blob, key, None)
        if v:
            return {str(p) for p in v}
    return set()


def _has_care_role() -> bool:
    return _is_admin() or bool(_roles() & CARE_ROLES)


def sparr_state(patient_guid: str) -> SparrState:
    """Blocks for one patient, with the #827 `checked` flag beside them.

    `checked` is `ips_client.is_configured()` — "could we even ask?" — which is
    what the existing six sites report as `sparr_checked`. When ips is
    configured but unreachable the blocks come back empty and `checked` stays
    True; that is a deliberate limitation of the existing client and is not
    made worse here, but it is why `has_any_block` is not evidence of safety.
    """
    checked = ips_client.is_configured()
    if not checked:
        return SparrState(checked=False, blocks=())
    try:
        blocks = tuple(ips_client.get_active_blocks(patient_guid) or ())
    except ips_client.IpsUnreachable:
        # Fail closed at the caller's level, but do NOT report this as "no
        # blocks" — an unreachable spärr authority is the `undetermined` case.
        raise PatientAccessRefused(
            "undetermined",
            "spärr could not be verified for this patient, so the read was "
            "refused rather than served unchecked",
            status=503,
        )
    return SparrState(checked=True, blocks=blocks)


def assert_patient_open(patient_guid: str, patient_org_guids=None) -> AccessDecision:
    """May this caller OPEN this patient? Raises PatientAccessRefused if not.

    `patient_org_guids` is injected rather than fetched, so the organisation
    comparison is testable and so this module does not grow a second patient
    directory. Pass the patient's organisations (from ips's clinic
    assignment); pass None to mean "could not be established", which refuses
    with `unassigned_patient` under the care basis.
    """
    blob = getattr(g, "access_blob", None)
    basis = resolve_basis(blob)
    if basis is None:
        raise PatientAccessRefused(
            "no_basis",
            "this service needs either a care relationship (care-delivery) or "
            "the secondary-use phase; the signed-in account holds neither",
        )

    # The canonical org source is `scope_org_guids(blob)` — affiliations[]
    # .care_unit_guid with a dual-read to the legacy organization_ids. Reading
    # `g.current_user` instead would depend on a route having populated it;
    # the blob is always there when a basis resolved.
    caller_orgs = tuple(scope_org_guids(blob) or ()) if isinstance(blob, dict) else ()

    # UNRESTRICTED IS ADMIN, NOT "no organisations". `org_guids_for` documents
    # an empty list as "no restriction", which is correct FOR AN ADMIN and
    # catastrophic for anyone else: a professional with no affiliation would
    # otherwise read every patient on the platform. Derive it from the role.
    unrestricted = _is_admin()

    if basis == "secondary-use":
        # SU sees full identity by the operator's decision, and is not scoped
        # by the patient's organisation — its gate is the SU grant/consent
        # check that already runs on the analysis path.
        return AccessDecision("secondary-use", patient_guid, caller_orgs,
                              unrestricted)

    if not _has_care_role():
        raise PatientAccessRefused(
            "wrong_role",
            "a care-basis read needs a treating clinical role (physician or "
            "nurse); this account has a care relationship but not such a role",
        )

    if unrestricted:
        return AccessDecision("care", patient_guid, caller_orgs, True)

    if not caller_orgs:
        # DEFENCE IN DEPTH, and currently unreachable: `has_care_delivery_access`
        # admits a non-admin only when `scope_org_guids(blob)` is non-empty, so
        # a care basis already implies a scope. Kept because the alternative —
        # deriving `unrestricted` from an empty list — silently becomes
        # "read every patient" the day that coupling loosens, and a refusal is
        # the right behaviour either way. The coupling itself is pinned by
        # test_the_care_basis_IMPLIES_a_non_empty_org_scope_for_a_non_admin.
        raise PatientAccessRefused(
            "no_care_unit",
            "a care-basis read is scoped to your own organisation and this "
            "account has no active care-unit affiliation",
        )

    if patient_org_guids is None:
        raise PatientAccessRefused(
            "unassigned_patient",
            "this patient has no clinic assignment, so a care relationship "
            "cannot be established; the record is reachable under secondary "
            "use only",
        )

    if not (set(caller_orgs) & set(patient_org_guids)):
        raise PatientAccessRefused(
            "other_organisation",
            "this patient belongs to another organisation; a care-basis read "
            "is scoped to your own",
        )

    return AccessDecision("care", patient_guid, caller_orgs, False)


def record_basis(decision: AccessDecision, *, zone: str | None = None,
                 **extra) -> None:
    """Put the access basis on the PDL audit row, IN THE PLATFORM'S OWN WORDS.

    The coding plan asks for "the access basis (care/SU)". That concept is
    already recorded, as two CLOSED ENUMS defined in
    `plans/pdhc_data_shapes.md` §5, and `audit.x1_tuple` already derives them:

      purpose       care · care_coordination · research · quality_registry ·
                    quality_assurance · statistics · patient_access ·
                    administration
      access_basis  same_unit · same_org · cross_org_consent ·
                    research_consent · quality_registry_basis · emergency ·
                    patient_self · su_admin

    SO DO NOT WRITE "care" OR "secondary-use" INTO `access_basis`. A route's
    values WIN over the X1 defaults (`audit.py`: `{**x1_tuple(...), **route}`),
    so inventing a third vocabulary would silently corrupt a closed enum that
    four services already populate. Map onto it instead.

    THE ONE CORRECTION THIS MAKES, and the spec supports it: `x1_tuple`
    classifies purpose by ROUTE, so every patient-facing path is `care`. A
    SECONDARY-USE caller reading this dashboard would therefore be logged as a
    care read. §5's own derivation says analysis readers map SU to
    `administration`, so that is what we set — caller-classed, not
    route-classed.

    `zone` is passed through when the composed zone rule has actually been
    evaluated for this read (ips returns it from care-access-check). When it
    is None we leave X1's value alone rather than guessing: §5 says
    access_basis is "the zone basis (same_unit / same_org /
    cross_org_consent)", and asserting `same_unit` for a read whose zone was
    never determined would overstate what is known.
    """
    snapshot = dict(getattr(g, "_audit_payload_snapshot", None) or {})

    if decision.basis == "secondary-use":
        snapshot["purpose"] = "administration"
        snapshot.setdefault("access_basis",
                            "su_admin" if decision.unrestricted
                            else "research_consent")
    else:
        snapshot["purpose"] = "care"
        if decision.unrestricted:
            snapshot["access_basis"] = "su_admin"
        elif zone in ("same_unit", "same_org", "cross_org_consent",
                      "emergency"):
            snapshot["access_basis"] = zone
        # else: leave X1's derived value; see the docstring.

    # The dashboard's own determination, under its own key, so the two are
    # comparable rather than conflated. This is extra detail, not a redefinition.
    snapshot["dashboard_basis"] = decision.basis
    snapshot["caller_org_guids"] = list(decision.caller_org_guids)
    snapshot["unrestricted"] = decision.unrestricted
    if zone:
        snapshot["zone"] = zone
    snapshot.update(extra)
    g._audit_payload_snapshot = snapshot


def refusal_response(exc: PatientAccessRefused):
    """A refusal that names itself.

    An unexplained 403 sends a clinician to the wrong place. `undetermined` in
    particular must not read as "you may not" — it means nobody knows yet.
    """
    return jsonify({"error": exc.code, "message": exc.reason}), exc.status


def dashboard_patient_read(org_lookup=None):
    """Decorator for a per-patient dashboard route.

    Order matters: `audit_read` is OUTERMOST so that a refusal is logged too.
    A denied read is PDL-relevant — ips audits its own denials for the same
    reason — and a gate that only logs successes cannot answer "who tried".
    """
    def decorate(fn):
        @wraps(fn)
        def inner(*args, **kwargs):
            patient_guid = kwargs.get("guid") or kwargs.get("patient_guid")
            try:
                orgs = org_lookup(patient_guid) if org_lookup else None
                decision = assert_patient_open(patient_guid, orgs)
            except PatientAccessRefused as exc:
                g._audit_payload_snapshot = {
                    **(getattr(g, "_audit_payload_snapshot", None) or {}),
                    "refused": exc.code, "reason": exc.reason,
                }
                return refusal_response(exc)
            record_basis(decision)
            g.access_decision = decision
            return fn(*args, **kwargs)
        return audit_read(inner)
    return decorate
