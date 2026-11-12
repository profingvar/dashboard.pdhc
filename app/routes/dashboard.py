"""The patient dashboard's read-only BFF (the operator's plan, step 5).

ALL FOUR of the plan's endpoints are built:

    GET /dashboard/patients/<guid>                      card + plans
    GET /dashboard/patients/<guid>/series/<code>        grouped series
    GET /dashboard/patients/<guid>/events               the event list
    GET /dashboard/events/<observation_guid>            one event's provenance

The last two needed #983 (CDR1 projecting the observation's own guid and its
provenance keys), deployed 2026-11-11. Against an older cdr they still say so
by name — `503 upstream_projection_too_old` — rather than building rows with
an empty identity or reporting every detail "not found".

The PAGE that reads these is `GET /dashboard/view/<guid>` (#976). It carries
no patient data itself; every datum arrives through the guarded, audited JSON
routes above.

AUTHORISATION is `app/dashboard/access.py`, which composes the existing
pieces and records which basis applied. `app/auth.py` admits both bases at
the door for these paths; access.py narrows and refuses by a named code.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import Any

from flask import Blueprint, jsonify, render_template, request

from app.dashboard import units
from app.services.audit import audit_read
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
    row = next((r for r in rows if r.get("guid") == patient_guid), None)
    from flask import g, has_app_context
    if row is not None and has_app_context():
        # The same index answers the card's name and birth date, so keep the
        # row rather than asking CDR1 a second time. Guarded: this function
        # is also a plain question asked outside a request.
        g._dashboard_patient_row = row
    return caller_orgs if row is not None else ()


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

def _eps_section(s: dict):
    """One ips summary section -> `dto.EpsSection`.

    Entries keep their `source` ("ips") and code `system`, because the card
    shows two diagnosis registers in two code systems and the reader must be
    able to tell which said what. An EXPLICITLY_ABSENT section carries its
    absent assertion's text ("no known allergies") as the narrative; a MISSING
    one has no entries and no assertion, which is a different statement.
    """
    from app.dashboard.dto import EpsEntry, EpsSection
    entries = tuple(
        EpsEntry(display=e.get("display") or e.get("narrative_text") or "",
                 source=e.get("source") or "ips", code=e.get("code"),
                 system=e.get("system"), date=e.get("date"),
                 status=e.get("status"))
        for e in (s.get("entries") or []))
    absent = s.get("absent")
    return EpsSection(
        section_code=s.get("loinc") or s.get("key"),
        title=s.get("title") or s.get("key"),
        entries=entries,
        narrative=(absent or {}).get("narrative_text"),
        absent_assertion=s.get("status") == "EXPLICITLY_ABSENT",
    )


@bp.get("/view/<guid>")
def patient_view(guid: str):
    """The page (#976). A SHELL: it holds the patient guid and nothing else.

    Every datum -- card, plans, events, series -- arrives through the JSON
    routes, each of which applies the care/SU basis, the organisation check,
    spärr and the PDL audit. So this route needs no patient check of its own:
    rendering it reveals nothing that the guid in the URL did not already say,
    and duplicating the check here would audit a read that did not happen.
    The door in `app/auth.py` still requires a care or analysis basis.
    """
    return render_template("dashboard.html", patient_guid=guid)


@bp.get("/patients/<guid>")
@dashboard_patient_read(org_lookup=_org_lookup)
def patient_dashboard(guid: str):
    """The patient card. One failing source degrades its own panel."""
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

    # Plans are NOT read here: the plan panel has its own route
    # (`/patients/<guid>/plan`), so the card no longer pays a request.pdhc
    # round trip per page view for a list nothing displayed.

    # The LIVE euIPS summary (#980), read from ips.pdhc on every call — not
    # the frozen snapshot, which the operator declined. A failure is reported
    # by name and degrades THIS panel only: the ICD-11 chips above stand on
    # their own source.
    eps_sections: tuple = ()
    ips_problem_count = None
    try:
        summary = ips_client._default_client().euips_summary(guid)
    except ips_client.IpsUnreachable as exc:
        unavailable.append(f"eps:unreachable:{exc}")
    else:
        if summary is None:
            unavailable.append("eps:patient-not-in-ips-scope")
        else:
            eps_sections = tuple(_eps_section(s) for s in summary["sections"])
            problems = next((s for s in summary["sections"]
                             if s.get("key") == "problems"), None)
            ips_problem_count = len((problems or {}).get("entries") or [])

    # Name and birth date come from CDR1's patient index, already read by the
    # organisation check. Sex, personnummer and organisation have NO source the
    # dashboard reads today; they stay None and the page shows "—", which is
    # "not known here", never "none".
    from flask import g
    row = getattr(g, "_dashboard_patient_row", None) or {}
    card = PatientCard(patient_guid=guid, name=row.get("name") or None,
                       birth_date=row.get("birth_date"),
                       diagnoses=tuple(diagnoses),
                       diagnosis_scope_note=scope_note,
                       eps_sections=eps_sections)
    sparr = sparr_state(guid)
    return jsonify({
        "patientCard": _wire(card),
        "diagnosesDefinitivelyEmpty": definitively_empty,
        # TWO REGISTERS, SHOWN SIDE BY SIDE AND NEVER MERGED (operator,
        # 2026-11-11; #984). The chips are icd11's ICD-11 register; the EPS
        # problems section is ips's SNOMED CT `Condition`s. They are coded in
        # different systems, populated independently, and where both exist
        # they disagree for 3 of 4 patients measured. Merging would combine two
        # code systems as if one; choosing would hide the other. So both are
        # listed with their source, and `reconciled` says plainly that nobody
        # has reconciled them.
        "diagnosisSources": [
            {"source": "icd11", "label": "ICD-11-register",
             "system": "http://id.who.int/icd/release/11/mms",
             "available": definitively_empty is not None,
             "count": len(diagnoses)},
            {"source": "ips", "label": "Patientsammanfattning (SNOMED CT)",
             "system": "http://snomed.info/sct",
             "available": ips_problem_count is not None,
             "count": ips_problem_count},
        ],
        "diagnosisSourcesReconciled": False,
        "unavailable": unavailable,
        "hasBlockedSources": ips_client.has_any_active_block(sparr.blocks),
        "sparrChecked": sparr.checked,
    })


#: The plan panel walks the patient's event pages to count observations per
#: transaction. Bounded: 10 pages x 500 = 5 000 observations, ~2.5x the
#: largest patient on cdr1 (2026-11-12). Past the bound the panel is told the
#: counts are INCOMPLETE rather than shown a smaller number as if whole.
_PLAN_SCAN_PAGE = 500
_PLAN_SCAN_MAX_PAGES = 10


@bp.get("/patients/<guid>/plan")
@dashboard_patient_read(org_lookup=_org_lookup)
def patient_plan(guid: str):
    """The plan panel (#971): plans, and the observations matched to them.

    PLANS come from request.pdhc. On production those are ServiceRequests,
    not CarePlans (0 CarePlans, 28 SRs; measured 2026-11-12) — see
    `request_client.build_order_summary`.

    STATE IS THE ORDER'S STATUS, never the presence of observations.

    OBSERVATIONS are counted per `transaction_guid` from cdr1, matched on
    patient + transaction (the transaction guid is a PlanDefinition-level id
    shared by every patient on that plan, so patient is half of the key).

    DISAGREEMENT IS SHOWN, NOT RECONCILED. Two kinds, both measured:
      - an item whose order is done and that has no observation;
      - observations whose transaction belongs to no order for this patient
        (`unordered`). On 2026-11-12 three of the four patients with data on
        cdr1 had ONLY these — measurements following a plan nobody ordered.
    """
    from flask import g as _g
    from app.services.cdr1_client import (
        Cdr1BadCursor, Cdr1NotConfigured, Cdr1ProjectionTooOld, Cdr1Unreachable)

    unavailable: list[str] = []
    plans: tuple = ()
    plans_known = True
    try:
        from app.services import request_client  # noqa: PLC0415
        plans = request_client.build_client().plan_summaries(guid)
    except Exception as exc:  # noqa: BLE001 - a panel, not the page
        plans_known = False
        unavailable.append(f"request:{type(exc).__name__}")

    decision = _g.access_decision
    client = cdr1_client.build_client()
    points: list = []
    complete = True
    observations_known = True
    cursor = None
    try:
        for _ in range(_PLAN_SCAN_MAX_PAGES):
            page = client.patient_events(
                guid, list(decision.caller_org_guids),
                is_admin=decision.unrestricted, cursor=cursor,
                limit=_PLAN_SCAN_PAGE)
            points.extend(page["points"])
            cursor = page["next_cursor"]
            if not cursor:
                break
        else:
            complete = cursor is None
    except (Cdr1BadCursor, Cdr1ProjectionTooOld, Cdr1NotConfigured,
            Cdr1Unreachable) as exc:
        observations_known = False
        points = []
        unavailable.append(f"cdr1:{type(exc).__name__}")

    # Spärr BEFORE counting: a count of blocked observations would disclose
    # that they exist. Exposures are audited as on every other route.
    sparr = sparr_state(guid)
    exposures: list = []
    if sparr.blocks and points:
        points, exposures = ips_client.filter_blocked_points(points, sparr.blocks)
    if exposures:
        from app.routes.charts import _lift_exposure_audit
        snap = dict(getattr(_g, "_audit_payload_snapshot", None) or {})
        snap["sparr_lift"] = _lift_exposure_audit(exposures)
        _g._audit_payload_snapshot = snap

    by_tx: dict[str, dict] = {}
    untagged = 0
    for p in points:
        tx = p.get("transaction_guid")
        if not tx:
            untagged += 1
            continue
        agg = by_tx.setdefault(str(tx), {"count": 0, "first": None, "last": None,
                                         "code": p.get("code")})
        agg["count"] += 1
        at = p.get("at")
        if at:
            agg["first"] = at if agg["first"] is None or at < agg["first"] else agg["first"]
            agg["last"] = at if agg["last"] is None or at > agg["last"] else agg["last"]

    planned_tx = {i.transaction_guid for pl in plans for i in pl.items
                  if i.transaction_guid}
    unordered_tx = sorted(set(by_tx) - planned_tx)

    names: dict = {}
    if unordered_tx:
        from app.services import plan_client
        concepts = {tx: ips_client.concept_guid_from_canonical(by_tx[tx]["code"])
                    for tx in unordered_tx}
        try:
            names = plan_client.concept_display_names(
                sorted({c for c in concepts.values() if c}))
        except Exception as exc:  # noqa: BLE001
            unavailable.append(f"plan:{type(exc).__name__}")
    else:
        concepts = {}

    _g._audit_n_rows = len(points)
    return jsonify({
        "plans": _wire(list(plans)),
        "plansKnown": plans_known,
        "observationsKnown": observations_known,
        # False = the scan stopped at its bound; counts are LOWER BOUNDS.
        "observationCountsComplete": complete,
        "observationsByTransaction": {
            tx: {"count": a["count"], "first": a["first"], "last": a["last"]}
            for tx, a in by_tx.items()},
        "unordered": [
            {"transactionGuid": tx,
             "display": names.get(concepts.get(tx)) or by_tx[tx]["code"] or tx,
             "count": by_tx[tx]["count"], "first": by_tx[tx]["first"],
             "last": by_tx[tx]["last"]}
            for tx in unordered_tx],
        "observationsWithoutTransaction": untagged,
        "unavailable": unavailable,
        "hasBlockedSources": ips_client.has_any_active_block(sparr.blocks),
        "sparrChecked": sparr.checked,
        # The clock "overdue" is judged by. Comparison is in UTC (an instant
        # is an instant); DISPLAY is Europe/Stockholm, which the page does.
        "now": datetime.now(timezone.utc).isoformat(),
        "displayTimeZone": "Europe/Stockholm",
    })


@bp.get("/patients/<guid>/concepts")
@dashboard_patient_read(org_lookup=_org_lookup)
def patient_concepts(guid: str):
    """The patient's concepts with names and counts — the filter bar's list.

    Free text is matched against THESE names in the page and becomes a
    concept filter sent to cdr1, so a text search is exact over the whole
    history rather than over whatever pages happen to be loaded.
    """
    from flask import g as _g
    from app.services.cdr1_client import (
        Cdr1NotConfigured, Cdr1ProjectionTooOld, Cdr1Unreachable)
    decision = _g.access_decision
    try:
        params = cdr1_client.build_client().patient_concepts(
            guid, list(decision.caller_org_guids), is_admin=decision.unrestricted)
    except (Cdr1NotConfigured, Cdr1ProjectionTooOld, Cdr1Unreachable) as exc:
        return jsonify(error="cdr1_unreachable", message=str(exc)), 503

    unavailable: list[str] = []
    concept_of = {p.get("code"): ips_client.concept_guid_from_canonical(p.get("code"))
                  for p in params}
    names: dict = {}
    if any(concept_of.values()):
        try:
            from app.services import plan_client
            names = plan_client.concept_display_names(
                sorted({c for c in concept_of.values() if c}))
        except Exception as exc:  # noqa: BLE001
            unavailable.append(f"plan:{type(exc).__name__}")
    _g._audit_n_rows = len(params)
    return jsonify({
        "concepts": [{"code": p.get("code"),
                      "displayName": names.get(concept_of.get(p.get("code"))) or p.get("code"),
                      "unit": p.get("unit"), "count": p.get("count"),
                      "first": p.get("first_observed_at"),
                      "last": p.get("last_observed_at")}
                     for p in params if p.get("code")],
        "unavailable": unavailable,
    })


#: The series panel's scan bound: 20 pages x 500 = 10 000 points, enough for
#: 14 days of 5-minute CGM (4 032) twice over. Past it the panel is told the
#: series is INCOMPLETE rather than shown a shorter one as if whole.
_SERIES_SCAN_PAGE = 500
_SERIES_SCAN_MAX_PAGES = 20
#: Above this many points the series is LTTB-downsampled SERVER-SIDE and the
#: response says "showing N of M" (plan step 20).
_SERIES_TARGET = 2000


def lttb_indices(xs: list[float], ys: list[float], target: int) -> list[int]:
    """Largest-triangle-three-buckets, returning INDICES, not values.

    `app.analyse.federation.lttb_downsample` returns (x, y) tuples, which
    loses each point's identity: two points with the same time and value
    cannot be told apart, so the selected observation could not be
    highlighted after downsampling. Same algorithm, keyed on position.
    First and last points are always kept.
    """
    n = len(xs)
    if target >= n or target < 3:
        return list(range(n))
    out = [0]
    every = (n - 2) / (target - 2)
    a = 0
    for i in range(target - 2):
        lo = int((i + 1) * every) + 1
        hi = min(int((i + 2) * every) + 1, n)
        avg_x = sum(xs[lo:hi]) / max(1, hi - lo)
        avg_y = sum(ys[lo:hi]) / max(1, hi - lo)
        r_lo = int(i * every) + 1
        r_hi = int((i + 1) * every) + 1
        best, best_area = r_lo, -1.0
        for j in range(r_lo, r_hi):
            area = abs((xs[a] - avg_x) * (ys[j] - ys[a])
                       - (xs[a] - xs[j]) * (avg_y - ys[a]))
            if area > best_area:
                best, best_area = j, area
        out.append(best)
        a = best
    out.append(n - 1)
    return out


def _epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@bp.get("/patients/<guid>/series/<path:code_canonical>")
@dashboard_patient_read(org_lookup=_org_lookup)
def patient_series(guid: str, code_canonical: str):
    """Points for one concept, grouped by convertible unit (#978).

    Read through the STRICT cursor endpoint (#983), not the legacy series
    proxy, for two reasons: every point carries its own `observation_guid`
    and `transaction_guid` (so the selected row can be highlighted and a
    point can open its provenance), and a failure is named instead of
    arriving as an empty list — "no points" and "cdr1 did not answer" are
    different statements.

    A concept may come back as SEVERAL series: units that are not convertible
    SPLIT rather than being plotted on one axis. HbA1c in % and mmol/mol is
    the exception that joins, through a named affine function -- the ratio
    between those two units is real and clinically wrong by ~32 %.

    `?format=csv` returns the same series as CSV. It is the same audited read
    (the decorator logs it), not a side door.
    """
    from flask import g
    from app.services.cdr1_client import (
        Cdr1BadCursor, Cdr1NotConfigured, Cdr1ProjectionTooOld, Cdr1Unreachable)
    decision = g.access_decision
    client = cdr1_client.build_client()
    # A zoomed chart asks for its visible window at full resolution.
    try:
        frm = _bound(request.args.get("from"), end=False)
        to = _bound(request.args.get("to"), end=True)
    except InvalidFilter as exc:
        return jsonify(error="invalid_filter", message=str(exc)), 400

    points: list = []
    complete = True
    cursor = None
    try:
        for _ in range(_SERIES_SCAN_MAX_PAGES):
            page = client.patient_events(
                guid, list(decision.caller_org_guids),
                is_admin=decision.unrestricted, cursor=cursor,
                limit=_SERIES_SCAN_PAGE, codes=[code_canonical], frm=frm, to=to)
            points.extend(page["points"])
            cursor = page["next_cursor"]
            if not cursor:
                break
        else:
            complete = cursor is None
    except Cdr1ProjectionTooOld as exc:
        return jsonify(error="upstream_projection_too_old", ticket="#983",
                       message=str(exc)), 503
    except Cdr1NotConfigured as exc:
        return jsonify(error="cdr1_not_configured", message=str(exc)), 503
    except (Cdr1Unreachable, Cdr1BadCursor) as exc:
        return jsonify(error="cdr1_unreachable", message=str(exc)), 503

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

    # Numeric points only: a chart cannot place a value_string.
    rows = [{"observation_guid": p.get("observation_guid") or "",
             "code_canonical": p.get("code") or code_canonical,
             "unit": p.get("unit"), "value": p.get("value"),
             "effective_at": p.get("at"),
             "transaction_guid": p.get("transaction_guid")}
            for p in points
            if isinstance(p.get("value"), (int, float)) and p.get("at")]
    non_numeric = len(points) - len(rows)
    groups = units.group_series(rows)

    unavailable: list[str] = []
    display = code_canonical
    concept = ips_client.concept_guid_from_canonical(code_canonical)
    if concept:
        try:
            from app.services import plan_client
            display = plan_client.concept_display_names([concept]).get(
                concept) or code_canonical
        except Exception as exc:  # noqa: BLE001 - a name, not the series
            unavailable.append(f"plan:{type(exc).__name__}")

    from app.dashboard import limits as _limits
    patient_limits = _patient_limits(guid, unavailable)

    series = []
    bands = []
    for grp in groups:
        s_ = grp.to_series(display_name=display)
        # Every point judged in the SERIES unit (group_series may have
        # converted it); limits.evaluate converts to the limit's unit or
        # refuses. The page draws these verdicts and never recomputes them.
        verdicts = [_limits.evaluate(p.value, s_.unit, p.transaction_guid,
                                     patient_limits, code_canonical=code_canonical,
                                     concept_guid=concept)
                    for p in s_.points]
        s_ = dataclasses.replace(s_, points=tuple(
            dataclasses.replace(p, limit_status=v.status)
            for p, v in zip(s_.points, verdicts)))
        bands.append(_band(verdicts, s_.unit))
        pts = list(s_.points)
        if len(pts) > _SERIES_TARGET:
            xs = [_epoch(p.effective_at) or 0.0 for p in pts]
            ys = [float(p.value) for p in pts]
            keep = set(lttb_indices(xs, ys, _SERIES_TARGET))
            # MDR IIa: downsampling must NEVER hide a value outside its limit.
            # LTTB keeps extremes by shape, not by clinical meaning, so every
            # outside point is kept explicitly on top of its selection.
            keep |= {i for i, p in enumerate(pts) if p.limit_status in _limits.OUTSIDE}
            s_ = dataclasses.replace(s_, points=tuple(pts[i] for i in sorted(keep)),
                                     downsampled_from=len(pts))
        series.append(s_)

    g._audit_n_rows = sum(len(s.points) for s in series)
    if exposures:
        from app.routes.charts import _lift_exposure_audit
        snap = dict(getattr(g, "_audit_payload_snapshot", None) or {})
        snap["sparr_lift"] = _lift_exposure_audit(exposures)
        g._audit_payload_snapshot = snap

    if request.args.get("format") == "csv":
        return _series_csv(code_canonical, series, complete)

    return jsonify({
        "series": _wire(series),
        "displayName": display,
        "complete": complete,
        "window": {"from": frm, "to": to},
        # Per series, the limit band IN THE SERIES UNIT when exactly one
        # limit applies to every judged point; otherwise None and the reason.
        "limitBands": bands,
        "nonNumericCount": non_numeric,
        "unavailable": unavailable,
        # THE #827 PAIR, and both halves are required. `hasBlockedSources` is
        # False when there is no block AND when ips was never asked;
        # `sparrChecked` is the only thing that tells those apart. A response
        # carrying the first without the second reinstates the defect.
        "hasBlockedSources": ips_client.has_any_active_block(sparr.blocks),
        "sparrChecked": sparr.checked,
        "provenanceAvailable": True,      # #983 deployed: points carry identity
    })


def _band(verdicts, series_unit):
    """The band to shade behind a series, or a reason there is none.

    Drawn only when ONE limit, from one care plan, governs every point that was
    judged, and its bounds convert to the series unit. Two orders with
    different limits, or a limit in an unconvertible unit, get no band — a
    shaded area must never suggest a single limit that is not there.
    """
    from app.dashboard import units as _u
    judged = [v for v in verdicts if v.limit is not None and not v.limit.invalid
              and v.status in ("inside", "below", "above")]
    if not judged:
        statuses = sorted({v.status for v in verdicts})
        return {"band": None, "reason": "no judged point", "statuses": statuses}
    keys = {(v.limit.low, v.limit.high, v.limit.unit, v.limit.low_inclusive,
             v.limit.high_inclusive) for v in judged}
    if len(keys) > 1:
        return {"band": None, "reason": "more than one limit applies"}
    lim = judged[0].limit
    try:
        low = (_u.convert(lim.low, lim.unit, series_unit) if lim.low is not None
               and lim.unit != series_unit else lim.low)
        high = (_u.convert(lim.high, lim.unit, series_unit) if lim.high is not None
                and lim.unit != series_unit else lim.high)
    except (_u.UnitConversionRefused, TypeError, KeyError):
        return {"band": None, "reason": f"limit in {lim.unit!r} does not convert to {series_unit!r}"}
    return {"band": {"low": low, "high": high, "unit": series_unit,
                     "sourceKind": lim.source_kind,
                     "carePlanGuid": lim.care_plan_guid,
                     "planDefinitionName": lim.plan_definition_name,
                     "planDefinitionVersion": lim.plan_definition_version},
            "reason": None}


def _series_csv(code_canonical: str, series, complete: bool):
    """The series as CSV. Every point the JSON carries, one row each.

    A downsampled series exports the DOWNSAMPLED points and says so in a
    comment line, as an incomplete scan does: a file is read far from the
    screen that explained it, so it must carry its own caveats.
    """
    import csv
    import io
    from flask import Response
    buf = io.StringIO()
    w = csv.writer(buf)
    for s_ in series:
        if s_.downsampled_from:
            buf.write(f"# {s_.unit or ''}: showing {len(s_.points)} of "
                      f"{s_.downsampled_from} points (LTTB)\n")
    if not complete:
        buf.write("# incomplete: the scan stopped at its bound; older points "
                  "are missing\n")
    w.writerow(["code_canonical", "unit", "effective_at", "value",
                "observation_guid", "transaction_guid", "limit_status"])
    for s_ in series:
        for p in s_.points:
            w.writerow([s_.code_canonical, s_.unit or "", p.effective_at,
                        p.value, p.observation_guid, p.transaction_guid or "",
                        p.limit_status or ""])
    safe = "".join(ch if ch.isalnum() else "_" for ch in code_canonical)[-60:]
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             f'attachment; filename="series_{safe}.csv"'})


# ---------------------------------------------------------------------------
# the event list and its detail (#983)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# limits (MDR class IIa, operator 2026-11-12) — judged ONLY in app/dashboard/limits.py
# ---------------------------------------------------------------------------

def _patient_limits(guid: str, unavailable: list[str]):
    """This patient's limits from their CAREPLANS (operator 2026-11-12: the
    CarePlan's goal, else its PlanDefinition range; orders are not used), or
    None when the CarePlans could not be read — which `limits.evaluate` turns
    into `unknown`, never into `no_limit`. One request.pdhc read per call."""
    from app.dashboard import limits
    try:
        from app.services import request_client
        care_plans = request_client.build_client().fetch_care_plans(guid)
    except Exception as exc:  # noqa: BLE001 - a judgement, not the page
        unavailable.append(f"limits:{type(exc).__name__}")
        return None
    return limits.limits_from_care_plans(care_plans, guid)


# ---------------------------------------------------------------------------
# filters (#977 part B) — held in the page URL, validated HERE
# ---------------------------------------------------------------------------

class InvalidFilter(ValueError):
    pass


def _bound(raw: str | None, *, end: bool) -> str | None:
    """A date or datetime filter bound -> an ISO-8601 UTC instant for cdr1.

    WHY THIS IS STRICT. cdr1 parses `from`/`to` leniently and IGNORES what it
    cannot parse, so a malformed bound would silently drop the filter and the
    clinician would read the whole history as the filtered one. Here a bad
    value is a 400 by name.

    A bare date is a CALENDAR DAY IN STOCKHOLM: `from=2026-11-01` starts at
    local midnight, `to=2026-11-01` ends at the next local midnight
    (exclusive, sent as one microsecond before it, since cdr1 compares `<=`).
    """
    if raw is None or raw == "":
        return None
    from datetime import date, timedelta
    from zoneinfo import ZoneInfo
    stockholm = ZoneInfo("Europe/Stockholm")
    try:
        if len(raw) == 10:
            d = date.fromisoformat(raw)
            if end:
                d = d + timedelta(days=1)
            local = datetime(d.year, d.month, d.day, tzinfo=stockholm)
            instant = local.astimezone(timezone.utc)
            if end:
                instant = instant - timedelta(microseconds=1)
        else:
            instant = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if instant.tzinfo is None:
                raise InvalidFilter(f"{raw!r} has no time zone")
            instant = instant.astimezone(timezone.utc)
    except ValueError as exc:
        raise InvalidFilter(f"{raw!r} is not a date (YYYY-MM-DD) or an "
                            f"ISO-8601 instant") from exc
    return instant.isoformat()


def _filters():
    """(codes, frm, to, transactions) from the query string, or InvalidFilter."""
    frm = _bound(request.args.get("from"), end=False)
    to = _bound(request.args.get("to"), end=True)
    if frm and to and frm > to:
        raise InvalidFilter("from is after to")
    codes = [c for c in request.args.getlist("code") if c]
    transactions = [t for t in request.args.getlist("transaction") if t]
    return codes, frm, to, transactions


#: The page size a caller may ask for. cdr1 caps its own limit too; this cap
#: keeps one dashboard request from asking cdr1 for its maximum.
_EVENTS_MAX_LIMIT = 500


@bp.get("/patients/<guid>/events")
@dashboard_patient_read(org_lookup=_org_lookup)
def patient_events(guid: str):
    """One page of the event list, newest first (#983 unblocked this).

    Every failure is answered BY NAME, never as an empty page: "this patient
    has no events" and "cdr1 did not answer" are different statements, and
    the plan's step 24 requires one failing source to degrade its own panel
    and say so.
    """
    from flask import g as _g
    from app.services.cdr1_client import (
        Cdr1BadCursor, Cdr1FilterUnsupported, Cdr1NotConfigured,
        Cdr1ProjectionTooOld, Cdr1Unreachable)

    decision = _g.access_decision
    try:
        limit = max(1, min(_EVENTS_MAX_LIMIT, int(request.args.get("limit", 100))))
    except ValueError:
        return jsonify(error="invalid_limit",
                       message="limit must be an integer"), 400

    try:
        codes, frm, to, transactions = _filters()
    except InvalidFilter as exc:
        return jsonify(error="invalid_filter", message=str(exc)), 400
    unavailable: list[str] = []
    from app.dashboard import limits as _limits
    patient_limits = _patient_limits(guid, unavailable)

    # "Utanför gräns" (MDR IIa). Only a transaction with a usable limit can
    # hold an outside value, so cdr1 is asked for exactly those (the echoed
    # transaction filter) and every page is scanned, bounded. Orders that
    # could not be read are a 503, never an empty "nothing is outside".
    outside_only = request.args.get("outside") == "1"
    if outside_only:
        if patient_limits is None:
            return jsonify(error="limits_unknown",
                           message="the patient's care plans could not be read, so "
                                   "no value can be judged against a limit"), 503
        ranged = sorted(patient_limits.limited_transactions())
        if transactions:
            ranged = [t for t in ranged if t in transactions]
        if not ranged:
            sparr0 = sparr_state(guid)
            return jsonify(rows=[], nextCursor=None, truncated=False,
                           undatedCount=0, unavailable=unavailable, noLimits=True,
                           outsideScanComplete=True,
                           hasBlockedSources=ips_client.has_any_active_block(sparr0.blocks),
                           sparrChecked=sparr0.checked)
        transactions = ranged

    client = cdr1_client.build_client()
    try:
        if outside_only:
            collected: list = []
            cur = None
            scan_complete = True
            for _ in range(_SERIES_SCAN_MAX_PAGES):
                pg = client.patient_events(
                    guid, list(decision.caller_org_guids),
                    is_admin=decision.unrestricted, cursor=cur,
                    limit=_SERIES_SCAN_PAGE, codes=codes, frm=frm, to=to,
                    transactions=transactions)
                collected.extend(pg["points"])
                cur = pg["next_cursor"]
                if not cur:
                    break
            else:
                scan_complete = cur is None
            page = {"points": collected, "next_cursor": None,
                    "truncated": not scan_complete, "undated_count": 0}
        else:
            page = client.patient_events(
                guid, list(decision.caller_org_guids), is_admin=decision.unrestricted,
                cursor=request.args.get("cursor"), limit=limit,
                codes=codes, frm=frm, to=to, transactions=transactions)
    except Cdr1BadCursor as exc:
        return jsonify(error="invalid_cursor", message=str(exc)), 400
    except Cdr1ProjectionTooOld as exc:
        # The code is committed in cdr.pdhc; an older cdr1 lacks it. Say so.
        return jsonify(error="upstream_projection_too_old", ticket="#983",
                       message=str(exc)), 503
    except Cdr1FilterUnsupported as exc:
        # Never an unfiltered page dressed as a filtered one.
        return jsonify(error="upstream_filter_unsupported",
                       message=str(exc)), 503
    except Cdr1NotConfigured as exc:
        return jsonify(error="cdr1_not_configured", message=str(exc)), 503
    except Cdr1Unreachable as exc:
        return jsonify(error="cdr1_unreachable", message=str(exc)), 503

    points = page["points"]

    # Spärr. The exposures carry #472's audit obligation and are RECORDED —
    # the series route's first version discarded them, which is the defect
    # this mirrors the fix for.
    sparr = sparr_state(guid)
    exposures: list = []
    if sparr.blocks:
        points, exposures = ips_client.filter_blocked_points(points, sparr.blocks)
    if outside_only:
        # Judged AFTER spärr, so a blocked value is neither shown nor counted.
        points = [p for p in points if _limits.evaluate(
            p.get("value"), p.get("unit"), p.get("transaction_guid"),
            patient_limits, code_canonical=p.get("code"),
            concept_guid=ips_client.concept_guid_from_canonical(p.get("code"))).outside]

    # Display names: ONE batched call for the whole page, never one per row.
    from app.services import plan_client
    concept_of = {p.get("code"): ips_client.concept_guid_from_canonical(p.get("code"))
                  for p in points}
    names: dict = {}
    try:
        names = plan_client.concept_display_names(
            sorted({c for c in concept_of.values() if c}))
    except Exception as exc:  # noqa: BLE001 - names are a panel concern
        unavailable.append(f"plan:{type(exc).__name__}")

    # Series counts are PER CONCEPT over the patient's whole history, not over
    # this page — a page-local count would put "x2" on a concept with 300
    # points. cdr1's summary already carries the per-concept total.
    summary = client.patient_summary(guid, list(decision.caller_org_guids),
                                     is_admin=decision.unrestricted)
    totals = {s.get("code"): s.get("count") or 0 for s in summary}
    if not summary and points:
        # patient_summary returns [] on failure; with points in hand an empty
        # summary cannot be a true answer. The badge is unknown, not zero.
        unavailable.append("cdr1:summary")

    # T+ from the CarePlan's period.start — only where a care plan is linked.
    # 0 of 1 949 cdr1 rows carry care_plan_guid today, so this usually makes
    # no call at all; it lights up when the datapoint-context work lands.
    from app.dashboard.timing import t_from_start_seconds
    starts: dict = {}
    plan_guids = sorted({p.get("care_plan_guid") for p in points
                         if p.get("care_plan_guid")})
    if plan_guids:
        try:
            from app.services import request_client
            rc = request_client.build_client()
            for cp in plan_guids:
                plan = rc.fetch_care_plan(cp)
                starts[cp] = (plan or {}).get("period_start")
        except Exception as exc:  # noqa: BLE001
            unavailable.append(f"request:{type(exc).__name__}")

    from app.dashboard.dto import EventRow
    rows = []
    for p in points:
        code = p.get("code") or ""
        concept = concept_of.get(code)
        count = totals.get(code)
        cp = p.get("care_plan_guid")
        rows.append(EventRow(
            observation_guid=p["observation_guid"],
            code_canonical=code,
            display_name=names.get(concept) or code,
            value=p.get("value") if p.get("value") is not None else p.get("value_string"),
            unit=p.get("unit"),
            effective_at=p.get("at"),
            source_cdr="cdr1",
            t_from_start_seconds=(t_from_start_seconds(p.get("at"), starts.get(cp))
                                  if cp else None),
            concept_guid=concept,
            transaction_guid=p.get("transaction_guid"),
            care_plan_guid=cp,
            ingest_raw_guid=p.get("ingest_raw_guid"),
            provider_org=p.get("org_guid"),
            has_series=bool(count and count >= 2),
            series_count=count or 0,
            limit=_limits.wire(_limits.evaluate(
                p.get("value"), p.get("unit"), p.get("transaction_guid"),
                patient_limits, code_canonical=code, concept_guid=concept)),
        ))

    _g._audit_n_rows = len(rows)
    if exposures:
        from app.routes.charts import _lift_exposure_audit
        snap = dict(getattr(_g, "_audit_payload_snapshot", None) or {})
        snap["sparr_lift"] = _lift_exposure_audit(exposures)
        _g._audit_payload_snapshot = snap

    return jsonify({
        "rows": _wire(rows),
        "nextCursor": page["next_cursor"],
        "truncated": page["truncated"],
        "undatedCount": page["undated_count"],
        "unavailable": unavailable,
        "hasBlockedSources": ips_client.has_any_active_block(sparr.blocks),
        "sparrChecked": sparr.checked,
        "outsideOnly": outside_only,
        # Only meaningful with outsideOnly: False = the scan stopped at its
        # bound, so older outside values may be missing.
        "outsideScanComplete": (not page["truncated"]) if outside_only else None,
    })


_NOT_FOUND = {"error": "not_found",
              "message": "no such observation in your scope"}


def _first(seq):
    return seq[0] if isinstance(seq, list) and seq else None


def _concept_text(cc):
    """A FHIR CodeableConcept to one string, or None."""
    if not isinstance(cc, dict):
        return None
    coding = _first(cc.get("coding")) or {}
    return cc.get("text") or coding.get("display") or coding.get("code")


def _raw_extras(raw: dict) -> dict:
    """Fields the detail panel shows that live only in the FHIR resource."""
    rr = _first(raw.get("referenceRange")) or {}
    low = (rr.get("low") or {}).get("value")
    high = (rr.get("high") or {}).get("value")
    device = raw.get("device") or {}
    return {
        "reference_low": float(low) if isinstance(low, (int, float)) else None,
        "reference_high": float(high) if isinstance(high, (int, float)) else None,
        "interpretation": _concept_text(_first(raw.get("interpretation"))),
        "method": _concept_text(raw.get("method")),
        "device": device.get("display") or device.get("reference"),
    }


@bp.get("/events/<observation_guid>")
@audit_read
def event_detail(observation_guid: str):
    """One event's full provenance — the plan's step 18.

    THE ROUTE CARRIES NO PATIENT GUID, so the order of operations IS the
    security design:

      1. does the caller hold a basis at all?   — refused before cdr1 is
                                                   asked anything
      2. fetch, scoped to the caller's orgs      — cdr1 answers a flat 404
                                                   outside them
      3. gate on the observation's OWN patient   — never on anything the
                                                   caller asserted
      4. spärr                                   — a blocked point gets the
                                                   SAME 404 as a missing one

    Every provenance field is returned, null when absent, so the panel can
    render "not recorded". `care_plan_guid` is populated on 0 of 1 949 cdr1
    rows and `service_request_guid` on 7 — this panel is where that shows.
    """
    from flask import g as _g
    from app.auth import scope_org_guids
    from app.dashboard.access import (
        PatientAccessRefused, assert_patient_open, record_basis, resolve_basis)
    from app.dashboard.dto import EventDetail, EventRow
    from app.services.cdr1_client import (
        Cdr1NotConfigured, Cdr1ProjectionTooOld, Cdr1Unreachable)
    from app.services.role_guards import _is_admin

    blob = getattr(_g, "access_blob", None)
    if resolve_basis(blob) is None:
        exc = PatientAccessRefused(
            "no_basis", "this service needs either a care relationship or "
                        "the secondary-use phase; the account holds neither")
        _g._audit_payload_snapshot = {"refused": exc.code}
        return refusal_response(exc)

    orgs = list(scope_org_guids(blob) or ()) if isinstance(blob, dict) else []
    admin = _is_admin()
    try:
        d = cdr1_client.build_client().observation_detail(
            observation_guid, orgs, is_admin=admin)
    except Cdr1ProjectionTooOld as exc:
        return jsonify(error="upstream_projection_too_old", ticket="#983",
                       message=str(exc)), 503
    except Cdr1NotConfigured as exc:
        return jsonify(error="cdr1_not_configured", message=str(exc)), 503
    except Cdr1Unreachable as exc:
        return jsonify(error="cdr1_unreachable", message=str(exc)), 503
    if d is None:
        return jsonify(_NOT_FOUND), 404

    patient = d.get("patient_guid")
    _g._audit_patient_guid = patient          # #213: not on the URL
    try:
        decision = assert_patient_open(patient, _org_lookup(patient))
    except PatientAccessRefused as exc:
        _g._audit_payload_snapshot = {"refused": exc.code}
        return refusal_response(exc)
    record_basis(decision)

    point = {"code": d.get("code"), "at": d.get("at"), "org_guid": d.get("org_guid"),
             "observation_guid": d.get("observation_guid")}
    sparr = sparr_state(patient)
    if sparr.blocks:
        kept, exposures = ips_client.filter_blocked_points([point], sparr.blocks)
        if not kept:
            # Hidden by spärr: the SAME answer as "does not exist", so the
            # existence of a blocked datapoint is not disclosed.
            return jsonify(_NOT_FOUND), 404
        if exposures:
            from app.routes.charts import _lift_exposure_audit
            snap = dict(getattr(_g, "_audit_payload_snapshot", None) or {})
            snap["sparr_lift"] = _lift_exposure_audit(exposures)
            _g._audit_payload_snapshot = snap

    ctx = d.get("context") or {}
    raw = d.get("raw_fhir") or {}
    extras = _raw_extras(raw) if isinstance(raw, dict) else _raw_extras({})

    unavailable: list[str] = []
    pd_name = pd_version = None
    if ctx.get("plan_definition_guid"):
        try:
            from app.services import plan_client
            ref = plan_client.build_client().get_plan_definition(
                ctx["plan_definition_guid"])
            if ref is not None:
                pd_name, pd_version = ref.name, ref.version
        except Exception as exc:  # noqa: BLE001
            unavailable.append(f"plan:{type(exc).__name__}")

    code = d.get("code") or ""
    row = EventRow(
        observation_guid=d["observation_guid"], code_canonical=code,
        display_name=code, value=d.get("value") if d.get("value") is not None
        else d.get("value_string"), unit=d.get("unit"),
        effective_at=d.get("at"), source_cdr="cdr1",
        concept_guid=ips_client.concept_guid_from_canonical(code),
        transaction_guid=ctx.get("transaction_guid"),
        care_plan_guid=ctx.get("care_plan_guid"),
        service_request_guid=ctx.get("service_request_guid"),
        plan_activity_guid=ctx.get("plan_activity_guid"),
        ingest_raw_guid=d.get("ingest_raw_guid"),
        provider_org=ctx.get("provider_org_guid"))
    detail = EventDetail(
        row=row,
        # The patient's organisation is not established per datapoint (#774);
        # left None rather than filled from the provider, which is a
        # different organisation with a different meaning.
        patient_org_guid=None,
        provider_org_guid=ctx.get("provider_org_guid"),
        contract_guid=ctx.get("contract_guid"),
        plan_definition_name=pd_name, plan_definition_version=pd_version,
        ingested_at=d.get("received_at"), raw_fhir_observation=raw or None,
        **extras)

    _g._audit_n_rows = 1
    from app.dashboard import limits as _limits
    verdict = _limits.evaluate(
        d.get("value"), d.get("unit"), ctx.get("transaction_guid"),
        _patient_limits(patient, unavailable), code_canonical=code,
        concept_guid=ips_client.concept_guid_from_canonical(code))
    return jsonify({
        "detail": _wire(detail),
        "limit": _limits.wire(verdict),
        # Every cdr1 context field verbatim, including those the DTO has no
        # slot for (requesting / author org, plan activity, requester user).
        "context": ctx,
        "contextFound": d.get("context_found"),
        "contextFilledFromRaw": d.get("context_filled_from_raw") or [],
        "unavailable": unavailable,
        "hasBlockedSources": ips_client.has_any_active_block(sparr.blocks),
        "sparrChecked": sparr.checked,
    })
