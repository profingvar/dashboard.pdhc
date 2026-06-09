"""HTML views: landing (eligible patients) + patient dashboard."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from flask import Blueprint, render_template, request, abort, g, redirect, url_for, flash, current_app, session
from sqlalchemy import func
from app.models import db, ObservationCache, RefreshLog
from app.auth import scope_to_user_orgs, org_guids_for
from app.services.audit import audit_read
from app.services.gateway_client import refresh_org, GatewayClient
from app.services.ips_client import (
    get_active_blocks,
    filter_blocked_rows,
    has_any_active_block,
)

AUTO_REFRESH_INTERVAL = timedelta(minutes=5)

bp = Blueprint("views", __name__)


def _auto_refresh_if_stale():
    """Silently refresh from gateway if last refresh is older than AUTO_REFRESH_INTERVAL."""
    token = session.get("sso_token")
    if not token:
        return
    user = g.current_user
    orgs = list(getattr(user, "org_ids", []) or [])
    if not orgs:
        return
    last = (
        RefreshLog.query
        .filter(RefreshLog.user_guid == user.guid, RefreshLog.status == "ok")
        .order_by(RefreshLog.finished_at.desc())
        .first()
    )
    if last and last.finished_at and last.finished_at > datetime.now(timezone.utc) - AUTO_REFRESH_INTERVAL:
        return
    client = GatewayClient(token=token)
    for org in orgs:
        try:
            refresh_org(user.guid, org, client=client)
        except Exception:
            current_app.logger.debug("auto-refresh failed for %s (non-fatal)", org)


@bp.get("/")
@audit_read
def landing():
    _auto_refresh_if_stale()
    q = scope_to_user_orgs(ObservationCache.query, ObservationCache.org_guid)
    all_rows = q.all()

    patients = defaultdict(lambda: {"count": 0, "latest": None})
    for r in all_rows:
        p = patients[r.patient_guid]
        p["count"] += 1
        if p["latest"] is None or r.observed_at > p["latest"]:
            p["latest"] = r.observed_at

    patient_rows = sorted(
        [{"guid": k, **v} for k, v in patients.items()],
        key=lambda x: x["latest"] or 0, reverse=True,
    )

    # Tell the audit decorator how many patient rows were shown.
    g._audit_n_rows = len(patient_rows)
    return render_template("landing.html", patients=patient_rows)


@bp.get("/patient/<guid>")
@audit_read
def patient(guid):
    user = g.current_user
    user_orgs = list(getattr(user, "org_ids", []) or [])
    is_admin = bool(getattr(user, "is_admin", False))

    # Ticket #212. PDL Ch 4 §1 — no exception to need-to-know without an
    # explicit reason + audit trail. An SU admin reading a patient whose
    # data lives outside their own ``organization_ids`` used to silently
    # bypass the org filter. Now they must supply a written
    # justification, and a distinct audit-row shape records it verbatim.
    if is_admin:
        patient_orgs = {
            row[0] for row in (
                db.session.query(ObservationCache.org_guid)
                .filter_by(patient_guid=guid)
                .distinct()
                .all()
            )
            if row[0] is not None
        }
        # No overlap means the admin would have seen zero rows under the
        # normal in-org rule — that's the "off-org read" the lift covers.
        admin_off_org = bool(
            patient_orgs and not (patient_orgs & set(user_orgs))
        )
    else:
        patient_orgs = set()
        admin_off_org = False

    justification = (request.args.get("justification") or "").strip()

    if admin_off_org and not justification:
        # Confirmation step: render the lift form, log the attempt, do
        # NOT touch the patient's data. The audit row's event_type
        # records that an attempt happened so /admin/audit (#215) can
        # show off-org-read attempts even when no justification was
        # ever supplied.
        g._audit_event_type = "admin_override_required"
        g._audit_n_rows = 0
        return render_template(
            "admin_override_required.html",
            patient_guid=guid,
        )

    if admin_off_org and justification:
        g._audit_event_type = "admin_override"
        g._audit_admin_justification = justification

    q = ObservationCache.query.filter_by(patient_guid=guid)
    if not is_admin:
        if not user_orgs:
            abort(403)
        q = q.filter(ObservationCache.org_guid.in_(user_orgs))
    rows = q.order_by(ObservationCache.observed_at.asc()).all()
    if not rows:
        abort(404)

    # Spärr Phase 2 (ticket #205, PDL Ch 4 § 4). Drop rows whose source
    # is actively blocked, and surface the metadata-only banner when
    # the patient has *any* active block — even after org-scoping
    # already hid every blocked row.
    blocks = get_active_blocks(guid)
    rows = filter_blocked_rows(rows, blocks)
    has_blocked_sources = has_any_active_block(blocks)
    if not rows:
        # Every row was filtered by spärr. The patient *exists* and has
        # data, just not data this caller may see — present the banner
        # but no measurements.
        # (404 would be wrong: it would leak "no data" vs "data exists
        # but is blocked".)
        return render_template(
            "patient.html",
            patient_guid=guid,
            latest={}, series={}, concepts=[], selected=[],
            measures=[], graphs=[], has_blocked_sources=has_blocked_sources,
        )

    selected = [c for c in request.args.getlist("concept") if c]

    by_concept = defaultdict(list)
    latest = {}
    concept_names: dict[str, str] = {}
    graphs: list[dict] = []
    for r in rows:
        # Detect graph observations from raw FHIR extensions
        graph_ext = _extract_graph(r.raw) if r.raw else None
        if graph_ext:
            graphs.append({**graph_ext, "concept_name": r.concept_name,
                           "observed_at": r.observed_at.isoformat() if r.observed_at else None})
            continue
        by_concept[r.concept_guid].append({"x": r.observed_at.isoformat(), "y": r.value})
        concept_names.setdefault(r.concept_guid, r.concept_name)
        if r.concept_guid not in latest or r.observed_at > latest[r.concept_guid]["observed_at"]:
            latest[r.concept_guid] = {
                "name": r.concept_name, "value": r.value, "unit": r.unit,
                "observed_at": r.observed_at,
            }

    # If no concepts selected, show all by default
    show = selected if selected else list(by_concept.keys())
    series = {cg: {"name": concept_names.get(cg, cg), "points": by_concept[cg]} for cg in show if cg in by_concept}

    measures = sorted(latest.values(), key=lambda m: m["name"])

    # Audit row count = total observations returned (not just selected).
    g._audit_n_rows = len(rows)
    return render_template(
        "patient.html",
        patient_guid=guid,
        latest=latest,
        series=series,
        concepts=sorted(concept_names.items(), key=lambda x: x[1]),
        selected=selected,
        measures=measures,
        graphs=graphs,
        has_blocked_sources=has_blocked_sources,
    )


def _extract_graph(raw: dict) -> dict | None:
    """Extract provider graph data from FHIR Observation extensions."""
    for ext in (raw.get("extension") or []):
        if ext.get("url") != "urn:pdhc:fhir:extension:provider-graph":
            continue
        info = {}
        for sub in (ext.get("extension") or []):
            url = sub.get("url")
            if url == "graph-type":
                info["graph_type"] = sub.get("valueString")
            elif url == "graph-data":
                try:
                    info["graph_data"] = json.loads(sub.get("valueString", "[]"))
                except (json.JSONDecodeError, TypeError):
                    info["graph_data"] = []
            elif url == "graph-provider":
                info["graph_provider"] = sub.get("valueString")
            elif url == "graph-provider-url":
                info["graph_provider_url"] = sub.get("valueUrl")
        if info.get("graph_type") and info.get("graph_data"):
            return info
    return None


@bp.post("/refresh")
def refresh():
    user = g.current_user
    # Refresh always uses the user's actual SSO org memberships, even for
    # admins. (org_guids_for() returns [] for admins to mean "no filter"
    # on read queries — but for refresh we need a concrete list of orgs
    # to query gateway for.)
    orgs = list(getattr(user, "org_ids", []) or [])
    if not orgs:
        flash("No organisations to refresh — your SSO profile has no org memberships.", "warning")
        return redirect(url_for("views.landing"))

    # In AUTH_MODE=off there is no session SSO token; gateway is then
    # unreachable anyway, so just bail with a clear message.
    token = session.get("sso_token")
    if not token:
        flash("Cannot refresh: no SSO token in session (AUTH_MODE=off?).", "warning")
        return redirect(url_for("views.landing"))

    client = GatewayClient(token=token)
    ok = 0
    errs = []
    for org in orgs:
        try:
            log = refresh_org(user.guid, org, client=client)
            ok += getattr(log, "rows_fetched", 0) or 0
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception("refresh_org failed for %s", org)
            errs.append(f"{org[:8]}: {exc}")
    if errs:
        flash(f"Refresh errors: {'; '.join(errs)}", "danger")
    else:
        flash(f"Refresh ok — {ok} observations updated.", "success")
    return redirect(url_for("views.landing"))
