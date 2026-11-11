"""観察記録に、こちらの対応とその後の様子を追加する。"""
from alembic import op
import sqlalchemy as sa

revision = '9e10c6ab5341'
down_revision = 'b7e2d9a4106c'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('observations', sa.Column('owner_action', sa.String(500), nullable=True))
    op.add_column('observations', sa.Column('observed_after', sa.String(500), nullable=True))


def downgrade():
    has_notes = op.get_bind().execute(sa.text(
        'SELECT EXISTS (SELECT 1 FROM observations '
        'WHERE owner_action IS NOT NULL OR observed_after IS NOT NULL)'
    )).scalar()
    if has_notes:
        raise RuntimeError('やり取りの記録を失うダウングレードは中止しました。')
    op.drop_column('observations', 'observed_after')
    op.drop_column('observations', 'owner_action')
