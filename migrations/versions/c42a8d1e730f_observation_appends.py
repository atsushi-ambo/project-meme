"""Guard observation edits and identify append retries without duplicating notes."""
from alembic import op
import sqlalchemy as sa

revision = 'c42a8d1e730f'
down_revision = '9be62a103d75'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('observations', sa.Column('revision', sa.BigInteger(), nullable=False, server_default='1'))
    op.create_check_constraint('observation_revision', 'observations', 'revision >= 1')
    op.create_table('observation_appends',
        sa.Column('operation_key', sa.String(36), primary_key=True),
        sa.Column('observation_id', sa.BigInteger(), sa.ForeignKey('observations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_observation_appends_observation_id', 'observation_appends', ['observation_id'])


def downgrade():
    # 追記済みの本文は保持し、再送用の管理情報だけを除去する。
    op.drop_table('observation_appends')
    op.drop_constraint('observation_revision', 'observations', type_='check')
    op.drop_column('observations', 'revision')
