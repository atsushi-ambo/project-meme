"""Least privilege: no PUBLIC access to the database, read-only migration state for the app role."""
from alembic import op

revision = 'a6d4e2f81c93'
down_revision = 'f3a9c1d27b40'
branch_labels = None
depends_on = None

# 初期化スクリプトは新規DBにしか効かないため、既存の環境へもここで適用する。
# PUBLICの既定（DBへの接続・一時テーブル作成）を外し、アプリ用ロールだけへ接続を許す。
# アプリ用ロールはマイグレーションの版番号を読めるが、書き換えられない。
UPGRADE = """
DO $$
BEGIN
  EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database());
  REVOKE CREATE ON SCHEMA public FROM PUBLIC;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'meme_app') THEN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO meme_app', current_database());
    REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON alembic_version FROM meme_app;
    GRANT SELECT ON alembic_version TO meme_app;
  END IF;
END $$;
"""
DOWNGRADE = """
DO $$
BEGIN
  EXECUTE format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO PUBLIC', current_database());
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'meme_app') THEN
    GRANT INSERT, UPDATE, DELETE ON alembic_version TO meme_app;
  END IF;
END $$;
"""


def upgrade():
    op.execute(UPGRADE)


def downgrade():
    op.execute(DOWNGRADE)
