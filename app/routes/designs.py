"""Saved designs — user-private reusable dashboard templates (#467 / #462 D5).

CRUD over ``SavedDesign``. Every operation is scoped to the caller's own
``owner_user_guid`` (the SSO user_guid); there is no cross-user or admin
view — a design is a personal view config (operator #469 Q3), not patient
data, so the #212 admin-override machinery deliberately does not apply
here. A design that belongs to another user reads back as 404 (never 403)
so the endpoint doesn't leak which guids exist.

The ``spec`` is opaque JSON owned by the frontend (the diagram list); the
backend only checks it is a JSON object so the charting shape (D4/#466)
can evolve without touching this module.
"""
from __future__ import annotations

from flask import Blueprint, g, jsonify, request

from app.models import db, SavedDesign

bp = Blueprint("designs", __name__, url_prefix="/api/v1/designs")

_MAX_NAME = 200


def _owner() -> str:
    """The current caller's stable identity used as the design owner."""
    return getattr(g.current_user, "guid", None) or ""


def _clean_name(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    if not name or len(name) > _MAX_NAME:
        return None
    return name


def _clean_spec(raw):
    """spec must be a JSON object; absent → empty dict."""
    if raw is None:
        return {}
    return raw if isinstance(raw, dict) else None


def _get_owned_or_none(guid: str) -> SavedDesign | None:
    return SavedDesign.query.filter_by(
        guid=guid, owner_user_guid=_owner(),
    ).one_or_none()


@bp.get("")
def list_designs():
    owner = _owner()
    rows = (
        SavedDesign.query
        .filter_by(owner_user_guid=owner)
        .order_by(SavedDesign.updated_at.desc())
        .all()
    )
    return jsonify(designs=[r.to_dict() for r in rows]), 200


@bp.post("")
def create_design():
    body = request.get_json(silent=True) or {}
    name = _clean_name(body.get("name"))
    if name is None:
        return jsonify(error="name is required (1..200 chars)"), 400
    spec = _clean_spec(body.get("spec"))
    if spec is None:
        return jsonify(error="spec must be a JSON object"), 400
    design = SavedDesign(owner_user_guid=_owner(), name=name, spec=spec)
    db.session.add(design)
    db.session.commit()
    return jsonify(design.to_dict()), 201


@bp.get("/<guid>")
def get_design(guid):
    design = _get_owned_or_none(guid)
    if design is None:
        return jsonify(error="not found"), 404
    return jsonify(design.to_dict()), 200


@bp.put("/<guid>")
def update_design(guid):
    design = _get_owned_or_none(guid)
    if design is None:
        return jsonify(error="not found"), 404
    body = request.get_json(silent=True) or {}
    if "name" in body:
        name = _clean_name(body.get("name"))
        if name is None:
            return jsonify(error="name is required (1..200 chars)"), 400
        design.name = name
    if "spec" in body:
        spec = _clean_spec(body.get("spec"))
        if spec is None:
            return jsonify(error="spec must be a JSON object"), 400
        design.spec = spec
    db.session.commit()
    return jsonify(design.to_dict()), 200


@bp.delete("/<guid>")
def delete_design(guid):
    design = _get_owned_or_none(guid)
    if design is None:
        return jsonify(error="not found"), 404
    db.session.delete(design)
    db.session.commit()
    return jsonify(deleted=guid), 200
