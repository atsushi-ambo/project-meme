"""健康記録に食事量・便の様子・日時精度を追加。

Revision ID: f4c7a9102b6d
Revises: a6d4e2f81c93
"""
from alembic import op
import sqlalchemy as sa

revision = 'f4c7a9102b6d'
down_revision = 'a6d4e2f81c93'
branch_labels = None
depends_on = None


def upgrade():
    # NULLは移行前の記録、または精度を指定しなかったAPI記録を表す。
    op.add_column('health_entries', sa.Column('food_amount', sa.String(length=120), nullable=True))
    op.add_column('health_entries', sa.Column('stool_condition', sa.String(length=500), nullable=True))
    op.add_column('health_entries', sa.Column('time_is_approximate', sa.Boolean(), nullable=True))
    op.create_check_constraint('food_amount_kind', 'health_entries', "food_amount IS NULL OR event_type='food'")
    op.create_check_constraint('stool_condition_kind', 'health_entries', "stool_condition IS NULL OR event_type='toilet'")


def downgrade():
    has_values = op.get_bind().execute(sa.text(
        'SELECT EXISTS (SELECT 1 FROM health_entries WHERE '
        'food_amount IS NOT NULL OR stool_condition IS NOT NULL OR time_is_approximate IS NOT NULL)'
    )).scalar()
    if has_values:
        raise RuntimeError('新しい健康記録の値があるため、データを失うダウングレードは中止しました。')
    op.drop_constraint('stool_condition_kind', 'health_entries', type_='check')
    op.drop_constraint('food_amount_kind', 'health_entries', type_='check')
    op.drop_column('health_entries', 'time_is_approximate')
    op.drop_column('health_entries', 'stool_condition')
    op.drop_column('health_entries', 'food_amount')
