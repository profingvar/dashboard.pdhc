import uuid
import sqlalchemy
from app import create_app
from app.models import db, User, OrgMembership


def _app():
    app = create_app({
        "TESTING": True,
        # Hermetic per-test in-memory DB (#441). StaticPool is required:
        # bare sqlite :memory: gives each connection a private db, so
        # seeded rows would be invisible to request-handling connections.
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


def test_crud_cycle():
    # ObservationCache / RefreshLog were retired in #471 — this covers the
    # models that remain (User + OrgMembership).
    app = _app()
    with app.app_context():
        u = User(username=f"t_{uuid.uuid4().hex[:8]}", is_su=True)
        db.session.add(u)
        db.session.commit()
        org = str(uuid.uuid4())
        db.session.add(OrgMembership(user_guid=u.guid, org_guid=org, role="admin"))
        db.session.commit()

        assert User.query.filter_by(guid=u.guid).one().is_su
        assert OrgMembership.query.filter_by(user_guid=u.guid).count() == 1

        OrgMembership.query.filter_by(user_guid=u.guid).delete()
        User.query.filter_by(guid=u.guid).delete()
        db.session.commit()
