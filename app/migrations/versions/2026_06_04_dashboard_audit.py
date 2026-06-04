"""dashboard_audit table — PDL Ch 4 §3 kontroller log (ticket #211)

Revision ID: a1d17211aa01
Revises: c0h0r70428aa
Create Date: 2026-06-04 13:00:00.000000

ACCESS-LOG FOUNDATION. Every patient-touching read through the
dashboard writes exactly one row here via the ``@audit_read``
decorator. Blocks Dashboard PDL #2 / #3 / #4 / #5 (#212-#215), which
all build on this table.

The session_id column is nullable until SSO Phase 3 (#191) emits the
session id in the access blob.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = 'a1d17211aa01'
down_revision = 'c0h0r70428aa'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'dashboard_audit',
        sa.Column('guid', sa.UUID(as_uuid=False), nullable=False),
        sa.Column(
            'timestamp',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('NOW()'),
        ),
        sa.Column('user_guid', sa.String(length=128), nullable=True),
        sa.Column(
            'user_org_guids',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column('route', sa.String(length=256), nullable=False),
        sa.Column('patient_guid', sa.UUID(as_uuid=False), nullable=True),
        sa.Column('n_rows_returned', sa.Integer(), nullable=True),
        sa.Column('response_status', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint('guid'),
    )
    op.create_index('ix_dashboard_audit_timestamp', 'dashboard_audit', ['timestamp'])
    op.create_index('ix_dashboard_audit_user_guid', 'dashboard_audit', ['user_guid'])
    op.create_index('ix_dashboard_audit_route', 'dashboard_audit', ['route'])
    op.create_index('ix_dashboard_audit_patient_guid', 'dashboard_audit', ['patient_guid'])
    op.create_index('ix_dashboard_audit_session_id', 'dashboard_audit', ['session_id'])


def downgrade():
    op.drop_index('ix_dashboard_audit_session_id', table_name='dashboard_audit')
    op.drop_index('ix_dashboard_audit_patient_guid', table_name='dashboard_audit')
    op.drop_index('ix_dashboard_audit_route', table_name='dashboard_audit')
    op.drop_index('ix_dashboard_audit_user_guid', table_name='dashboard_audit')
    op.drop_index('ix_dashboard_audit_timestamp', table_name='dashboard_audit')
    op.drop_table('dashboard_audit')
