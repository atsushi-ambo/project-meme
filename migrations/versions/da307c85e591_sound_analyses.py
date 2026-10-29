"""Store user-selected sound measurements and their context."""
from alembic import op
import sqlalchemy as sa

revision = 'da307c85e591'
down_revision = 'e7802a46bd31'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('sound_analyses',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('media_id', sa.BigInteger(), sa.ForeignKey('media.id', ondelete='CASCADE'), nullable=False),
        sa.Column('start_ms', sa.Integer(), nullable=False),
        sa.Column('end_ms', sa.Integer(), nullable=False),
        sa.Column('rms', sa.Numeric(8, 6), nullable=False),
        sa.Column('peak', sa.Numeric(8, 6), nullable=False),
        sa.Column('observation', sa.Text(), nullable=False),
        sa.Column('hypothesis', sa.Text(), nullable=False),
        sa.Column('unknown', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('start_ms >= 0 AND end_ms > start_ms AND end_ms - start_ms <= 10000', name='sound_analysis_interval'),
        sa.CheckConstraint('rms >= 0 AND rms <= 1 AND peak >= 0 AND peak <= 1', name='sound_analysis_levels'),
        sa.UniqueConstraint('media_id', 'start_ms', 'end_ms', name='sound_analysis_unique_interval'))
    op.create_index('ix_sound_analyses_media_id', 'sound_analyses', ['media_id'])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT EXISTS (SELECT 1 FROM sound_analyses)')).scalar():
        raise RuntimeError('音声分析の記録を自動削除できません。バックアップを確認してください。')
    op.drop_table('sound_analyses')
