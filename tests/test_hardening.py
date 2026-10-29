"""最小権限と、他のサイト・他のOSユーザーからの読み取り防止を確認する。"""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.config import settings
from app.db import transaction
from app.media import paths
from app.models import Media
from test_app import client, csrf, observation, png, upload


def test_cross_origin_reads_are_blocked_by_headers(client):
    oid = observation(client, '写真の記録')
    mid, response = upload(client, oid, png())
    assert response.status_code == 200
    for path in ['/', '/voices', f'/media/{mid}', '/api/analysis/voices']:
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers['cross-origin-resource-policy'] == 'same-origin'
        assert response.headers['cross-origin-opener-policy'] == 'same-origin'
        assert 'microphone=()' in response.headers['permissions-policy']
        assert response.headers['x-content-type-options'] == 'nosniff'


def test_saved_media_is_private_to_the_app_user(client):
    oid = observation(client, '写真の記録')
    mid, response = upload(client, oid, png())
    assert response.status_code == 200
    with transaction() as db:
        path, _ = paths(db.get(Media, mid).storage_key)
    assert path.stat().st_mode & 0o077 == 0
    assert (settings.media_root / 'staging').is_dir()


@pytest.mark.parametrize('statement', [
    "UPDATE alembic_version SET version_num = 'tampered'",
    'DELETE FROM alembic_version',
    'CREATE TABLE app_should_not_create (id int)',
    'CREATE TEMP TABLE app_should_not_create_temp (id int)',
])
def test_app_role_cannot_change_schema_or_migration_state(client, statement):
    with transaction() as db:
        if db.scalar(text('SELECT current_user')) != 'meme_app':
            pytest.skip('アプリ用ロールで接続したときだけ確認する')
    with pytest.raises(DBAPIError):
        with transaction() as db:
            db.execute(text(statement))
    with transaction() as db:
        assert db.scalar(text('SELECT count(*) FROM alembic_version')) == 1
        assert db.scalar(text("SELECT has_database_privilege('meme_app', current_database(), 'CONNECT')"))
        assert not db.scalar(text("SELECT has_database_privilege('public', current_database(), 'CONNECT')"))
