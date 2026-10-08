"""Store bounded, selected technical metadata without inspecting originals on GET."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'd61e38b8a204'
down_revision = 'c42a8d1e730f'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('media', sa.Column('technical_info', postgresql.JSONB(), nullable=True))
    op.create_check_constraint('media_technical_object', 'media',
        "technical_info IS NULL OR jsonb_typeof(technical_info) = 'object'")


def downgrade():
    op.drop_constraint('media_technical_object', 'media', type_='check')
    op.drop_column('media', 'technical_info')
