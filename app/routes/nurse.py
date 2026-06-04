"""Nurse workspace API — platform-plan execution §4.2.

Endpoints (all under ``/api/nurse``):

  GET /patient/<guid>                       — demographics + conditions +
                                               regimen + last-N summary
  GET /patient/<guid>/agp?window=14d|90d    — AGP shape: bands + summary
  GET /patient/<guid>/variable/<canonical>  — single-variable series,
                                               LTTB-downsampled to ≤ 2000
  GET /patient/<guid>/events                — hypo / encounter / med-change
                                               markers
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from flask import Blueprint, abort, current_app, g, jsonify, request

from app.services.audit import audit_read
from app.services.federation import (
    CdrRegistry,
    agp_hourly_bands,
    fanout,
    lttb_downsample,
    merge_agp_bands,
)
from app.services.role_guards import nurse_required


bp = Blueprint("nurse_api", __name__, url_prefix="/api/nurse")


def _registry() -> CdrRegistry:
    if not hasattr(current_app, "_cdr_registry"):
        current_app._cdr_registry = CdrRegistry.from_config(current_app.config)
    return current_app._cdr_registry


def _auth_headers() -> dict:
    blob = getattr(g, "access_blob", None) or {}
    if isinstance(blob, dict):
        is_admin = bool(blob.get("is_su_admin"))
        org_ids = blob.get("organization_ids") or []
    else:
        is_admin = bool(getattr(blob, "is_su_admin", False))
        org_ids = getattr(blob, "organization_ids", None) or []
    return {
        "is_admin": is_admin,
        "org_guids": ",".join(str(g) for g in (org_ids or [])),
    }


# ---------------------------------------------------------------------------
# GET /api/nurse/patient/<guid>
# ---------------------------------------------------------------------------

@bp.get("/patient/<guid>")
@nurse_required
@audit_read
def patient_summary(guid: str):
    """Find the owning CDR (the one that has this Patient) and return a
    rolled-up summary for the nurse view.

    We fan out a Patient read across all CDRs; the first ok response
    wins ("the owning CDR"). Then on that CDR we fetch
    ``$everything?_count=200&_since=<90d ago>`` to populate the
    near-term clinical picture.
    """
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

    conditions = []
    regimens = []
    last_obs: dict[str, dict] = {}  # canonical → most-recent obs
    for entry in everything_body.get("entry") or []:
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
        elif rt == "Observation":
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
    })


# ---------------------------------------------------------------------------
# GET /api/nurse/patient/<guid>/agp
# ---------------------------------------------------------------------------

@bp.get("/patient/<guid>/agp")
@nurse_required
@audit_read
def patient_agp(guid: str):
    """Ambulatory Glucose Profile for one patient.

    Calls the CDR's `Observation/$agp` SQL-aggregation operation
    instead of streaming raw CGM points back. Per-CDR responses are
    ~2KB regardless of window size; merge happens in
    federation.merge_agp_bands.

    Previously this route pulled `_count=30000` of CGM rows back from
    each CDR, hit the dashboard's 8s CDR_FANOUT_TIMEOUT on the 14-90 d
    window, and silently returned empty bands. See the cdr.pdhc
    fhir_read.py _agp_postgres comment for the SQL.
    """
    window = request.args.get("window", "14d")
    days = 14 if window == "14d" else 90
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    auth = _auth_headers()
    cgm_canonical = "https://termbank.pdhc.se/CodeSystem/loinc|41653-7"
    resp = fanout(
        _registry(),
        method="GET",
        path="api/v1/fhir/Observation/$agp",
        params={
            "patient": guid,
            "code": cgm_canonical,
            "date": f"ge{since}",
        },
        org_guids_header=auth["org_guids"],
        is_admin_header=auth["is_admin"],
    )

    merged = merge_agp_bands(resp.results)
    return jsonify({
        "guid": guid,
        "window": window,
        "fanout_mode": resp.mode,
        "succeeded_cdrs": resp.succeeded,
        "failed_cdrs": resp.failed,
        **merged,
    })


# ---------------------------------------------------------------------------
# GET /api/nurse/patient/<guid>/variable/<canonical>
# ---------------------------------------------------------------------------

@bp.get("/patient/<guid>/variable/<path:canonical>")
@nurse_required
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

    raw_points: list[tuple[float, float, str]] = []
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
                    raw_points.append((ts, float(val), eff))
    raw_points.sort(key=lambda p: p[0])

    xy = [(p[0], p[1]) for p in raw_points]
    sampled = lttb_downsample(xy, target=target)
    sampled_set = set(sampled)
    return jsonify({
        "guid": guid,
        "canonical": canonical,
        "n_raw": len(raw_points),
        "n_returned": len(sampled),
        "downsampled": len(sampled) < len(raw_points),
        "points": [
            {"t": p[0], "value": p[1]}
            for p in sampled
        ],
    })


# ---------------------------------------------------------------------------
# GET /api/nurse/patient/<guid>/events
# ---------------------------------------------------------------------------

@bp.get("/patient/<guid>/events")
@nurse_required
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
                })
    events.sort(key=lambda e: e.get("at") or e.get("start") or "")
    return jsonify({"guid": guid, "events": events, "n": len(events)})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
