"""Keep currently available videos until the user decides to remove them."""
from alembic import op

revision = 'e7802a46bd31'
down_revision = 'd61e38b8a204'
branch_labels = None
depends_on = None


def upgrade():
    # Do not resurrect expired or deletion-pending records.
    op.execute("""UPDATE media SET keep = true
        WHERE status = 'ready' AND mime_type LIKE 'video/%'
          AND (keep = true OR expires_at > CURRENT_TIMESTAMP)""")


def downgrade():
    # Reverting the schema must not turn retention back on for saved videos.
    pass
