"""Workspace selector + nurse / researcher HTML shells.

Platform-plan execution §4.4.a / §4.7.b. The selector lets a user with
both roles pick which one they want; users with only one role go
straight there. Route guards are duplicated from the JSON-API decorators
since these are HTML pages a user navigates to.
"""
from __future__ import annotations

from flask import Blueprint, abort, g, redirect, render_template, url_for

from app.services.role_guards import _is_admin, _roles


bp = Blueprint("workspace", __name__)


def _user_roles_or_admin() -> set[str]:
    """Effective role set: admin gets both clinical roles."""
    roles = set(_roles())
    if _is_admin():
        roles |= {"nurse", "researcher"}
    return roles


@bp.get("/workspace")
def selector():
    """Pick a workspace. Sole-role users get redirected; both-role
    users see the chooser."""
    eff = _user_roles_or_admin()
    if not eff & {"nurse", "researcher"}:
        abort(403, description="no clinical workspace available for this user")
    if eff == {"nurse"}:
        return redirect(url_for("workspace.nurse_view"))
    if eff == {"researcher"}:
        return redirect(url_for("workspace.researcher_view"))
    return render_template("workspace_selector.html",
                           has_nurse=("nurse" in eff),
                           has_researcher=("researcher" in eff))


@bp.get("/nurse")
def nurse_view():
    if "nurse" not in _user_roles_or_admin():
        abort(403)
    return render_template("nurse_workspace.html")


@bp.get("/researcher")
def researcher_view():
    if "researcher" not in _user_roles_or_admin():
        abort(403)
    return render_template("researcher_workspace.html")
