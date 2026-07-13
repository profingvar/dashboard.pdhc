"""Auth + org-scoping.

AUTH_MODE=off  → loads a dev SU user (Rule 23, local dev only).
AUTH_MODE=sso  → OAuth-style flow against sso.pdhc, mirroring gateway.pdhc.

Validation is delegated to sso.pdhc /api/auth/me/service (no local HMAC),
matching gateway's working implementation.

Access gate (#463/D1, route-aware): the clinical dashboard's own routes
(/, /select, /patient/*, /api/v1/designs, /refresh) use the CARE-DELIVERY
gate — `is_su_admin` OR (professional with a care relationship, i.e. a
care-unit scope). The (soon-to-relocate to analyse.pdhc) analyse engine
routes keep the legacy *analysis* phase gate: `is_su_admin` OR
(`user_type == "professional"` AND `"analysis" in effective_phases`).

Rule 24: non-admin users only see ObservationCache rows whose org_guid is
in their `organization_ids` blob field.
"""
from __future__ import annotations

import hashlib
from functools import wraps
from types import SimpleNamespace
from typing import Optional

import click
import requests
from flask import current_app, g, request, session, redirect, url_for, abort

from app.models import db, User


# ---------- helpers ----------

def _hash(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


_DEV_BLOB = {
    "user_guid": "00000000-0000-0000-0000-000000000000",
    "email": "dev@local",
    "display_name": "Dev SU",
    "user_type": "professional",
    "is_su_admin": True,
    "effective_phases": ["analysis"],
    "organization_ids": [],
}


def _blob_to_user(blob: dict) -> SimpleNamespace:
    """Lightweight wrapper exposing the attributes routes expect."""
    return SimpleNamespace(
        guid=blob.get("user_guid"),
        username=blob.get("email") or blob.get("user_guid"),
        is_admin=bool(blob.get("is_su_admin")),
        is_su=bool(blob.get("is_su_admin")),
        org_ids=scope_org_guids(blob),
        blob=blob,
    )


def _phases(blob: dict) -> list:
    """Reform-canonical phases (M0 #415): prefer session_phases (Option C),
    fall back to the dual-emitted legacy effective_phases for pre-reform
    tokens."""
    return blob.get("session_phases") or blob.get("effective_phases") or []


def scope_org_guids(blob: dict) -> list:
    """Zone-1 read scope (M0 #415): affiliations[].care_unit_guid — the exact
    equivalent of the legacy flat organization_ids semantics — with a dual-read
    fallback to organization_ids for pre-reform tokens. Public so route modules
    (nurse.py) share the same derivation. Zone-2 (parent care organisation) is
    deliberately NOT folded in here."""
    affs = blob.get("affiliations") or []
    if affs:
        return [a["care_unit_guid"] for a in affs if a.get("care_unit_guid")]
    return list(blob.get("organization_ids") or [])


def research_project_guids(blob: dict) -> list:
    """Reader-side research scope (M0 #415): the union of
    research_project_guids across the caller's affiliations, order-preserving.
    Only researcher affiliations carry projects (S9/#400), so no role filter
    is applied here. Intersected against the patient's
    consented_research_projects by ips's analysis-filter — never locally."""
    seen: set = set()
    out: list = []
    for a in blob.get("affiliations") or []:
        for guid in a.get("research_project_guids") or []:
            if guid not in seen:
                seen.add(guid)
                out.append(guid)
    return out


def has_analysis_access(blob: Optional[dict]) -> bool:
    if not blob:
        return False
    if blob.get("is_su_admin"):
        return True
    return (
        blob.get("user_type") == "professional"
        and "analysis" in _phases(blob)
    )


# ---- #463 / #462 D1: care-delivery front door ----------------------------
#
# The rebuilt dashboard is a CLINICAL tool. Its own routes must be reachable
# by a treating clinician WITHOUT the 'analysis' phase — access is a care
# relationship, not an analysis grant. The (soon-to-relocate to analyse.pdhc)
# analyse engine keeps the analysis-phase gate. Rather than touch every
# analyse route, the SSO gate is route-aware: clinical paths → care-delivery,
# everything else → analysis phase (unchanged).

def _is_clinical_path(path: str) -> bool:
    """The clinical dashboard's own routes (care-delivery gated)."""
    return (
        path == "/"
        or path == "/refresh"
        or path.startswith("/select")
        or path.startswith("/patient/")
        or path.startswith("/api/v1/patient/")
        or path.startswith("/api/v1/designs")
    )


def has_care_delivery_access(blob: Optional[dict]) -> bool:
    """Care-delivery baseline for the clinical dashboard (#463/D1).

    Access = an SU admin, OR a professional with a care relationship — i.e.
    at least one care-unit scope (affiliations[].care_unit_guid, dual-read
    to legacy organization_ids via ``scope_org_guids``). This REPLACES the
    analysis-phase gate as the dashboard's front door: a treating clinician
    no longer needs the 'analysis' phase to view their own patients. Spärr
    is enforced per-patient on the read side (operator #469 Q1); the
    vårdrelation model is confirmed with legal (#437)."""
    if not blob:
        return False
    if blob.get("is_su_admin"):
        return True
    if blob.get("user_type") != "professional":
        return False
    return bool(scope_org_guids(blob))


def _dashboard_access_allowed(path: str, blob: Optional[dict]) -> bool:
    """Route-aware gate (#463/D1): clinical routes need care-delivery
    access; the analyse engine's routes keep the analysis-phase gate until
    they relocate to analyse.pdhc."""
    if _is_clinical_path(path):
        return has_care_delivery_access(blob)
    return has_analysis_access(blob)


# ---------- SSO calls ----------

def validate_sso_token(token: str) -> Optional[dict]:
    """Call sso.pdhc /api/auth/me/service with the bearer + service creds.
    Returns the access blob or None on failure. Mirrors gateway."""
    base = current_app.config.get("SSO_BASE_URL", "").rstrip("/")
    cid = current_app.config.get("SSO_CLIENT_ID", "")
    sec = current_app.config.get("SSO_CLIENT_SECRET", "")
    if not (base and cid and sec):
        current_app.logger.error("SSO config missing (BASE_URL/CLIENT_ID/CLIENT_SECRET)")
        return None
    try:
        r = requests.get(
            f"{base}/api/auth/me/service",
            headers={
                "Authorization": f"Bearer {token}",
                "X-SSO-Client-Id": cid,
                "X-SSO-Client-Secret": sec,
            },
            timeout=10,
            verify=True,
        )
        if r.status_code == 200:
            return r.json()
        current_app.logger.warning("SSO validate failed: %s %s", r.status_code, r.text[:200])
        return None
    except requests.RequestException as e:
        current_app.logger.error("SSO validate error: %s", e)
        return None


def initiate_sso_login(next_url: str, state: str) -> str:
    base = current_app.config.get("SSO_BASE_URL", "").rstrip("/")
    cb = current_app.config.get("SSO_CALLBACK_URL", "")
    return f"{base}/login?next={cb}&state={state}"


# ---------- per-request loader ----------

def _upsert_local_user(blob: dict) -> None:
    """Make sure a local users row exists so RefreshLog FK works."""
    guid = blob.get("user_guid")
    if not guid:
        return
    u = User.query.filter_by(guid=guid).first()
    if not u:
        u = User(
            guid=guid,
            username=blob.get("email") or guid,
            is_admin=bool(blob.get("is_su_admin")),
            is_su=bool(blob.get("is_su_admin")),
        )
        db.session.add(u)
        db.session.commit()


def _public_path(path: str) -> bool:
    return (
        path.startswith("/auth/")
        or path == "/healthz"
        or path == "/metadata"
        or path.startswith("/static/")
    )


# Service-key auth: trusted sibling tools (perf benchmarks, monitoring,
# CI smoke tests) may call dashboard APIs without an SSO session.
# Ticket #291: gateway.pdhc joins as the analyse-pull caller — its
# /api/v1/observations proxy now lands here instead of cdr1.
KNOWN_SERVICES = {
    "monitor.pdhc": "MONITOR_PDHC_SERVICE_KEY",
    "gateway.pdhc": "GATEWAY_PDHC_SERVICE_KEY",
}


def _service_key_outcome(app):
    """None / True / False — same shape as cdr.pdhc's _service_key_outcome."""
    source = request.headers.get("X-Source-Service", "").strip()
    key = request.headers.get("X-Service-Key", "").strip()
    if not source and not key:
        return None
    if not source or not key:
        return False
    cfg_var = KNOWN_SERVICES.get(source)
    if not cfg_var:
        return False
    expected = app.config.get(cfg_var, "")
    if not expected or key != expected:
        return False
    g.source_service = source
    return True


def _service_blob(source_service: str) -> dict:
    """Machine identity for service-key callers (M0 #415).

    Pre-reform this was a synthetic SU blob with roles=[nurse, researcher,
    admin] so any route would pass — a stopgap the reform removes. Service
    callers now carry NO clinical roles and NO admin bit: they can only
    reach the analyse-layer service endpoints, which gate on
    ``service_source`` explicitly (observations_search / stats / canonical,
    #291/#292). Clinical UI routes (nurse/researcher/admin guards) require
    a real operator session with affiliations[]."""
    return {
        "user_guid": f"00000000-0000-0000-0000-service-{source_service[:8]}",
        "email": f"service:{source_service}",
        "display_name": f"service:{source_service}",
        "user_type": "service",
        "is_su_admin": False,
        "session_phases": ["analysis"],
        "organization_ids": [],
        "affiliations": [],
        "service_source": source_service,
    }


def install_request_loader(app):
    """Install a single before_request that resolves the current user."""

    @app.before_request
    def _loader():  # noqa: ANN202
        if _public_path(request.path):
            return None
        sk = _service_key_outcome(app)
        if sk is True:
            blob = _service_blob(g.source_service)
            g.access_blob = blob
            g.current_user = _blob_to_user(blob)
            return None
        if sk is False:
            from flask import jsonify
            return jsonify({"error": "Invalid service credentials"}), 403
        mode = app.config.get("AUTH_MODE", "off")
        if mode == "off":
            g.access_blob = _DEV_BLOB
            g.current_user = _blob_to_user(_DEV_BLOB)
            return None
        # sso — always re-validate the bearer with sso.pdhc so that a
        # logout on the SSO side immediately invalidates this dashboard
        # session too. (Caching the blob locally would let users stay
        # logged in here after revoking their token there.)
        token = session.get("sso_token")
        if not token:
            session["sso_next"] = request.url
            return redirect(url_for("auth.login"))
        blob = validate_sso_token(token)
        if not blob:
            session.clear()
            session["sso_next"] = request.url
            return redirect(url_for("auth.login"))
        session["access_blob"] = blob
        # Ticket #51 / SSO #43: forced password reset — bounce to SSO's
        # change-password page until SSO clears the flag on the next blob.
        if blob.get("must_change_password"):
            base = app.config.get("SSO_BASE_URL", "").rstrip("/")
            return redirect(f"{base}/change-password")
        if not _dashboard_access_allowed(request.path, blob):
            abort(403)
        g.access_blob = blob
        g.current_user = _blob_to_user(blob)
        return None


# ---------- org scoping (Rule 24) ----------

def org_guids_for(user) -> list[str]:
    if getattr(user, "is_admin", False):
        return []  # empty = no restriction
    return list(getattr(user, "org_ids", []) or [])


def scope_to_user_orgs(query, model_attr):
    """Apply org filter on a SQLAlchemy query unless user is admin."""
    user = g.current_user
    if user.is_admin:
        return query
    orgs = org_guids_for(user)
    if not orgs:
        return query.filter(model_attr == "__none__")
    return query.filter(model_attr.in_(orgs))


# back-compat shim used by existing routes; before_request already loaded
def load_user():  # noqa: D401
    """No-op kept for back-compat with existing route imports."""
    return None


# ---------- CLI: bootstrap SU (Rule 23) ----------

def register_cli(app):
    @app.cli.command("create-su")
    @click.option("--username", required=True)
    @click.option("--password", required=True)
    def create_su(username, password):
        existing = User.query.filter_by(username=username).first()
        if existing:
            existing.is_su = True
            existing.is_admin = True
            existing.password_hash = _hash(password)
        else:
            db.session.add(User(
                username=username,
                password_hash=_hash(password),
                is_su=True, is_admin=True,
            ))
        db.session.commit()
        click.echo(f"SU {username} ready")
