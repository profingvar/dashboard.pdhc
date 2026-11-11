"""The patient dashboard's read-only BFF (the operator's plan, step 5).

TWO OF THE PLAN'S FOUR ENDPOINTS ARE BUILT AND TWO ARE BLOCKED, by a measured
upstream gap rather than by effort:

    GET /dashboard/patients/<guid>                      built
    GET /dashboard/patients/<guid>/series/<code>        built
    GET /dashboard/patients/<guid>/events               501, see #983
    GET /dashboard/events/<observation_guid>            501, see #983

CDR1's `/api/v1/clinical/patient/<guid>/series` projects exactly six columns
-- code, at, value, unit, value_string, org_guid -- and omits the
observation's own guid, `transaction_guid` and `ingest_raw_guid`. The columns
EXIST in the database and are populated (`transaction_guid` on 1 949 of
1 949 rows), so this is a projection gap, not a data gap. Without a row
identity there is no cursor tiebreak, no selection, and nothing to look a
detail up by; without the transaction keys there is no path to
`clinical_context`, which is the whole content of the plan's step 18.

The two blocked routes therefore answer 501 with a NAMED reason. A half-built
event list that looks like it works until someone clicks a row is worse than
an honest refusal, and the plan's own principle -- show what is missing rather
than omit it -- applies to the API as much as to the UI.

AUTHORISATION is `app/dashboard/access.py`, which composes the existing
pieces and records which basis applied. `app/auth.py` admits both bases at
the door for these paths; access.py narrows and refuses by a named code.
"""
from __future__ import annotations

import dataclasses
from typing import Any

from flask import Blueprint, jsonify, request

from app.dashboard import units
from app.dashboard.access import (
    UNKNOWN_ORGS,
    PatientAccessRefused,
    dashboard_patient_read,
    refusal_response,
    sparr_state,
)
from app.dashboard.dto import PatientCard, Series
from app.services import cdr1_client, ips_client

bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")

#: Wire names are camelCase (the plan's DTO list); the dataclasses are
#: snake_case (Python). One translation, in one place.
def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(w.title() for w in rest)


