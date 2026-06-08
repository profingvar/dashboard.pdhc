"""dashboard_audit.payload_snapshot column (ticket #214)

Revision ID: e21404aa01
Revises: a1d17211aa01
Create Date: 2026-06-08 09:55:00.000000

#214 migrates the researcher CSV export audit from
``results/export_audit.log`` to the dashboard_audit table. The new
``payload_snapshot`` JSONB column gives that event a place to carry
its export-specific metadata (export_id, cohort_id, variables) without
inventing a peer table. Stays NULL for every other row written by the
generic ``@audit_read`` decorator.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = 'e21404aa01'
down_revision = 'a1d17211aa01'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'dashboard_audit',
        sa.Column(
            'payload_snapshot',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade():
    op.drop_column('dashboard_audit', 'payload_snapshot')
