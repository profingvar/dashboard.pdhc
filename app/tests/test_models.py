import os
import uuid
from datetime import datetime, timezone
from app import create_app
from app.models import db, User, OrgMembership, ObservationCache, RefreshLog


def _app():
    return create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": os.environ.get("DATABASE_URL"),
    })


def test_crud_cycle():
    app = _app()
    with app.app_context():
        u = User(username=f"t_{uuid.uuid4().hex[:8]}", is_su=True)
        db.session.add(u)
        db.session.commit()
        org = str(uuid.uuid4())
        db.session.add(OrgMembership(user_guid=u.guid, org_guid=org, role="admin"))
        obs = ObservationCache(
            source_obs_guid=str(uuid.uuid4()),
            patient_guid=str(uuid.uuid4()),
            org_guid=org,
            concept_guid=str(uuid.uuid4()),
            concept_name="B-glucose",
            value=5.4,
            unit="mmol/L",
            observed_at=datetime.now(timezone.utc),
        )
        rl = RefreshLog(user_guid=u.guid, org_guid=org, status="ok", rows_fetched=1)
        db.session.add_all([obs, rl])
        db.session.commit()

        assert User.query.filter_by(guid=u.guid).one().is_su
        assert OrgMembership.query.filter_by(user_guid=u.guid).count() == 1
        assert ObservationCache.query.filter_by(org_guid=org).count() >= 1
        assert RefreshLog.query.filter_by(user_guid=u.guid).count() == 1

        # cleanup
        ObservationCache.query.filter_by(org_guid=org).delete()
        RefreshLog.query.filter_by(user_guid=u.guid).delete()
        OrgMembership.query.filter_by(user_guid=u.guid).delete()
        User.query.filter_by(guid=u.guid).delete()
        db.session.commit()
