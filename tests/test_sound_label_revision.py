"""二つの編集画面や遅れて届いた評価で、分類とメモを上書きしない。"""
import re

import pytest
from sqlalchemy import select

from app.db import transaction
from app.models import DictionaryItem, Media, SoundAnalysis, SoundBehavior
from app.voices import label_values
from test_app import client, observation, post
from test_voice_migration import legacy_sound
from test_voices import api


@pytest.fixture
def labelled_sound(client):
    # 実ファイルや音声解析を使わず、隔離DBに架空の区間を作る。
    sid = legacy_sound(client)
    with transaction() as db:
        oid = db.get(Media, db.get(SoundAnalysis, sid).media_id).observation_id
    response = api(client, f'/api/observations/{oid}/sounds/{sid}/labels', {
        'expected_revision': 1, 'meaning': 'preset:food', 'assessment': 'supports',
        'observation': '架空の元の観察', 'hypothesis': '架空の元の予想',
        'unknown': '架空の元の不明点', 'owner_response': '架空の元の対応', 'behaviors': ['look']})
    assert response.status_code == 200, response.text
    return oid, sid, response.json()


def snapshot(sid):
    with transaction() as db:
        return (dict(db.execute(select(SoundAnalysis.__table__).where(SoundAnalysis.id == sid)).mappings().one()),
                list(db.execute(select(SoundBehavior.__table__).where(SoundBehavior.sound_analysis_id == sid))),
                list(db.execute(select(DictionaryItem.__table__).order_by(DictionaryItem.id))))


def form_revision(page, url):
    form = re.search(r'<form method="post" action="' + re.escape(url) + r'">(.*?)</form>', page, re.S)[1]
    return re.search(r'name="expected_revision" value="([0-9]+)"', form)[1]


def test_stale_browser_save_keeps_new_labels_and_unsaved_text(client, labelled_sound):
    oid, sid, initial = labelled_sound
    url = f'/observations/{oid}/sound-result/{sid}/labels'
    detail = f'/entries/observation/{oid}'
    first = form_revision(client.get(detail).text, url)
    second = form_revision(client.get(detail).text, url)
    assert first == second == str(initial['revision'])
    response = post(client, url, {'expected_revision': first, 'meaning': 'preset:play',
        'assessment': 'contradicts', 'observation': '架空の新しい観察', 'owner_response': '架空の新しい対応',
        'hypothesis': '架空の新しい予想', 'unknown': '架空の新しい不明点', 'behaviors': ['door']})
    assert response.status_code == 303, response.text
    before = snapshot(sid)
    assert before[0]['revision'] == initial['revision'] + 1
    typed = '未保存の文章 </textarea><script>alert(1)</script>'
    response = post(client, url, {'expected_revision': second,
        'meaning': f'item:{initial["meaning"]["id"]}', 'assessment': 'supports',
        'observation': typed, 'owner_response': '古い画面での対応', 'hypothesis': '古い画面での予想',
        'unknown': '古い画面での不明点', 'behaviors': ['look']})
    assert response.status_code == 409
    assert '再読み込み' in response.text and '処理を完了できませんでした' in response.text
    assert '未保存の文章 &lt;/textarea&gt;&lt;script&gt;' in response.text
    assert '<script>alert(1)</script>' not in response.text
    assert all(note in response.text for note in ['古い画面での対応', '古い画面での予想', '古い画面での不明点'])
    # 文章以外の未保存の選択も、読める名前で残す。
    assert all(choice in response.text for choice in ['ごはんがほしい', '合っていそう', 'こちらを見る'])
    assert snapshot(sid) == before


