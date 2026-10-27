"""検索用 pg_trgm 拡張と GIN インデックス。

本文検索は部分一致(ILIKE)のままだが、pg_trgm の GIN インデックスで
件数が増えても逐次スキャンにならないようにする。日本語の形態素解析は
行わないため、全文検索ではなくトライグラムによる高速化に留める。

Revision ID: e8b7c2d4a501
Revises: d61e38b8a204
"""
from alembic import op

revision = 'e8b7c2d4a501'
down_revision = 'd61e38b8a204'
branch_labels = None
depends_on = None

def upgrade():
    op.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')
    op.create_index('ix_observations_description_trgm', 'observations',
                    ['description'], postgresql_using='gin',
                    postgresql_ops={'description': 'gin_trgm_ops'})
    op.create_index('ix_comments_body_trgm', 'observation_comments',
                    ['body'], postgresql_using='gin',
                    postgresql_ops={'body': 'gin_trgm_ops'})
    op.create_index('ix_health_details_trgm', 'health_entries',
                    ['details'], postgresql_using='gin',
                    postgresql_ops={'details': 'gin_trgm_ops'})

def downgrade():
    op.drop_index('ix_health_details_trgm', table_name='health_entries')
    op.drop_index('ix_comments_body_trgm', table_name='observation_comments')
    op.drop_index('ix_observations_description_trgm', table_name='observations')
    op.execute('DROP EXTENSION IF EXISTS pg_trgm')
