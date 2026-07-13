"""SavedDesign CRUD + owner isolation (#467 / #462 D5).

A design is a user-private reusable template. These tests assert the
happy-path CRUD for the signed-in user AND that one user can never see,
read, mutate, or delete another user's design (it reads back as 404, not
403, so existence isn't leaked).
"""
import uuid

import sqlalchemy

from app import create_app
from app.models import db, SavedDesign

# AUTH_MODE=off loads the dev SU blob; this is the owner the HTTP client acts as.
DEV_GUID = "00000000-0000-0000-0000-000000000000"


def _app():
    app = create_app({
        "TESTING": True,
        "DATABASE_URL": "sqlite:///:memory:",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {
            "connect_args": {"check_same_thread": False},
            "poolclass": sqlalchemy.pool.StaticPool,
        },
    })
    with app.app_context():
        db.create_all()
    return app


def test_crud_happy_path():
    app = _app()
    c = app.test_client()

    # empty to start
    r = c.get("/api/v1/designs")
    assert r.status_code == 200
    assert r.get_json()["designs"] == []

    # create
    r = c.post("/api/v1/designs", json={
        "name": "BP + weight",
        "spec": {"diagrams": [{"param": "c1", "mirror": "c2"}]},
    })
    assert r.status_code == 201
    d = r.get_json()
    guid = d["guid"]
    assert d["name"] == "BP + weight"
    assert d["spec"]["diagrams"][0]["mirror"] == "c2"

    # list shows it
    r = c.get("/api/v1/designs")
    assert [x["guid"] for x in r.get_json()["designs"]] == [guid]

    # get one
    r = c.get(f"/api/v1/designs/{guid}")
    assert r.status_code == 200 and r.get_json()["name"] == "BP + weight"

    # update name + spec
    r = c.put(f"/api/v1/designs/{guid}", json={
        "name": "renamed", "spec": {"diagrams": []},
    })
    assert r.status_code == 200
    assert r.get_json()["name"] == "renamed"
    assert r.get_json()["spec"] == {"diagrams": []}

    # delete
    r = c.delete(f"/api/v1/designs/{guid}")
    assert r.status_code == 200
    assert c.get("/api/v1/designs").get_json()["designs"] == []


def test_validation():
    app = _app()
    c = app.test_client()
    # blank name rejected
    assert c.post("/api/v1/designs", json={"name": "  "}).status_code == 400
    # non-object spec rejected
    assert c.post(
        "/api/v1/designs", json={"name": "x", "spec": [1, 2, 3]},
    ).status_code == 400
    # spec optional → defaults to {}
    r = c.post("/api/v1/designs", json={"name": "no-spec"})
    assert r.status_code == 201 and r.get_json()["spec"] == {}


def test_owner_isolation():
    app = _app()
    other_guid = str(uuid.uuid4())
    with app.app_context():
        foreign = SavedDesign(
            owner_user_guid=other_guid, name="not yours", spec={},
        )
        db.session.add(foreign)
        db.session.commit()
        foreign_guid = foreign.guid

    c = app.test_client()  # acts as DEV_GUID

    # not listed
    r = c.get("/api/v1/designs")
    assert foreign_guid not in [x["guid"] for x in r.get_json()["designs"]]
    # not readable / updatable / deletable — 404, not 403 (no existence leak)
    assert c.get(f"/api/v1/designs/{foreign_guid}").status_code == 404
    assert c.put(
        f"/api/v1/designs/{foreign_guid}", json={"name": "hijack"},
    ).status_code == 404
    assert c.delete(f"/api/v1/designs/{foreign_guid}").status_code == 404

    # the foreign row is untouched
    with app.app_context():
        still = SavedDesign.query.filter_by(guid=foreign_guid).one()
        assert still.name == "not yours"
        assert still.owner_user_guid == other_guid
