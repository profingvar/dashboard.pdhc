"""Workspace selector + nurse HTML shell.

Platform-plan execution §4.4.a / §4.7.b. Since #543 extracted the
group/population analyse-engine into the separate analyse.pdhc service,
the only clinical workspace this (cd-assist) service serves is the nurse
single-patient view. The selector therefore always routes straight to
the nurse workspace. Route guards are duplicated from the JSON-API
decorators since these are HTML pages a user navigates to.
"""
from __future__ import annotations

from flask import Blueprint, abort, redirect, render_template, url_for

from app.services.role_guards import _is_admin, _roles


bp = Blueprint("workspace", __name__)


def _has_nurse() -> bool:
    """Nurse role, or admin (admins satisfy any clinical role)."""
    return _is_admin() or "nurse" in set(_roles())


@bp.get("/workspace")
def selector():
    """Only the nurse workspace remains (#543), so route straight to it."""
    if not _has_nurse():
        abort(403, description="no clinical workspace available for this user")
    return redirect(url_for("workspace.nurse_view"))


@bp.get("/nurse")
def nurse_view():
    if not _has_nurse():
        abort(403)
    return render_template("nurse_workspace.html")
