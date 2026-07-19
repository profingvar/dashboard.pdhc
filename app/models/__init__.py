import uuid
from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import JSON, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB as _PG_JSONB

# Dialect-aware JSON: JSONB on Postgres (production — unchanged, no
# migration), plain JSON on SQLite so the hermetic test suite can
# create_all() (#415; same pattern as cdr_app/app/models/resources.py).
JSONB = JSON().with_variant(_PG_JSONB(astext_type=Text()), "postgresql")

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


# ObservationCache + RefreshLog were removed in #471 (the legacy gateway->cache
# clinical surface was retired; operator #469 Q6 = live CDR1 reads only). The
# tables are dropped by migration drop0719cache01; prod data was backed up to
# ~/backups/predeploy/dashboard/ first.


class DashboardAudit(db.Model):
    """PDL Ch 4 §3 kontroller log — one row per patient-touching read
    via the dashboard.

    Ticket #211. Replaces the file-based researcher-export audit at
    ``results/export_audit.log`` (which only covered §4.6 exports) with
    a Postgres-backed table that covers every read.

    Columns:
      - ``user_guid``: SSO ``user_guid`` of the caller, or a synthetic
        ``00000000-...-service-<svc>`` for service-key callers
        (sim.pdhc, monitor.pdhc).
      - ``user_org_guids``: snapshot of the caller's ``organization_ids``
        at read time (used to attribute the read to a vårdenhet).
      - ``route``: ``"<METHOD> <rule>"``, e.g. ``"GET /patient/<guid>"``.
        Stored at the rule level (not the materialised URL) so the
        same logical action aggregates cleanly.
      - ``patient_guid``: the single patient touched, or NULL for
        cohort-level reads (researcher aggregates touch many patients;
        ``n_rows_returned`` is the right denominator there).
      - ``n_rows_returned``: best-effort count of patient-data rows in
        the response body; NULL for streamed responses or when the
        shape can't be inferred. Routes may set ``g._audit_n_rows`` to
        override.
      - ``response_status``: HTTP code returned to the caller. Includes
        4xx; 5xx pre-DB are NOT logged (we'd have no audit context).
      - ``session_id``: SSO session id, set once Phase 3 / #191 ships;
        nullable until then.
    """
    __tablename__ = "dashboard_audit"
    guid = db.Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    timestamp = db.Column(db.DateTime(timezone=True), default=_now, nullable=False, index=True)
    user_guid = db.Column(db.String(128), nullable=True, index=True)
    user_org_guids = db.Column(JSONB, nullable=False, default=list)
    route = db.Column(db.String(256), nullable=False, index=True)
    patient_guid = db.Column(UUID(as_uuid=False), nullable=True, index=True)
    n_rows_returned = db.Column(db.Integer, nullable=True)
    response_status = db.Column(db.Integer, nullable=False)
    session_id = db.Column(db.String(128), nullable=True, index=True)
    # Ticket #214: arbitrary per-event details. Currently used by the
    # researcher CSV export to carry export_id + cohort_id + variables;
    # nullable for every other route so the existing @audit_read
    # decorator stays unaffected.
    payload_snapshot = db.Column(JSONB, nullable=True)
    # Ticket #212: SU-admin off-org reads become an explicit, audited
    # lift instead of a silent bypass.
    #   event_type: 'read' (default) | 'admin_override_required' (admin
    #     tried to view a patient outside their orgs without a
    #     justification — the view rendered the confirmation form, no
    #     patient data leaked) | 'admin_override' (admin proceeded with
    #     a justification).
    #   admin_justification: verbatim text the admin entered; NULL for
    #     every non-override row. Immutable once written (no UPDATE
    #     path in any audit code).
    event_type = db.Column(
        db.String(32), nullable=False, default="read",
        server_default="read", index=True,
    )
    admin_justification = db.Column(db.Text, nullable=True)


class SavedDesign(db.Model):
    """User-private, reusable dashboard design template (#467 / #462 D5).

    A 'design' is a reusable TEMPLATE — a set of diagram definitions
    (each: a parameter concept, an optional mirror parameter on a second
    y-axis, the y-axis mode, and the time-window scaler position) that is
    re-applied to ANY patient. Per the operator's #469 answers (2026-07-13):

      - Q3: reusable template (NOT patient-bound) and PRIVATE to the owner.
        Every query filters on ``owner_user_guid``; there is no admin
        cross-user view — these are personal view configs, not patient
        data, so the #212 admin-override machinery does not apply.

    ``owner_user_guid`` is the SSO ``user_guid`` string (String(128), not
    UUID, to match DashboardAudit — service-key callers carry synthetic
    non-UUID guids, though in practice only real operators save designs).

    ``spec`` is opaque JSON owned by the frontend (the diagram list). The
    backend persists it and only lightly validates (must be a JSON object)
    so the charting shape (D4/#466) can evolve without a migration.
    """
    __tablename__ = "saved_design"
    guid = db.Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    owner_user_guid = db.Column(db.String(128), nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    spec = db.Column(JSONB, nullable=False, default=dict)
    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)
    updated_at = db.Column(
        db.DateTime(timezone=True), default=_now, onupdate=_now, nullable=False,
    )

    def to_dict(self) -> dict:
        return {
            "guid": self.guid,
            "name": self.name,
            "spec": self.spec or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


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
