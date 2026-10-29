"""声の分類の更新を、編集前の版番号で保護する。"""
from alembic import op
import sqlalchemy as sa

revision = 'b7e2d9a4106c'
down_revision = 'a6d4e2f81c93'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('sound_analyses', sa.Column('revision', sa.BigInteger(), nullable=False, server_default='1'))


def downgrade():
    # 分類やメモは残し、派生する版番号だけを除去する。
    op.drop_column('sound_analyses', 'revision')
