"""Mark sample records explicitly so reviews use actual observations only."""
from alembic import op
import sqlalchemy as sa

revision = '9be62a103d75'
down_revision = '4d33b438142b'
branch_labels = None
depends_on = None


def upgrade():
    for table, field in [('observations', 'description'), ('health_entries', 'details')]:
        op.add_column(table, sa.Column('is_sample', sa.Boolean(), nullable=False, server_default=sa.false()))
        # 旧demoの先頭マーカーだけを移行。本文中の言及は実記録のまま。
        op.execute(sa.text(f"UPDATE {table} SET is_sample = true WHERE {field} LIKE :marker").bindparams(marker='【サンプル】%'))


def downgrade():
    for table in ['health_entries', 'observations']:
        op.drop_column(table, 'is_sample')
