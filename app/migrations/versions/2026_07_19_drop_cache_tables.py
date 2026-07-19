"""drop observation_cache + refresh_log (retired cache surface, #471)

Revision ID: drop0719cache01
Revises: sd071322aa01
Create Date: 2026-07-19 19:30:00.000000

#471 item 1 retired the ObservationCache clinical surface (operator #469 Q6 =
live CDR1 reads only). This drops the two now-orphaned tables. The prod data
(7019 observation_cache + 185 refresh_log rows) was pg_dump'd to
~/backups/predeploy/dashboard/ before the drop — restorable from there.
``downgrade`` recreates the empty table schemas (data would be reloaded from
that dump). Revision id <=32 chars (alembic_version is varchar(32)).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = 'drop0719cache01'
down_revision = 'sd071322aa01'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table('refresh_log')          # drop the FK-bearing table first
    with op.batch_alter_table('observation_cache', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_observation_cache_patient_guid'))
        batch_op.drop_index(batch_op.f('ix_observation_cache_org_guid'))
        batch_op.drop_index(batch_op.f('ix_observation_cache_concept_guid'))
    op.drop_table('observation_cache')


def downgrade():
    op.create_table(
        'observation_cache',
        sa.Column('guid', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('source_obs_guid', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('patient_guid', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('org_guid', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('concept_guid', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('concept_name', sa.String(length=256), nullable=False),
        sa.Column('value', sa.Float(), nullable=True),
        sa.Column('unit', sa.String(length=64), nullable=True),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('raw', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('guid'),
        sa.UniqueConstraint('source_obs_guid'),
    )
    with op.batch_alter_table('observation_cache', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_observation_cache_concept_guid'), ['concept_guid'], unique=False)
        batch_op.create_index(batch_op.f('ix_observation_cache_org_guid'), ['org_guid'], unique=False)
        batch_op.create_index(batch_op.f('ix_observation_cache_patient_guid'), ['patient_guid'], unique=False)
    op.create_table(
        'refresh_log',
        sa.Column('guid', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('user_guid', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('org_guid', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('rows_fetched', sa.Integer(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['user_guid'], ['users.guid'], ),
        sa.PrimaryKeyConstraint('guid'),
    )