def test_matching_revision_increments_even_for_same_labels(client, labelled_sound):
    oid, sid, initial = labelled_sound
    url = f'/api/observations/{oid}/sounds/{sid}'
    current = client.get(url).json()
    assert current == initial
    for revision in (initial['revision'], initial['revision'] + 1):
        response = api(client, url + '/labels', {'expected_revision': revision, 'assessment': 'contradicts'})
        assert response.status_code == 200, response.text
        assert response.json()['revision'] == revision + 1
        assert response.json()['meaning']['assessment'] == 'contradicts'
        assert response.json()['observation'] == initial['observation']
    with transaction() as db:
        assert label_values(db, db.get(SoundAnalysis, sid))['revision'] == initial['revision'] + 2
    other = observation(client, '架空の別の観察')
    assert client.get(f'/api/observations/{other}/sounds/{sid}').status_code == 404
    assert client.get(f'/api/observations/{oid}/sounds/9223372036854775807').status_code == 404
    report = client.get('/api/analysis/voices').json()
    # 集計の区間にも版番号が付く。
    assert report['segments'][0]['revision'] == initial['revision'] + 2
    assert report['totals']['segments'] == 1
    comparison = client.get(f'/api/analysis/observations?ids={oid}').json()
    assert comparison['observations'][0]['sound_notes'][0]['revision'] == initial['revision'] + 2


@pytest.mark.parametrize('assessment', ['supports', 'contradicts'])
def test_delayed_assessment_cannot_attach_to_changed_meaning(client, labelled_sound, assessment):
    oid, sid, initial = labelled_sound
    url = f'/api/observations/{oid}/sounds/{sid}/labels'
    response = api(client, url, {'expected_revision': initial['revision'], 'meaning': 'preset:play'})
    assert response.status_code == 200, response.text
    assert response.json()['meaning']['assessment'] == 'unknown'
    before = snapshot(sid)
    response = api(client, url, {'expected_revision': initial['revision'], 'assessment': assessment})
    assert response.status_code == 409 and '再読み込み' in response.json()['detail']
    assert snapshot(sid) == before


@pytest.mark.parametrize('browser', [False, True])
@pytest.mark.parametrize('revision', [None, 0, -1, True, 1.5, 'bad', 9223372036854775808])
def test_missing_or_invalid_revision_is_conflict(client, labelled_sound, browser, revision):
    oid, sid, _ = labelled_sound
    payload = {'assessment': 'contradicts', 'observation': '拒否される変更'}
    if revision is not None:
        payload['expected_revision'] = revision
    before = snapshot(sid)
    response = (post(client, f'/observations/{oid}/sound-result/{sid}/labels', payload) if browser else
                api(client, f'/api/observations/{oid}/sounds/{sid}/labels', payload))
    assert response.status_code == 409
    assert snapshot(sid) == before


@pytest.mark.parametrize('invalid', [None, True, 2.0, '2'])
def test_json_revision_is_not_coerced(client, labelled_sound, invalid):
    oid, sid, initial = labelled_sound
    assert initial['revision'] == 2
    before = snapshot(sid)
    response = api(client, f'/api/observations/{oid}/sounds/{sid}/labels', {
        'expected_revision': invalid, 'assessment': 'contradicts'})
    assert response.status_code == 409
    assert snapshot(sid) == before


def test_conflict_page_keeps_draft_while_maintenance_lock_is_held(client, labelled_sound, monkeypatch):
    oid, sid, initial = labelled_sound
    from contextlib import contextmanager
    from fastapi import HTTPException
    from app.routers import entries

    @contextmanager
    def locked():
        # 出力中の保守ロックと同じく、DB処理を409で断る。
        raise HTTPException(409, '保守処理中です。少し待ってから再試行してください。')
        yield

    monkeypatch.setattr(entries, 'transaction', locked)
    response = post(client, f'/observations/{oid}/sound-result/{sid}/labels', {
        'expected_revision': str(initial['revision']), 'meaning': 'preset:play', 'assessment': 'supports',
        'observation': 'ロック中に書いた架空の観察', 'owner_response': '', 'hypothesis': '', 'unknown': '',
        'behaviors': ['look']})
    assert response.status_code == 409
    assert all(text in response.text for text in ['ロック中に書いた架空の観察', '遊びたい', '合っていそう', 'こちらを見る'])
