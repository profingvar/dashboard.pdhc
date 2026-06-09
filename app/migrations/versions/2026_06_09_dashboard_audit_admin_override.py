"""dashboard_audit.event_type + admin_justification (ticket #212)

Revision ID: a8bc21200001
Revises: e21404aa01
Create Date: 2026-06-09 08:30:00.000000

#212 makes SU-admin off-org reads an explicit, audited lift instead of
a silent bypass. Two columns:

  - ``event_type``: 'read' (default) | 'admin_override' |
    'admin_override_required'. Indexed so the future /admin/audit view
    (#215) can filter for override events cheaply.
  - ``admin_justification``: free-text reason the admin provided.
    Immutable once written, queried back on /admin/audit.
"""
from alembic import op
import sqlalchemy as sa


revision = 'a8bc21200001'
down_revision = 'e21404aa01'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'dashboard_audit',
        sa.Column(
            'event_type', sa.String(32),
            nullable=False, server_default='read',
        ),
    )
    op.create_index(
        'ix_dashboard_audit_event_type',
        'dashboard_audit', ['event_type'],
    )
    op.add_column(
        'dashboard_audit',
        sa.Column('admin_justification', sa.Text(), nullable=True),
    )


def downgrade():
    op.drop_index('ix_dashboard_audit_event_type', 'dashboard_audit')
    op.drop_column('dashboard_audit', 'admin_justification')
    op.drop_column('dashboard_audit', 'event_type')
