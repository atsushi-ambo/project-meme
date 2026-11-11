"""同じ観察に対応と反応を残し、添付なしでも読み返せることを検証する。"""
import hashlib
import os
import subprocess
from uuid import uuid4

from sqlalchemy import select

from app.db import transaction
from app.models import Observation, ObservationTag
from app.schemas import ObservationInput
from test_app import client, csrf, observation, post


def test_episode_notes_save_search_compare_and_edit(client):
    old = observation(client, '名前を呼んだ場面のメモ。')
    form = {
        'occurred_at': '2026-10-06T12:00', 'description': '猫じゃらしを持ってきた。',
        'submission_key': str(uuid4()), 'owner_action': ' 名前を呼んだ。\r\n見守った。 ',
        'observed_after': '近づいた。<b>追記</b>',
    }
    created = post(client, '/entries/observation/save', form)
    assert created.status_code == 303
    url = created.headers['location']
    record = int(url.split('/')[-1])
    api_url = f'/api/observations/{record}'
    assert post(client, '/entries/observation/save', form).headers['location'] == url
    assert post(client, '/entries/observation/save', {**form, 'observed_after': '鳴いた'}).status_code == 409
    assert '近づいた。&lt;b&gt;追記&lt;/b&gt;' in client.get(url).text
    edit_page = client.get(url + '/edit').text
    assert '名前を呼んだ。\n見守った。</textarea>' in edit_page
    assert '近づいた。&lt;b&gt;追記&lt;/b&gt;</textarea>' in edit_page
    assert '近づいた。&lt;b&gt;追記&lt;/b&gt;' in client.get('/observations', params={'q': ' 近づいた '}).text

    for scope, expected in [('all', [old, record]), ('memo', [old]), ('action', [record]), ('after', [])]:
        response = client.get('/compare', params={'q': ' 名前を呼んだ ', 'scope': scope})
        assert response.status_code == 200
        for candidate in (old, record):
            assert (f'name="selected" value="{candidate}"' in response.text) == (candidate in expected)
    for query in ('%', '_'):
        assert 'name="selected"' not in client.get('/compare', params={'q': query, 'scope': 'action'}).text
    assert client.get('/compare?scope=unsupported').status_code == 422

    comparison = client.get(f'/compare?ids={record},{old}')
    assert '近づいた。&lt;b&gt;追記&lt;/b&gt;' in comparison.text
    assert '欄は未入力' in comparison.text
    rows = client.get(f'/api/analysis/observations?ids={record},{old}').json()['observations']
    assert rows[0]['owner_action'] == '名前を呼んだ。\n見守った。'
    assert rows[0]['media'] == [] and rows[0]['revision'] == 1
    assert rows[1]['owner_action'] is None and rows[1]['observed_after'] is None

    edit = {name: form[name] for name in ('occurred_at', 'description', 'submission_key')}
    assert post(client, url + '/save', {**edit, 'expected_revision': 1}).status_code == 303
    assert client.get(api_url).json()['owner_action'] == rows[0]['owner_action']
    stale = post(client, url + '/save', {**edit, 'expected_revision': 1, 'owner_action': '立ち上がった。'})
    assert stale.status_code == 409 and '立ち上がった。' in stale.text
    assert client.get(api_url).json()['revision'] == 2
    cleared = post(client, url + '/save', {**edit, 'expected_revision': 2, 'owner_action': ''})
    assert cleared.status_code == 303
    saved = client.get(api_url).json()
    assert saved['owner_action'] is None and saved['observed_after'] == '近づいた。<b>追記</b>'


def test_legacy_submission_hash_and_json_validation(client):
    entry = {'occurred_at': '2026-10-06T12:00', 'description': '廊下で鳴いた。',
             'submission_key': str(uuid4())}
    record = observation(client, entry['description'], entry['submission_key'])
    with transaction() as db:
        row = db.get(Observation, record)
        old_payload = ObservationInput(
            **{**entry, 'occurred_at': row.occurred_at}, location=row.location,
        )
        old_payload.tags = list(db.scalars(select(ObservationTag.tag_id).where(ObservationTag.observation_id == record)))
        old_json = old_payload.model_dump_json(exclude={'owner_action', 'observed_after'})
        row.submission_hash = hashlib.sha256(old_json.encode()).hexdigest()
        entry = old_payload.model_dump(mode='json', exclude={'owner_action', 'observed_after'})
    headers = {'Origin': 'http://localhost:8000', 'X-CSRF-Token': csrf(client)}
    path = '/api/entries/observation'
    retried = client.post(path, json={**entry, 'owner_action': '  '}, headers=headers)
    assert retried.status_code == 200 and retried.json()['id'] == record
    for invalid in ('\0', 'あ' * 501):
        assert client.post(path, json={**entry, 'observed_after': invalid}, headers=headers).status_code == 422


def test_episode_migration_preserves_old_records_and_refuses_data_loss(client):
    record = observation(client, '移行前のメモ。')
    env = {**os.environ, 'DATABASE_URL': 'postgresql+psycopg://meme_owner:test-owner-only@db:5432/meme'}
    previous = 'b7e2d9a4106c'

    def migrate(command, revision, *, check=True):
        return subprocess.run(['alembic', command, revision], env=env,
                              capture_output=True, text=True, check=check)

    migrate('downgrade', previous)
    migrate('upgrade', 'head')
    url = f'/api/observations/{record}'
    saved = client.get(url).json()
    assert saved['description'] == '移行前のメモ。'
    assert saved['owner_action'] is None and saved['observed_after'] is None

    edited = post(client, f'/entries/observation/{record}/save', {
        'occurred_at': saved['occurred_at'], 'description': saved['description'],
        'expected_revision': saved['revision'], 'submission_key': str(uuid4()),
        'observed_after': 'その後は見ていない。',
    })
    assert edited.status_code == 303
    try:
        denied = migrate('downgrade', previous, check=False)
        assert denied.returncode != 0 and 'やり取りの記録を失う' in denied.stderr
    finally:
        migrate('upgrade', 'head')
    assert client.get(url).json()['observed_after'] == 'その後は見ていない。'


def test_observation_form_accepts_all_fields_at_maximum_length(client):
    text = '🐈'
    response = post(client, '/entries/observation/save', {
        'occurred_at': '2026-10-06T12:00', 'description': text * 10000,
        'owner_action': text * 500, 'observed_after': text * 500,
        'location': text * 200, 'submission_key': str(uuid4()),
    })
    assert response.status_code == 303
