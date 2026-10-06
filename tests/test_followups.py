"""一つのメモへの追記・競合防止と、明示した添付の保管指定。"""
import uuid
from concurrent.futures import ThreadPoolExecutor
import os
import subprocess
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
import pytest
from fastapi import HTTPException
from app.db import transaction
from app.models import Observation, ObservationAppend, Media, now
from app.schemas import AppendInput
from app.services import append_observation
from test_app import client, csrf, observation, post, upload, png


def headers(client):
    return {'Origin': 'http://localhost:8000', 'X-CSRF-Token': csrf(client)}


def draft(revision=1, text='その後、立ち上がると廊下へ走った。'):
    return {'operation_key': str(uuid.uuid4()), 'expected_revision': revision, 'text': text}


def edit(client, oid, revision, text):
    return post(client, f'/entries/observation/{oid}/save', {
        'occurred_at': '2026-10-06T12:00', 'description': text, 'location': '',
        'submission_key': str(uuid.uuid4()), 'expected_revision': revision})


def test_snapshot_and_append_replay_keep_a_single_memo(client):
    key = str(uuid.uuid4())
    oid = observation(client, '鳴いていた。', key)
    mid, uploaded = upload(client, oid, png())
    assert uploaded.status_code == 200
    before = client.get(f'/api/observations/{oid}').json()
    assert before['description'] == '鳴いていた。' and before['revision'] == 1
    assert before['media'][0]['id'] == mid and before['media'][0]['keep'] is False
    assert not {'submission_key', 'submission_hash', 'storage_key', 'operation_key'} & set(before)
    assert not {'storage_key', 'upload_key', 'path'} & set(before['media'][0])
    data = draft(text='  <script>後の様子</script>  ')
    url = f'/api/observations/{oid}/append'
    first = client.post(url, json=data, headers=headers(client))
    assert first.status_code == 200 and first.json()['revision'] == 2
    assert first.json()['replayed'] is False
    replay = client.post(url, json=data, headers=headers(client))
    assert replay.status_code == 200 and replay.json()['replayed'] is True
    assert client.get(f'/api/observations/{oid}').json()['description'] == '鳴いていた。\n\n<script>後の様子</script>'
    assert '&lt;script&gt;後の様子&lt;/script&gt;' in client.get(first.json()['url']).text
    # 元の新規投稿を再送しても、追記済み本文を巻き戻さない。
    assert observation(client, '鳴いていた。', key) == oid
    with transaction() as db:
        assert db.scalar(select(func.count()).select_from(ObservationAppend)) == 1


def test_append_key_cannot_change_text_revision_or_parent(client):
    oid = observation(client)
    other = observation(client, '別の記録。')
    data = draft()
    url = f'/api/observations/{oid}/append'
    assert client.post(url, json=data, headers=headers(client)).status_code == 200
    for changed in [{**data, 'text': '違う追記。'}, {**data, 'expected_revision': 2}]:
        assert client.post(url, json=changed, headers=headers(client)).status_code == 409
    assert client.post(f'/api/observations/{other}/append', json=data, headers=headers(client)).status_code == 409
    assert client.get(f'/api/observations/{other}').json()['revision'] == 1


def test_browser_conflict_preserves_input_and_does_not_overwrite_append(client):
    oid = observation(client)
    initial = client.get(f'/entries/observation/{oid}/edit').text
    assert 'name="expected_revision" value="1"' in initial
    assert client.post(f'/api/observations/{oid}/append', json=draft(), headers=headers(client)).status_code == 200
    conflict = edit(client, oid, 1, '書きかけのメモ。')
    assert conflict.status_code == 409
    assert '書きかけのメモ。</textarea>' in conflict.text
    assert 'name="expected_revision" value="1"' in conflict.text
    assert '保存済みの記録を確認' in conflict.text
    assert '廊下へ走った。' in client.get(f'/api/observations/{oid}').json()['description']
    invalid = edit(client, oid, 1, '')
    assert invalid.status_code == 422 and 'name="expected_revision" value="1"' in invalid.text
    assert edit(client, oid, 2, '内容を合わせたメモ。').status_code == 303
    assert client.get(f'/api/observations/{oid}').json()['revision'] == 3


