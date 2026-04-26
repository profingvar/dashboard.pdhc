"""Auth + org-scoping.

AUTH_MODE=off  → loads a dev SU user (Rule 23, local dev only).
AUTH_MODE=sso  → OAuth-style flow against sso.pdhc, mirroring gateway.pdhc.

Validation is delegated to sso.pdhc /api/auth/me/service (no local HMAC),
matching gateway's working implementation.

Phase gate: dashboard belongs to the *analysis* phase. A user is granted
access if `is_su_admin` OR (`user_type == "professional"` AND
`"analysis" in effective_phases`).

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
        org_ids=list(blob.get("organization_ids") or []),
        blob=blob,
    )


def has_analysis_access(blob: Optional[dict]) -> bool:
    if not blob:
        return False
    if blob.get("is_su_admin"):
        return True
    return (
        blob.get("user_type") == "professional"
        and "analysis" in (blob.get("effective_phases") or [])
    )


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


def install_request_loader(app):
    """Install a single before_request that resolves the current user."""

    @app.before_request
    def _loader():  # noqa: ANN202
        if _public_path(request.path):
            return None
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
        if not has_analysis_access(blob):
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
