"""Role-based decorators for the dashboard's nurse / researcher / admin
workspaces. Platform-plan execution §4.7.

Roles are read from the SSO blob (``g.access_blob`` populated by
``app.auth``). A user may hold one or both clinical roles. Admins
satisfy any role check.

Usage::

    @bp.get("/api/nurse/patient/<guid>")
    @nurse_required
    def nurse_patient(guid):
        ...

The decorators are intentionally Flask-aware (they call ``flask.abort``)
so handlers stay tiny.
"""
from __future__ import annotations

from functools import wraps

from flask import abort, g


def _roles() -> set[str]:
    """Read the caller's roles. Returns ``set()`` if no blob is loaded.

    M0 #415: roles derive from affiliations[].role (the reform-canonical
    source — one role per active affiliation), with a dual-read fallback
    to the legacy flat roles[] for pre-reform tokens. Service-key callers
    carry neither, so every role guard denies them by construction."""
    blob = getattr(g, "access_blob", None) or {}
    if not isinstance(blob, dict):
        # When AUTH_MODE=off the blob is a dataclass-shaped namespace; both
        # interfaces expose .get / dict access. Fall back to attr lookup.
        roles = getattr(blob, "roles", None) or set()
        if isinstance(roles, (list, tuple, set)):
            return {str(r) for r in roles}
        return set()
    affs = blob.get("affiliations") or []
    if affs:
        return {str(a["role"]).lower() for a in affs if a.get("role")}
    roles = blob.get("roles") or []
    if isinstance(roles, str):
        roles = [roles]
    return {str(r) for r in roles}


def _is_admin() -> bool:
    blob = getattr(g, "access_blob", None) or {}
    if isinstance(blob, dict):
        return bool(blob.get("is_su_admin")) or "admin" in _roles()
    return bool(getattr(blob, "is_su_admin", False)) or "admin" in _roles()


def nurse_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if _is_admin() or "nurse" in _roles():
            return fn(*args, **kwargs)
        abort(403, description="nurse role required")
    return wrapper


def researcher_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if _is_admin() or "researcher" in _roles():
            return fn(*args, **kwargs)
        abort(403, description="researcher role required")
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if _is_admin():
            return fn(*args, **kwargs)
        abort(403, description="admin role required")
    return wrapper