def test_browser_requires_revision_and_append_checks_browser_changes(client):
    oid = observation(client)
    data = draft()
    missing = post(client, f'/entries/observation/{oid}/save', {
        'occurred_at': '2026-10-06T12:00', 'description': '変更', 'submission_key': str(uuid.uuid4())})
    assert missing.status_code == 422
    assert edit(client, oid, 1, '先にブラウザから編集。').status_code == 303
    assert client.post(f'/api/observations/{oid}/append', json=data, headers=headers(client)).status_code == 409
    with transaction() as db:
        assert db.scalar(select(func.count()).select_from(ObservationAppend)) == 0
    data['expected_revision'] = 2
    assert client.post(f'/api/observations/{oid}/append', json=data, headers=headers(client)).status_code == 200
    assert edit(client, oid, 3, '追記した後で編集した本文。').status_code == 303
    replay = client.post(f'/api/observations/{oid}/append', json=data, headers=headers(client))
    assert replay.status_code == 200 and replay.json()['replayed'] is True
    current = client.get(f'/api/observations/{oid}').json()
    assert current['description'] == '追記した後で編集した本文。' and current['revision'] == 4


def test_append_length_boundary_rolls_back_everything(client):
    oid = observation(client, 'あ' * 9997)
    assert client.post(f'/api/observations/{oid}/append', json=draft(text='い'), headers=headers(client)).status_code == 200
    response = client.post(f'/api/observations/{oid}/append', json=draft(2, 'う'), headers=headers(client))
    assert response.status_code == 422
    current = client.get(f'/api/observations/{oid}').json()
    assert len(current['description']) == 10000 and current['revision'] == 2
    with transaction() as db:
        assert db.scalar(select(func.count()).select_from(ObservationAppend)) == 1


@pytest.mark.parametrize('value', [True, False, '1', 1.5, 0, 9223372036854775808, None])
def test_append_revision_is_a_strict_positive_bigint(client, value):
    oid = observation(client)
    assert client.post(f'/api/observations/{oid}/append', json=draft(value), headers=headers(client)).status_code == 422
    assert client.get(f'/api/observations/{oid}').json()['revision'] == 1


@pytest.mark.parametrize('changed', [{'text': ''}, {'text': '  '}, {'text': 'あ' * 10001}, {'extra': '拒否'}, {'operation_key': 'bad'}])
def test_append_validation_rejects_invalid_payload(client, changed):
    oid = observation(client)
    assert client.post(f'/api/observations/{oid}/append', json={**draft(), **changed}, headers=headers(client)).status_code == 422


def test_append_missing_deleting_and_deleted_parent_and_ledger_cascade(client):
    oid = observation(client)
    data = draft()
    url = f'/api/observations/{oid}/append'
    assert client.post(url, json=data, headers=headers(client)).status_code == 200
    with transaction() as db:
        db.get(Observation, oid).deletion_requested_at = now()
    assert client.get(f'/api/observations/{oid}').status_code == 404
    assert client.post(url, json=data, headers=headers(client)).status_code == 404
    with transaction() as db:
        db.delete(db.get(Observation, oid))
    assert client.post(url, json=data, headers=headers(client)).status_code == 404
    with transaction() as db:
        assert db.scalar(select(func.count()).select_from(ObservationAppend)) == 0


def test_concurrent_appends_cannot_lose_or_duplicate_text(client):
    oid = observation(client)
    inputs = [AppendInput.model_validate(draft(text='追記A。')), AppendInput.model_validate(draft(text='追記B。'))]
    def save(data):
        try:
            with transaction() as db:
                append_observation(db, oid, data)
            return 200
        except HTTPException as exc:
            return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, inputs)) == [200, 409]
    current = client.get(f'/api/observations/{oid}').json()
    assert current['revision'] == 2
    assert ('追記A。' in current['description']) != ('追記B。' in current['description'])
    with transaction() as db:
        assert db.scalar(select(func.count()).select_from(ObservationAppend)) == 1
        with pytest.raises(IntegrityError):
            with db.begin_nested():
                db.get(Observation, oid).revision = 0
                db.flush()


def test_explicit_keep_is_idempotent_and_browser_toggle_still_works(client):
    oid = observation(client)
    mid, uploaded = upload(client, oid, png())
    assert uploaded.status_code == 200
    url = f'/api/media/{mid}/keep'
    for value in [True, True, False, False]:
        response = client.post(url, json={'keep': value}, headers=headers(client))
        assert response.status_code == 200 and response.json() == {'id': mid, 'keep': value}
    assert post(client, f'/media/{mid}/keep', {}).status_code == 303
    assert client.get(f'/api/observations/{oid}').json()['media'][0]['keep'] is True
    with transaction() as db:
        assert db.get(Observation, oid).revision == 1
        db.get(Media, mid).expires_at = now()
    assert client.post(url, json={'keep': True}, headers=headers(client)).status_code == 200
    assert client.get(f'/media/{mid}').status_code == 200
    assert client.post(url, json={'keep': False}, headers=headers(client)).status_code == 200
    assert client.post(url, json={'keep': False}, headers=headers(client)).status_code == 200
    assert client.post(url, json={'keep': True}, headers=headers(client)).status_code == 409
    assert client.get(f'/media/{mid}').status_code == 404


