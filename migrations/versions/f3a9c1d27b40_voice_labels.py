"""Owner-named call types, behaviors, outcomes and acoustic features for sound segments."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'f3a9c1d27b40'
down_revision = 'da307c85e591'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('call_types',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('name', sa.String(40), nullable=False, unique=True),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.add_column('sound_analyses', sa.Column('call_type_id', sa.BigInteger(),
        sa.ForeignKey('call_types.id', ondelete='SET NULL')))
    op.add_column('sound_analyses', sa.Column('call_count', sa.Integer()))
    op.add_column('sound_analyses', sa.Column('owner_response', sa.Text(), nullable=False, server_default=''))
    op.add_column('sound_analyses', sa.Column('outcome', sa.String(20)))
    op.add_column('sound_analyses', sa.Column('dictionary_item_id', sa.BigInteger(),
        sa.ForeignKey('dictionary_items.id', ondelete='SET NULL')))
    op.add_column('sound_analyses', sa.Column('assessment', sa.String(20), nullable=False, server_default='unknown'))
    op.add_column('sound_analyses', sa.Column('features', postgresql.JSONB()))
    op.add_column('sound_analyses', sa.Column('updated_at', sa.DateTime(timezone=True)))
    op.create_index('ix_sound_analyses_call_type_id', 'sound_analyses', ['call_type_id'])
    op.create_index('ix_sound_analyses_dictionary_item_id', 'sound_analyses', ['dictionary_item_id'])
    op.create_check_constraint('sound_analysis_call_count', 'sound_analyses',
        'call_count IS NULL OR call_count BETWEEN 1 AND 100')
    op.create_check_constraint('sound_analysis_outcome', 'sound_analyses',
        "outcome IS NULL OR outcome IN ('settled','repeated','stronger','left','unknown')")
    op.create_check_constraint('sound_analysis_assessment', 'sound_analyses',
        "assessment IN ('supports','contradicts','unknown')")
    op.create_check_constraint('sound_analysis_features_object', 'sound_analyses',
        "features IS NULL OR jsonb_typeof(features) = 'object'")
    op.create_table('sound_behaviors',
        sa.Column('sound_analysis_id', sa.BigInteger(),
                  sa.ForeignKey('sound_analyses.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('behavior', sa.String(20), primary_key=True))


def downgrade():
    labelled = op.get_bind().execute(sa.text(
        'SELECT EXISTS (SELECT 1 FROM sound_analyses WHERE call_type_id IS NOT NULL OR dictionary_item_id IS NOT NULL '
        "OR outcome IS NOT NULL OR owner_response <> '' OR call_count IS NOT NULL) "
        'OR EXISTS (SELECT 1 FROM sound_behaviors)')).scalar()
    if labelled:
        raise RuntimeError('声の分類・行動・その後の記録を自動削除できません。バックアップを確認してください。')
    op.drop_table('sound_behaviors')
    for name in ['sound_analysis_features_object', 'sound_analysis_assessment',
                 'sound_analysis_outcome', 'sound_analysis_call_count']:
        op.drop_constraint(name, 'sound_analyses', type_='check')
    op.drop_index('ix_sound_analyses_dictionary_item_id', 'sound_analyses')
    op.drop_index('ix_sound_analyses_call_type_id', 'sound_analyses')
    for column in ['updated_at', 'features', 'assessment', 'dictionary_item_id', 'outcome',
                   'owner_response', 'call_count', 'call_type_id']:
        op.drop_column('sound_analyses', column)
    op.drop_table('call_types')
