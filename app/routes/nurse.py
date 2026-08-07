"""Nurse workspace API — individual / point-of-care single-patient views.

Part of cd-assist (the individual clinical-decision-assist half of the old
dashboard — analyse-engine split #533, pivot 2026-08-07). Endpoints (all
under ``/api/nurse``):

  GET /patient/<guid>                       — demographics + conditions +
                                              regimen + last-N summary
  GET /patient/<guid>/agp?window=14d|90d    — AGP shape: bands + summary
  GET /patient/<guid>/variable/<canonical>  — single-variable series,
                                              LTTB-downsampled to <= 2000
  GET /patient/<guid>/events                — hypo / encounter markers

Two properties distinguish these from the analyse-engine (researcher) routes
(#546, the nurse-fold; salvaged from the discarded greenfield cd-assist):

1. Gate — these are CARE-DELIVERY routes, NOT analysis-phase. ``/api/nurse``
   is registered as a clinical path in ``app.auth._is_clinical_path``, so the
   request loader gates it on ``has_care_delivery_access`` (a care relationship
   — at least one care-unit scope — or SU admin), exactly like the /charts
   clinical view. Care-unit patient-scoping is forwarded to each CDR as
   ``X-Org-Guids`` / ``X-Is-Admin`` (``_auth_headers`` → ``scope_org_guids``).

2. Spärr — the nurse reads apply spärr per patient, mirroring
   ``app/routes/charts.py::series`` (operator #469 Q1 + #471.4/#472): fetch the
   patient's active blocks, then either the COARSE org-level drop (default) or
   the indispensable-care LIFT when ``SPARR_LIFT_ENABLED`` is set (with the
   ``sparr_lift_exposure`` audit event). Federation output is reshaped into
   spärr-ready points carrying ``org_guid`` (from
   ``Observation.performer[0].identifier.value``), ``code`` (the
   ``urn:pdhc:concept/<guid>`` canonical) and ``at`` (effectiveDateTime).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, current_app, g, jsonify, request

from app.services.audit import audit_read
from app.analyse.federation import (
    CdrRegistry,
    fanout,
    lttb_downsample,
    merge_agp_bands,
)
from app.analyse.aggregations import aggregate_per_cdr_results
from app.services.ips_client import (
    get_active_blocks,
    blocked_clinic_ids,
    has_any_active_block,
    filter_blocked_points,
    concept_guid_from_canonical,
)


bp = Blueprint("nurse_api", __name__, url_prefix="/api/nurse")


def _registry() -> CdrRegistry:
    if not hasattr(current_app, "_cdr_registry"):
        current_app._cdr_registry = CdrRegistry.from_config(current_app.config)
    return current_app._cdr_registry


def _auth_headers() -> dict:
    """Care-unit patient scope for the CDR fan-out.

    The care-delivery gate already validated the caller and resolved the
    Zone-1 care-unit scope; we forward it so each CDR enforces its own Rule 24
    / care-delivery filter. SU admins send ``X-Is-Admin`` and no org
    restriction."""
    from app.auth import scope_org_guids  # M0 #415
    blob = getattr(g, "access_blob", None) or {}
    if isinstance(blob, dict):
        is_admin = bool(blob.get("is_su_admin"))
        org_ids = scope_org_guids(blob)
    else:
        is_admin = bool(getattr(blob, "is_su_admin", False))
        org_ids = getattr(blob, "organization_ids", None) or []
    return {
        "is_admin": is_admin,
        "org_guids": ",".join(str(o) for o in (org_ids or [])),
    }


# ---------------------------------------------------------------------------
# Spärr — mirror charts.py::series (operator #469 Q1 / #471.4 / #472)
# ---------------------------------------------------------------------------

def _lift_exposure_audit(exposures):
    """Q4 (#472) audit detail: the lift reference for each exposure, aggregated
    by lift (blocked clinic + window). Reader + patient + role are already on
    the audit row; this adds *which* lift exposed *what*. Same shape as
    charts.py::_lift_exposure_audit."""
    by_lift: dict = {}
    for p, lift in exposures:
        key = (lift.source_scope_id, lift.lift_from_date, lift.lift_until_date)
        e = by_lift.setdefault(key, {
            "blocked_clinic": lift.source_scope_id,
            "lift_kind": lift.lift_kind,
            "lift_from_date": lift.lift_from_date,
            "lift_until_date": lift.lift_until_date,
            "exposed_concepts": set(),
            "exposed_points": 0,
        })
        cg = concept_guid_from_canonical(p.get("code") or p.get("code_canonical"))
        if cg:
            e["exposed_concepts"].add(cg)
        e["exposed_points"] += 1
    return [{**v, "exposed_concepts": sorted(v["exposed_concepts"])}
            for v in by_lift.values()]


def _apply_sparr(guid: str, points: list[dict]) -> tuple[list[dict], bool]:
    """Apply spärr to dict points, mirroring charts.py::series exactly.

    Each point must carry ``org_guid`` + ``code``/``code_canonical`` + ``at``.
    Two modes:
      - default (SPARR_LIFT_ENABLED off): COARSE org-level hide — drop every
        point from a blocked clinic (safe, over-hides).
      - SPARR_LIFT_ENABLED (#471.4, DPO-approved #472): apply the
        indispensable-care LIFT; a blocked point is exposed iff an active
        indispensable_care lift on its clinic covers its concept AND its date.
        Exposures raise the ``sparr_lift_exposure`` audit event.

    Returns ``(kept_points, has_blocked_sources)`` — the boolean drives the
    PDL Ch 4 §4 ¶3 metadata-only banner."""
    blocks = get_active_blocks(guid)
    blocked = blocked_clinic_ids(blocks)
    if blocked:
        if current_app.config.get("SPARR_LIFT_ENABLED"):
            points, exposures = filter_blocked_points(points, blocks)
            if exposures:
                g._audit_event_type = "sparr_lift_exposure"
                g._audit_payload_snapshot = {
                    "sparr_lift": _lift_exposure_audit(exposures),
                }
        else:
            points = [p for p in points if p.get("org_guid") not in blocked]
    return points, has_any_active_block(blocks)


def _obs_point(obs: dict, **extra) -> dict:
    """Reshape a FHIR Observation into a spärr-ready point.

    ``org_guid`` is the provider org the CDR ingest recorded on
    ``Observation.performer[0].identifier.value``; ``code`` is the
    ``urn:pdhc:concept/<guid>`` canonical (from ``_coding_uri`` on
    ``Observation.code``); ``at`` is the effective date. These are the three
    fields ``filter_blocked_points`` / the coarse drop key on."""
    return {
        "org_guid": _obs_org_guid(obs),
        "code": _coding_uri(obs.get("code")),
        "at": obs.get("effectiveDateTime"),
        **extra,
    }


def _sparr_filter_entries(guid: str, entries: list[dict]) -> tuple[list[dict], bool]:
    """Spärr-filter a list of FHIR Observation Bundle *entries*.

    Wraps ``_apply_sparr`` for the routes that work on raw Bundle entries
    (summary, agp) rather than already-projected points. Each entry is given a
    transient point view carrying ``_idx`` so kept points map back to their
    entry. Returns ``(kept_entries, has_blocked_sources)``."""
    pts = []
    for i, e in enumerate(entries):
        obs = e.get("resource") or {}
        pts.append(_obs_point(obs, _idx=i))
    kept, has_blocked = _apply_sparr(guid, pts)
    kept_entries = [entries[p["_idx"]] for p in kept]
    return kept_entries, has_blocked


# ---------------------------------------------------------------------------
# GET /api/nurse/patient/<guid>
# ---------------------------------------------------------------------------

@bp.get("/patient/<guid>")
@audit_read
def patient_summary(guid: str):
    """Owning-CDR lookup + rolled-up near-term clinical picture.

    We fan out a Patient read across all CDRs; the first ok response wins
    ("the owning CDR"). Then on that CDR we fetch
    ``$everything?_count=500&_since=<90d ago>``. The Observation entries are
    spärr-filtered before ``latest_values`` is derived; conditions / regimens
    are already care-unit-scoped by the CDR (X-Org-Guids)."""
    auth = _auth_headers()

    pat_resp = fanout(
        _registry(),
        method="GET",
        path=f"api/v1/fhir/Patient/{guid}",
        org_guids_header=auth["org_guids"],
        is_admin_header=auth["is_admin"],
    )
    owner = next((r for r in pat_resp.results if r.ok), None)
    if owner is None:
        abort(404, description="patient not found in any CDR")

    since = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    everything_resp = fanout(
        _registry(),
        method="GET",
        path=f"api/v1/fhir/Patient/{guid}/$everything",
        cdr_ids=[owner.cdr_id],
        params={"_since": since, "_count": "500"},
        org_guids_header=auth["org_guids"],
        is_admin_header=auth["is_admin"],
    )
    everything_body = next(
        (r.body for r in everything_resp.results if r.ok and isinstance(r.body, dict)),
        {"entry": []},
    )

    entries = everything_body.get("entry") or []
    # Spärr the Observation entries; non-Observation entries pass through the
    # filter unchanged (no org_guid → never in a blocked clinic set).
    obs_entries = [e for e in entries
                   if (e.get("resource") or {}).get("resourceType") == "Observation"]
    other_entries = [e for e in entries
                     if (e.get("resource") or {}).get("resourceType") != "Observation"]
    kept_obs, has_blocked = _sparr_filter_entries(guid, obs_entries)

    conditions = []
    regimens = []
    last_obs: dict[str, dict] = {}  # canonical → most-recent obs
    for entry in other_entries:
        r = entry.get("resource") or {}
        rt = r.get("resourceType")
        if rt == "Condition":
            conditions.append({
                "canonical": _coding_uri(r.get("code")),
                "display": _coding_display(r.get("code")),
                "onset": r.get("onsetDateTime"),
                "status": (r.get("clinicalStatus") or {}).get("coding", [{}])[0].get("code"),
            })
        elif rt == "MedicationStatement":
            regimens.append({
                "canonical": _coding_uri(r.get("medicationCodeableConcept")),
                "display": _coding_display(r.get("medicationCodeableConcept")),
                "start": (r.get("effectivePeriod") or {}).get("start"),
                "status": r.get("status"),
            })
    for entry in kept_obs:
        r = entry.get("resource") or {}
        canonical = _coding_uri(r.get("code"))
        eff = r.get("effectiveDateTime") or ""
        existing = last_obs.get(canonical)
        if not existing or eff > existing.get("effective", ""):
            last_obs[canonical] = {
                "canonical": canonical,
                "display": _coding_display(r.get("code")),
                "value": (r.get("valueQuantity") or {}).get("value"),
                "unit": (r.get("valueQuantity") or {}).get("unit"),
                "effective": eff,
            }

    return jsonify({
        "patient": owner.body,
        "owner_cdr": owner.cdr_id,
        "owner_region": owner.region_label,
        "conditions": conditions,
        "regimen": regimens,
        "latest_values": list(last_obs.values()),
        "has_blocked_sources": has_blocked,
    })


# ---------------------------------------------------------------------------
# GET /api/nurse/patient/<guid>/agp
# ---------------------------------------------------------------------------

@bp.get("/patient/<guid>/agp")
@audit_read
def patient_agp(guid: str):
    """Ambulatory Glucose Profile for one patient.

    Fetches raw CGM Observations from each federated CDR, spärr-filters the
    per-CDR bundles, then runs the per-CDR ``compute_agp`` aggregation and
    merges the resulting Parameters. Spärr is applied to the raw points BEFORE
    aggregation so a blocked clinic never contributes to the merged bands."""
    window = request.args.get("window", "14d")
    days = 14 if window == "14d" else 90
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    auth = _auth_headers()
    cgm_canonical = "https://termbank.pdhc.se/CodeSystem/loinc|41653-7"
    raw = fanout(
        _registry(),
        method="GET",
        path="api/v1/fhir/Observation",
        params={
            "patient": guid,
            "code": cgm_canonical,
            "date": f"ge{since}",
            "_count": "30000",
        },
        org_guids_header=auth["org_guids"],
        is_admin_header=auth["is_admin"],
    )

    # Spärr each per-CDR bundle's Observation entries before aggregation.
    has_blocked = False
    for r in raw.results:
        if not (r.ok and isinstance(r.body, dict)):
            continue
        kept, hb = _sparr_filter_entries(guid, r.body.get("entry") or [])
        has_blocked = has_blocked or hb
        r.body = {**r.body, "entry": kept}

    per_cdr_params = aggregate_per_cdr_results(raw.results, kind="agp")

    merged = merge_agp_bands(per_cdr_params)
    return jsonify({
        "guid": guid,
        "window": window,
        "fanout_mode": raw.mode,
        "succeeded_cdrs": raw.succeeded,
        "failed_cdrs": raw.failed,
        "has_blocked_sources": has_blocked,
        **merged,
    })


# ---------------------------------------------------------------------------
# GET /api/nurse/patient/<guid>/variable/<canonical>
# ---------------------------------------------------------------------------

@bp.get("/patient/<guid>/variable/<path:canonical>")
@audit_read
def patient_variable(guid: str, canonical: str):
    auth = _auth_headers()
    target = int(request.args.get("max", "2000"))
    days = int(request.args.get("days", "365"))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    code_arg = canonical
    if "|" not in code_arg:
        # accept "url/<system>/<code>" → "<system>|<code>" too
        last = canonical.rsplit("/", 1)
        if len(last) == 2:
            code_arg = f"{last[0]}|{last[1]}"

    resp = fanout(
        _registry(),
        method="GET",
        path="api/v1/fhir/Observation",
        params={
            "patient": guid,
            "code": code_arg,
            "date": f"ge{since}",
            "_count": "5000",
        },
        org_guids_header=auth["org_guids"],
        is_admin_header=auth["is_admin"],
    )

    # Build spärr-ready dict points (org_guid/code/at) that also carry the
    # numeric (t, value) needed for downsampling.
    points: list[dict] = []
    for r in resp.results:
        if not r.ok or not isinstance(r.body, dict):
            continue
        for entry in r.body.get("entry") or []:
            obs = entry.get("resource") or {}
            eff = obs.get("effectiveDateTime")
            val = (obs.get("valueQuantity") or {}).get("value")
            if eff and val is not None:
                ts = _parse_iso(eff)
                if ts is not None:
                    points.append(_obs_point(obs, t=ts, value=float(val)))

    points, has_blocked = _apply_sparr(guid, points)
    points.sort(key=lambda p: p["t"])

    xy = [(p["t"], p["value"]) for p in points]
    sampled = lttb_downsample(xy, target=target)
    return jsonify({
        "guid": guid,
        "canonical": canonical,
        "n_raw": len(points),
        "n_returned": len(sampled),
        "downsampled": len(sampled) < len(points),
        "has_blocked_sources": has_blocked,
        "points": [
            {"t": p[0], "value": p[1]}
            for p in sampled
        ],
    })


# ---------------------------------------------------------------------------
# GET /api/nurse/patient/<guid>/events
# ---------------------------------------------------------------------------

@bp.get("/patient/<guid>/events")
@audit_read
def patient_events(guid: str):
    auth = _auth_headers()
    days = int(request.args.get("days", "180"))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    # Encounters and hypo-event Observations.
    enc = fanout(
        _registry(),
        method="GET",
        path="api/v1/fhir/Encounter",
        params={"patient": guid, "date": f"ge{since}", "_count": "200"},
        org_guids_header=auth["org_guids"],
        is_admin_header=auth["is_admin"],
    )
    # 104642-4 = "Time below range, very low" (severe hypoglycemia, <3.0 mmol/L)
    # — matches sim.pdhc's cgm_hypo_count concept code.
    hypo_canonical = "https://termbank.pdhc.se/CodeSystem/loinc|104642-4"
    hypo = fanout(
        _registry(),
        method="GET",
        path="api/v1/fhir/Observation",
        params={"patient": guid, "code": hypo_canonical,
                "date": f"ge{since}", "_count": "1000"},
        org_guids_header=auth["org_guids"],
        is_admin_header=auth["is_admin"],
    )

    events: list[dict] = []
    for r in enc.results:
        if not r.ok or not isinstance(r.body, dict):
            continue
        for entry in r.body.get("entry") or []:
            res = entry.get("resource") or {}
            period = res.get("period") or {}
            events.append({
                "kind": "encounter",
                "start": period.get("start"),
                "end": period.get("end"),
                "class": (res.get("class") or {}).get("coding", [{}])[0].get("code"),
                "display": _coding_display(res.get("code")),
                "cdr_id": r.cdr_id,
                # spärr keys — encounters carry no provider org / concept code,
                # so they are never dropped by a clinic block (safe pass-through).
                "org_guid": _obs_org_guid(res),
                "code": _coding_uri(res.get("code")),
                "at": period.get("start"),
            })
    for r in hypo.results:
        if not r.ok or not isinstance(r.body, dict):
            continue
        for entry in r.body.get("entry") or []:
            res = entry.get("resource") or {}
            count = (res.get("valueQuantity") or {}).get("value")
            if count and float(count) > 0:
                events.append({
                    "kind": "hypo",
                    "at": res.get("effectiveDateTime"),
                    "count": count,
                    "cdr_id": r.cdr_id,
                    "org_guid": _obs_org_guid(res),
                    "code": _coding_uri(res.get("code")),
                })

    events, has_blocked = _apply_sparr(guid, events)
    events.sort(key=lambda e: e.get("at") or e.get("start") or "")
    # Strip the transient spärr keys from the wire shape (keep cdr_id).
    for e in events:
        e.pop("org_guid", None)
        if e.get("kind") == "hypo":
            e.pop("code", None)
    return jsonify({
        "guid": guid,
        "events": events,
        "n": len(events),
        "has_blocked_sources": has_blocked,
    })


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _obs_org_guid(res: dict | None) -> str | None:
    """Provider org GUID from ``performer[0].identifier.value`` — the field the
    CDR ingest pipeline records org_guid from (cdr.pdhc ingest_pipeline)."""
    if not res:
        return None
    for performer in (res.get("performer") or []):
        ident = (performer.get("identifier") or {}).get("value")
        if ident:
            return ident
    return None


def _coding_uri(cc: dict | None) -> str | None:
    if not cc:
        return None
    codings = cc.get("coding") or []
    if not codings:
        return None
    sys_uri = codings[0].get("system") or ""
    code = codings[0].get("code") or ""
    if sys_uri and code:
        return f"{sys_uri.rstrip('/')}/{code}"
    return code or None


def _coding_display(cc: dict | None) -> str | None:
    if not cc:
        return None
    codings = cc.get("coding") or []
    if codings and codings[0].get("display"):
        return codings[0]["display"]
    return cc.get("text")


def _parse_iso(s: str) -> float | None:
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None
