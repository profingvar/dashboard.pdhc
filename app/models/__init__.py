import uuid
from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.dialects.postgresql import UUID, JSONB

db = SQLAlchemy()


def _uuid():
    return str(uuid.uuid4())


def _now():
    return datetime.now(timezone.utc)


class User(db.Model):
    __tablename__ = "users"
    guid = db.Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    username = db.Column(db.String(128), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=True)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    is_su = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)


class OrgMembership(db.Model):
    __tablename__ = "org_memberships"
    guid = db.Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_guid = db.Column(UUID(as_uuid=False), db.ForeignKey("users.guid"), nullable=False)
    org_guid = db.Column(UUID(as_uuid=False), nullable=False)
    role = db.Column(db.String(64), nullable=False, default="member")


class ObservationCache(db.Model):
    __tablename__ = "observation_cache"
    guid = db.Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    source_obs_guid = db.Column(UUID(as_uuid=False), unique=True, nullable=False)
    patient_guid = db.Column(UUID(as_uuid=False), nullable=False, index=True)
    org_guid = db.Column(UUID(as_uuid=False), nullable=False, index=True)
    concept_guid = db.Column(UUID(as_uuid=False), nullable=False, index=True)
    concept_name = db.Column(db.String(256), nullable=False)
    value = db.Column(db.Float, nullable=True)
    unit = db.Column(db.String(64), nullable=True)
    observed_at = db.Column(db.DateTime(timezone=True), nullable=False)
    raw = db.Column(JSONB, nullable=True)
    fetched_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)


class RefreshLog(db.Model):
    __tablename__ = "refresh_log"
    guid = db.Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_guid = db.Column(UUID(as_uuid=False), db.ForeignKey("users.guid"), nullable=False)
    org_guid = db.Column(UUID(as_uuid=False), nullable=False)
    started_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True)
    status = db.Column(db.String(32), nullable=False, default="running")
    rows_fetched = db.Column(db.Integer, nullable=False, default=0)
    error = db.Column(db.Text, nullable=True)


class Cohort(db.Model):
    """Persisted researcher cohort definition (Phase-4.5).

    Replaces the in-process ``_COHORTS`` dict in routes/researcher.py.
    Filter and member set are stored as JSONB so the cohort can be
    reused across gunicorn workers and across process restarts.

    The owner label is just the SSO-blob email-or-display-name string;
    we don't FK to ``users`` because service-key callers (sim.pdhc,
    monitor.pdhc) write cohorts too without ever appearing in users.
    """
    __tablename__ = "cohort"
    guid = db.Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    filter = db.Column(JSONB, nullable=False)
    members = db.Column(JSONB, nullable=False, default=list)
    n = db.Column(db.Integer, nullable=False, default=0)
    owner_label = db.Column(db.String(256), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)
