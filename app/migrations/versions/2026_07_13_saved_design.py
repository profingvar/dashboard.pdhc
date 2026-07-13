"""saved_design table — user-private reusable dashboard templates (#467 / #462 D5)

Revision ID: sd071322aa01
Revises: a8bc21200001
Create Date: 2026-07-13 19:20:00.000000

#462 redesign D5 (#467). A 'design' is a reusable template — a set of
diagram definitions re-applied to any patient — that is PRIVATE to its
owner (operator #469 Q3). owner_user_guid is the SSO user_guid string;
spec is opaque frontend JSON. Revision id kept <=32 chars (alembic
alembic_version is varchar(32)).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = 'sd071322aa01'
down_revision = 'a8bc21200001'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'saved_design',
        sa.Column('guid', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('owner_user_guid', sa.String(length=128), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('spec', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text('NOW()'),
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text('NOW()'),
        ),
        sa.PrimaryKeyConstraint('guid'),
    )
    op.create_index(
        'ix_saved_design_owner_user_guid', 'saved_design', ['owner_user_guid'],
    )


def downgrade():
    op.drop_index('ix_saved_design_owner_user_guid', table_name='saved_design')
    op.drop_table('saved_design')