@pytest.mark.parametrize('value', ['true', 'false', 0, 1, None, [], {}])
def test_keep_rejects_non_boolean_values(client, value):
    mid, uploaded = upload(client, observation(client), png())
    assert uploaded.status_code == 200
    assert client.post(f'/api/media/{mid}/keep', json={'keep': value}, headers=headers(client)).status_code == 422
    with transaction() as db:
        assert db.get(Media, mid).keep is False


@pytest.mark.parametrize('status', ['pending', 'failed', 'delete_pending', 'deleted'])
def test_keep_cannot_revive_not_ready_media(client, status):
    mid, uploaded = upload(client, observation(client), png())
    assert uploaded.status_code == 200
    with transaction() as db:
        db.get(Media, mid).status = status
    assert client.post(f'/api/media/{mid}/keep', json={'keep': True}, headers=headers(client)).status_code == 409
    with transaction() as db:
        assert db.get(Media, mid).keep is False


def test_keep_rejects_missing_or_deleting_parent(client):
    oid = observation(client)
    mid, uploaded = upload(client, oid, png())
    assert uploaded.status_code == 200
    with transaction() as db:
        db.get(Observation, oid).deletion_requested_at = now()
    assert client.post(f'/api/media/{mid}/keep', json={'keep': True}, headers=headers(client)).status_code == 404
    assert client.post('/api/media/999999/keep', json={'keep': True}, headers=headers(client)).status_code == 409


@pytest.mark.parametrize('id', [0, -1, 9223372036854775808])
def test_new_api_ids_have_bigint_bounds(client, id):
    h = headers(client)
    assert client.get(f'/api/observations/{id}').status_code == 422
    assert client.post(f'/api/observations/{id}/append', json=draft(), headers=h).status_code == 422
    assert client.post(f'/api/media/{id}/keep', json={'keep': True}, headers=h).status_code == 422


def test_new_mutation_apis_require_csrf_and_origin(client):
    oid = observation(client)
    mid, uploaded = upload(client, oid, png())
    assert uploaded.status_code == 200
    token = csrf(client)
    for url, data in [(f'/api/observations/{oid}/append', draft()), (f'/api/media/{mid}/keep', {'keep': True})]:
        assert client.post(url, json=data, headers={'Origin': 'http://localhost:8000'}).status_code == 403
        assert client.post(url, json=data, headers={'Origin': 'https://evil.example', 'X-CSRF-Token': token}).status_code == 403
        assert client.post(url, content=b'{', headers={'Origin': 'http://localhost:8000', 'X-CSRF-Token': token}).status_code == 422
    from app.config import settings
    (settings.media_root / 'deleting-all').write_text('pending')
    for url, data in [(f'/api/observations/{oid}/append', draft()), (f'/api/media/{mid}/keep', {'keep': True})]:
        assert client.post(url, json=data, headers=headers(client)).status_code == 409


def test_append_migration_preserves_existing_and_appended_text(client):
    oid = observation(client, '移行前のメモ。')
    assert client.post(f'/api/observations/{oid}/append', json=draft(text='追記した様子。'), headers=headers(client)).status_code == 200
    env = {**os.environ, 'DATABASE_URL': 'postgresql+psycopg://meme_owner:test-owner-only@db:5432/meme'}
    subprocess.run(['alembic', 'downgrade', '9be62a103d75'], env=env, check=True, capture_output=True)
    try:
        subprocess.run(['alembic', 'upgrade', 'head'], env=env, check=True, capture_output=True)
        current = client.get(f'/api/observations/{oid}').json()
        assert current['description'] == '移行前のメモ。\n\n追記した様子。'
        assert current['revision'] == 1
    finally:
        subprocess.run(['alembic', 'upgrade', 'head'], env=env, check=True, capture_output=True)


def test_browser_can_save_full_unicode_memo_but_rejects_extra_characters(client):
    oid = observation(client, '🐱' * 10000)
    assert client.get(f'/api/observations/{oid}').json()['description'] == '🐱' * 10000
    assert edit(client, oid, 1, '🐱' * 10001).status_code == 422
    assert client.get(f'/api/observations/{oid}').json()['revision'] == 1
