"""Queue for host-side AI drafts of video analyses."""
from alembic import op
import sqlalchemy as sa

revision = 'c5d1e9a7f203'
down_revision = '9e10c6ab5341'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('ai_jobs',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('media_id', sa.BigInteger(), sa.ForeignKey('media.id', ondelete='CASCADE'), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('trigger', sa.String(20), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('lease', sa.String(64)),
        sa.Column('claimed_at', sa.DateTime(timezone=True)),
        sa.Column('tool', sa.String(40)),
        sa.Column('images_sent', sa.Integer()),
        sa.Column('result', sa.Text()),
        sa.Column('error', sa.String(500)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('queued','running','done','failed')", name='ai_job_status'),
        sa.CheckConstraint("trigger IN ('manual','auto')", name='ai_job_trigger'),
        sa.CheckConstraint('attempts BETWEEN 0 AND 10', name='ai_job_attempts'),
        sa.CheckConstraint('result IS NULL OR length(result) <= 20000', name='ai_job_result_size'),
        sa.CheckConstraint("status <> 'done' OR (result IS NOT NULL AND tool IS NOT NULL)", name='ai_job_done'))
    op.create_index('ix_ai_jobs_media_id', 'ai_jobs', ['media_id'])
    op.create_index('ux_ai_jobs_active', 'ai_jobs', ['media_id'], unique=True,
                    postgresql_where=sa.text("status IN ('queued','running')"))


def downgrade():
    op.drop_table('ai_jobs')