def _wire(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {_camel(f.name): _wire(getattr(obj, f.name))
                for f in dataclasses.fields(obj)}
    if isinstance(obj, (list, tuple)):
        return [_wire(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _wire(v) for k, v in obj.items()}
    return obj


# ---------------------------------------------------------------------------
# the organisation question
# ---------------------------------------------------------------------------

def _patient_orgs(patient_guid: str, caller_orgs: tuple[str, ...],
                  is_admin: bool):
    """Is this patient in the caller's organisations?

    Answered through CDR1's care-delivery patient index, which is already
    org-scoped on its side. We do not need the patient's full organisation
    set -- only whether an overlap exists -- so a hit returns the caller's own
    organisations and a miss returns an empty tuple.

    `list_org_patients` RETURNS `[]` ON ANY ERROR as well as on a genuinely
    empty result, so an empty list is ambiguous by construction. It is
    resolved toward `UNKNOWN_ORGS`, which refuses with 503 `undetermined`
    rather than 403 `other_organisation`: an outage must not be recorded as a
    policy denial, and a clinician told "this patient is not yours" when the
    truth is "we could not ask" goes looking for the wrong problem. The
    ambiguity itself belongs upstream and is noted on #983.
    """
    client = cdr1_client.build_client()
    if not getattr(client, "base_url", None):
        return UNKNOWN_ORGS
    rows = client.list_org_patients(list(caller_orgs), is_admin=is_admin)
    if not rows:
        return UNKNOWN_ORGS
    hit = any(r.get("guid") == patient_guid for r in rows)
    return caller_orgs if hit else ()


def _org_lookup(patient_guid: str):
    """Adapter for the decorator, which knows only the patient guid."""
    from flask import g
    blob = getattr(g, "access_blob", None)
    from app.auth import scope_org_guids
    from app.services.role_guards import _is_admin
    orgs = tuple(scope_org_guids(blob) or ()) if isinstance(blob, dict) else ()
    return _patient_orgs(patient_guid, orgs, _is_admin())


# ---------------------------------------------------------------------------
# built
# ---------------------------------------------------------------------------

@bp.get("/patients/<guid>")
@dashboard_patient_read(org_lookup=_org_lookup)
def patient_dashboard(guid: str):
    """The card and the plans. One failing source degrades its own panel."""
    unavailable: list[str] = []

    diagnoses: tuple = ()
    scope_note = None
    definitively_empty = None
    try:
        from app.services import icd11_client  # noqa: PLC0415
        problems = icd11_client.build_client().problem_list(guid)
    except Exception as exc:  # noqa: BLE001 - a panel, not the page
        # NOT an empty chip list. "Unknown" and "none" are different
        # statements about a patient, and a card that silently shows no
        # diagnoses because a service was unreachable asserts the second.
        unavailable.append(f"icd11:{type(exc).__name__}")
    else:
        fields = problems.patient_card_fields()
        diagnoses = fields["diagnoses"]
        scope_note = fields["diagnosis_scope_note"]
        # THREE-WAY, not two. An empty list means either "this patient has no
        # diagnoses" or "none you are scoped to see", and icd11 tells them
        # apart with its `urn:pdhc:scope` tag. Collapsing them sent a
        # clinician hunting a bug that was not there on 2026-10-23, which is
        # why icd11 carries the note at all — so the dashboard must not be
        # where it gets dropped.
        definitively_empty = problems.is_definitively_empty

    plans: tuple = ()
    try:
        from app.services import request_client  # noqa: PLC0415
        plans = request_client.build_client().plan_summaries(guid)
    except Exception as exc:  # noqa: BLE001
        unavailable.append(f"request:{type(exc).__name__}")

    # The EPS sections need a LIVE composer in ips.pdhc (#980); the only read
    # path today is a frozen snapshot, which the operator declined. Reported
    # by name rather than returned as an empty list, because an empty section
    # list and an uncomposed one are not the same statement about a patient.
    unavailable.append("eps:live-composition-pending-980")

    card = PatientCard(patient_guid=guid, diagnoses=tuple(diagnoses),
                       diagnosis_scope_note=scope_note)
    sparr = sparr_state(guid)
    return jsonify({
        "patientCard": _wire(card),
        "plans": _wire(list(plans)),
        "eventsPage": {"rows": [], "nextCursor": None,
                       "unavailable": ["cdr1:no-row-identity-983"]},
        "diagnosesDefinitivelyEmpty": definitively_empty,
        "unavailable": unavailable,
        "hasBlockedSources": ips_client.has_any_active_block(sparr.blocks),
        "sparrChecked": sparr.checked,
    })


@bp.get("/patients/<guid>/series/<path:code_canonical>")
@dashboard_patient_read(org_lookup=_org_lookup)
def patient_series(guid: str, code_canonical: str):
    """Points for one concept, grouped by convertible unit.

    This endpoint works on what CDR1 gives today: `{code, at, value, unit}` is
    enough to draw a chart. What it cannot carry is the per-point provenance,
    because the identity fields are not projected (#983) -- so
    `SeriesPoint.observation_guid` is empty here and the UI must not offer a
    "open this datapoint" affordance from a series until that lands.

    A concept may come back as SEVERAL series: units that are not convertible
    SPLIT rather than being plotted on one axis. HbA1c in % and mmol/mol is
    the exception that joins, through a named affine function -- the ratio
    between those two units is real and clinically wrong by ~32 %.
    """
    client = cdr1_client.build_client()
    from flask import g
    decision = getattr(g, "access_decision", None)
    orgs = list(decision.caller_org_guids) if decision else []
    is_admin = bool(decision and decision.unrestricted)

    points = client.patient_series(
        guid, [code_canonical], request.args.get("from"),
        request.args.get("to"), orgs, is_admin=is_admin,
    )

    sparr = sparr_state(guid)
    exposures: list = []
    if sparr.blocks:
        # ALWAYS a 2-tuple: (kept_points, exposures). The second half is not
        # optional bookkeeping — an indispensable-care LIFT that exposed a
        # blocked point carries its own audit obligation (#472), and
        # `charts.py` records it as `sparr_lift` on the audit row. Discarding
        # it would drop a required detail silently, which is how an audit
        # trail becomes untrue rather than merely thin.
        points, exposures = ips_client.filter_blocked_points(
            points, sparr.blocks)

    rows = [{"observation_guid": "", "code_canonical": p.get("code"),
             "unit": p.get("unit"), "value": p.get("value"),
             "effective_at": p.get("at")} for p in points]
    groups = units.group_series(rows)

    series = [g_.to_series(display_name=code_canonical) for g_ in groups]

    from flask import g as _g
    _g._audit_n_rows = sum(len(s.points) for s in series)
    if exposures:
        from app.routes.charts import _lift_exposure_audit
        snap = dict(getattr(_g, "_audit_payload_snapshot", None) or {})
        snap["sparr_lift"] = _lift_exposure_audit(exposures)
        _g._audit_payload_snapshot = snap

    return jsonify({
        "series": _wire(series),
        # THE #827 PAIR, and both halves are required. `hasBlockedSources` is
        # False when there is no block AND when ips was never asked;
        # `sparrChecked` is the only thing that tells those apart. A response
        # carrying the first without the second reinstates the defect.
        "hasBlockedSources": ips_client.has_any_active_block(sparr.blocks),
        "sparrChecked": sparr.checked,
        "provenanceAvailable": False,     # #983
    })


# ---------------------------------------------------------------------------
# blocked on #983 — answered by name, not half-built
# ---------------------------------------------------------------------------

_BLOCKED = {
    "error": "upstream_projection_incomplete",
    "ticket": "#983",
    "message": (
        "CDR1's clinical series projects six columns and omits the "
        "observation's own guid, transaction_guid and ingest_raw_guid. The "
        "columns exist and are populated, so this is a projection gap. "
        "Without a row identity there is no cursor, no selection and nothing "
        "to look a detail up by."
    ),
}


@bp.get("/patients/<guid>/events")
@dashboard_patient_read(org_lookup=_org_lookup)
def patient_events(guid: str):
    return jsonify(_BLOCKED), 501


@bp.get("/events/<observation_guid>")
def event_detail(observation_guid: str):
    # No patient guid on this route, so the per-patient gate cannot apply; it
    # is blocked anyway. When #983 lands this must be re-gated on the
    # observation's OWN patient, not on the caller's word for it.
    return jsonify(_BLOCKED), 501
