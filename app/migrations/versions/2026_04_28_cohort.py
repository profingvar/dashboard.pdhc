"""cohort table — Phase-4.5 cross-worker persistence

Revision ID: c0h0r70428aa
Revises: 146cc611c12e
Create Date: 2026-04-28 18:00:00.000000

Replaces the per-gunicorn-worker ``_COHORTS`` dict in
routes/researcher.py with a Postgres-backed table so cohorts survive
restarts and are visible across workers.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = 'c0h0r70428aa'
down_revision = '146cc611c12e'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'cohort',
        sa.Column('guid', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('filter', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('members', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('n', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('owner_label', sa.String(length=256), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('NOW()'),
        ),
        sa.PrimaryKeyConstraint('guid'),
    )
    op.create_index('ix_cohort_created_at', 'cohort', ['created_at'])


def downgrade():
    op.drop_index('ix_cohort_created_at', table_name='cohort')
    op.drop_table('cohort')
