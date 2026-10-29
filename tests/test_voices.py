"""声の区間の分類・その後・声の高さと、鳴き声の傾向の集計を検証する。"""
import math
import re
import subprocess

import pytest
from sqlalchemy import select

from app import acoustics
from app.db import transaction
from app.models import CallType, DictionaryItem, Media, SoundAnalysis, SoundBehavior
from test_app import client, csrf, observation, post, upload
from test_media_info import info_post


@pytest.fixture(scope='module')
def meow_video(tmp_path_factory):
    """0.5秒の無音のあと、600Hzの音が0.6秒、また無音。合計1.6秒の小さな動画。"""
    path = tmp_path_factory.mktemp('meow') / 'meow.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=gray:s=64x48:r=10:d=1.6',
        '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=mono:d=0.5',
        '-f', 'lavfi', '-i', 'sine=frequency=600:sample_rate=48000:duration=0.6',
        '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=mono:d=0.5',
        '-filter_complex', '[1:a][2:a][3:a]concat=n=3:v=0:a=1[a]', '-map', '0:v', '-map', '[a]',
        '-c:v', 'libx264', '-threads:v', '1', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-t', '1.6', str(path)],
        check=True, capture_output=True, timeout=30)
    return path.read_bytes()


def saved(client, data, text='夕方、台所でこちらを見て鳴いた。'):
    oid = observation(client, text)
    mid, response = upload(client, oid, data, 'meow.mp4')
    assert response.status_code == 200, response.text
    assert info_post(client, mid).status_code == 200
    return oid, mid


def api(client, url, payload):
    return client.post(url, json=payload, headers={'Origin': 'http://localhost:8000', 'X-CSRF-Token': csrf(client)})


def call_type_id(name):
    with transaction() as db:
        return db.scalar(select(CallType.id).where(CallType.name == name))


def test_default_call_types_are_seeded(client):
    with transaction() as db:
        names = list(db.scalars(select(CallType.name).order_by(CallType.id)))
    assert names[0] == '短い「ニャッ」' and 'まだ分からない' in names
    page = client.get('/voices').text
    assert '鳴き声の傾向' in page and 'まだ声の区間がありません' in page and '短い「ニャッ」' in page


def test_segment_labels_pitch_and_trends(client, meow_video):
    oid, mid = saved(client, meow_video)
    short = call_type_id('短い「ニャッ」')
    data = {'start': '0.5', 'end': '1.1', 'call_type_id': str(short), 'call_count': '1',
            'behaviors': ['look', 'bowl'], 'meaning': 'preset:food', 'assessment': 'supports',
            'owner_response': 'ごはんを出した', 'outcome': 'settled', 'observation': '一声だけ'}
    response = client.post(f'/observations/{oid}/sound/{mid}', data={'csrf': csrf(client), **data},
                           headers={'Origin': 'http://localhost:8000'}, follow_redirects=False)
    assert response.status_code == 303
    with transaction() as db:
        result = db.scalar(select(SoundAnalysis).where(SoundAnalysis.media_id == mid))
        assert result.call_type_id == short and result.outcome == 'settled' and result.assessment == 'supports'
        assert 590 <= result.features['pitch_hz']['median'] <= 610 and result.features['contour'] == 'flat'
        assert result.features['voiced_ms'] >= 400
        assert sorted(db.scalars(select(SoundBehavior.behavior))) == ['bowl', 'look']
        item = db.get(DictionaryItem, result.dictionary_item_id)
        assert item.title == 'ごはんがほしい'
        result_id = result.id
    page = client.get(f'/entries/observation/{oid}').text
    assert 'ごはんを出した' in page and '鳴きやんだ・満足そうだった' in page
    assert re.search(r'約(59[0-9]|60[0-9]|610)Hz', page)

    # 同じ種類の別の区間は、予想と合わない例として残す。
    oid2, mid2 = saved(client, meow_video, '朝、ドアの前で鳴いた。')
    assert api(client, f'/api/observations/{oid2}/sounds', {
        'media_id': mid2, 'start': '0.4', 'end': '1.2', 'call_type_id': short, 'behaviors': ['door'],
        'meaning': f'item:{item.id}', 'assessment': 'contradicts', 'outcome': 'repeated'}).status_code == 200

    report = client.get('/api/analysis/voices').json()
    assert report['totals']['segments'] == 2 and report['totals']['with_meaning'] == 2
    group = next(g for g in report['call_types'] if g['id'] == short)
    assert group['segments'] == 2 and group['observations'] == 2 and group['few_examples_notice']
    assert group['meanings'] == [{'id': item.id, 'title': 'ごはんがほしい', 'url': f'/dictionary/{item.id}',
                                  'counts': {'supports': 1, 'contradicts': 1, 'unknown': 0}}]
    assert {row['key']: row['count'] for row in group['outcomes']} == {'settled': 1, 'repeated': 1}
    assert {row['key'] for row in group['behaviors']} == {'look', 'bowl', 'door'}
    assert 590 <= group['acoustics']['pitch_hz']['median'] <= 610
    meaning = report['meanings'][0]
    assert meaning['call_types'][0]['name'] == '短い「ニャッ」' and meaning['call_types'][0]['count'] == 2
    page = client.get('/voices').text
    assert 'ごはんがほしい' in page and '合わなそう <b>1</b>' in page

    # あとから分かったことを書き足す。測定値は変えない。
    response = post(client, f'/observations/{oid}/sound-result/{result_id}/labels',
                    {'expected_revision': result.revision, 'call_type_id': '', 'outcome': 'left', 'owner_response': '撫でた'})
    assert response.status_code == 303
    with transaction() as db:
        result = db.get(SoundAnalysis, result_id)
        assert result.call_type_id is None and result.outcome == 'left' and result.dictionary_item_id is None
        assert result.assessment == 'unknown' and result.features['pitch_hz']
        assert list(db.scalars(select(SoundBehavior).where(SoundBehavior.sound_analysis_id == result_id))) == []


def test_api_label_update_merges_and_validates(client, meow_video):
    oid, mid = saved(client, meow_video)
    created = api(client, f'/api/observations/{oid}/sounds', {
        'media_id': mid, 'start': '0.5', 'end': '1.1', 'behaviors': ['look'], 'owner_response': '声をかけた'})
    assert created.status_code == 200, created.text
    body = created.json()
    assert body['revision'] == 1
    assert body['call_type'] is None and body['features']['pitch_hz']
    assert api(client, f'/api/observations/{oid}/sounds', {'media_id': mid, 'start': '0.5', 'end': '1.1'}).status_code == 409
    url = f'/api/observations/{oid}/sounds/{body["id"]}/labels'
    updated = api(client, url, {'expected_revision': body['revision'], 'outcome': 'settled'}).json()
    assert updated['outcome']['key'] == 'settled' and updated['owner_response'] == '声をかけた'
    assert [b['key'] for b in updated['behaviors']] == ['look']
    for bad in [{'behaviors': ['meow_means_food']}, {'outcome': 'happy'}, {'call_type_id': 999999},
                {'meaning': 'preset:nope'}, {'call_count': 0}, {'unexpected': 1}]:
        assert api(client, url, {'expected_revision': updated['revision'], **bad}).status_code in (404, 422), bad
    other = observation(client, '別の記録')
    assert api(client, f'/api/observations/{other}/sounds/{body["id"]}/labels', {'outcome': 'left'}).status_code == 404
    assert client.post(url, json={'outcome': 'left'}, headers={'Origin': 'http://localhost:8000'}).status_code == 403


@pytest.mark.parametrize('assessment', ['supports', 'contradicts'])
@pytest.mark.parametrize('change', ['preset', 'item', 'clear', 'same_preset', 'same_item', 'unrelated', 'explicit'])
def test_api_meaning_change_resets_only_implicit_assessment(client, meow_video, assessment, change):
    oid, mid = saved(client, meow_video)
    created = api(client, f'/api/observations/{oid}/sounds', {
        'media_id': mid, 'start': '0.5', 'end': '1.1',
        'meaning': 'preset:food', 'assessment': assessment, 'owner_response': '声をかけた'})
    assert created.status_code == 200, created.text
    body = created.json()
    old_id = body['meaning']['id']
    with transaction() as db:
        other = DictionaryItem(cat_id=1, kind='hypothesis', title='別の予想', hypothesis='遊びたいかもしれない')
        db.add(other)
        db.flush()
        other_id = other.id
    explicit = 'contradicts' if assessment == 'supports' else 'supports'
    payload = {
        'preset': {'meaning': 'preset:play'},
        'item': {'meaning': f'item:{other_id}'},
        'clear': {'meaning': ''},
        'same_preset': {'meaning': 'preset:food'},
        'same_item': {'meaning': f'item:{old_id}'},
        'unrelated': {'outcome': 'left'},
        'explicit': {'meaning': 'preset:play', 'assessment': explicit},
    }[change]
    response = api(client, f'/api/observations/{oid}/sounds/{body["id"]}/labels', {'expected_revision': body['revision'], **payload})
    assert response.status_code == 200, response.text
    expected = ('unknown' if change in {'preset', 'item', 'clear'} else
                explicit if change == 'explicit' else assessment)
    with transaction() as db:
        result = db.get(SoundAnalysis, body['id'])
        assert result.assessment == expected
        assert result.owner_response == '声をかけた'
        if change == 'clear':
            assert result.dictionary_item_id is None
        elif change in {'same_preset', 'same_item', 'unrelated'}:
            assert result.dictionary_item_id == old_id
        elif change == 'item':
            assert result.dictionary_item_id == other_id
        else:
            assert result.dictionary_item_id != old_id


def test_loudness_candidates_and_remeasure(client, meow_video):
    oid, mid = saved(client, meow_video)
    url = f'/api/observations/{oid}/media/{mid}/loudness'
    assert client.get(url).status_code == 405
    data = api(client, url, {}).json()
    assert len(data['levels']) == 16 and data['truncated'] is False
    assert len(data['candidates']) == 1
    span = data['candidates'][0]
    assert 0.4 <= span['start'] <= 0.6 and 1.0 <= span['end'] <= 1.2
    # 以前の分析（声の高さなし）にあとから測定を追加できる。
    assert post(client, f'/observations/{oid}/sound/{mid}', {'start': '0.5', 'end': '1.1'}).status_code == 303
    with transaction() as db:
        result = db.scalar(select(SoundAnalysis).where(SoundAnalysis.media_id == mid))
        result.features = None
        result_id = result.id
    assert '声の高さ・抑揚・長さも測る' in client.get(f'/entries/observation/{oid}').text
    assert post(client, f'/observations/{oid}/sound-result/{result_id}/measure', {}).status_code == 303
    with transaction() as db:
        assert db.get(SoundAnalysis, result_id).features['pitch_hz']['median'] > 500
        db.get(Media, mid).status = 'deleted'
    assert api(client, url, {}).status_code == 404
    assert post(client, f'/observations/{oid}/sound-result/{result_id}/measure', {}).status_code == 404


def test_call_types_can_be_added_and_renamed(client):
    assert post(client, '/call-types/save', {'name': '朝の「ニャニャッ」', 'description': '2回'}).status_code == 303
    assert post(client, '/call-types/save', {'name': '朝の「ニャニャッ」'}).status_code == 409
    assert post(client, '/call-types/save', {'name': ''}).status_code == 422
    new_id = call_type_id('朝の「ニャニャッ」')
    assert post(client, f'/call-types/{new_id}/save', {'name': '朝の二声', 'description': ''}).status_code == 303
    assert post(client, f'/call-types/{new_id}/save', {'name': 'ゴロゴロ'}).status_code == 409
    assert post(client, '/call-types/999999/save', {'name': 'x'}).status_code == 404
    assert '朝の二声' in client.get('/voices').text


def test_delete_all_removes_custom_call_types_and_restores_defaults(client):
    from app.services import CALL_TYPE_PRESETS
    assert post(client, '/call-types/save', {'name': '架空の専用名', 'description': '架空の私用説明'}).status_code == 303
    first = call_type_id(CALL_TYPE_PRESETS[0][0])
    second = call_type_id(CALL_TYPE_PRESETS[1][0])
    assert post(client, f'/call-types/{first}/save', {'name': '架空の変更名', 'description': '架空の変更説明'}).status_code == 303
    assert post(client, f'/call-types/{second}/save', {'name': CALL_TYPE_PRESETS[1][0], 'description': '架空の追記説明'}).status_code == 303
    private = ('架空の専用名', '架空の私用説明', '架空の変更名', '架空の変更説明', '架空の追記説明')
    for url in ('/voices', '/api/analysis/voices'):
        before = client.get(url).text
        assert all(value in before for value in private)
    assert post(client, '/delete-all', {'confirm': 'メメの全記録を削除'}).status_code == 303
    with transaction() as db:
        assert list(db.execute(select(CallType.name, CallType.description).order_by(CallType.id))) == CALL_TYPE_PRESETS
    for url in ('/voices', '/api/analysis/voices'):
        response = client.get(url)
        assert response.status_code == 200
        assert all(value not in response.text for value in private)
    choices = client.get('/api/analysis/voices').json()['choices']['call_types']
    assert [(row['name'], row['description']) for row in choices] == CALL_TYPE_PRESETS


def test_samples_and_deleted_records_are_not_in_trends(client, meow_video):
    from app.models import Observation
    oid, mid = saved(client, meow_video)
    assert post(client, f'/observations/{oid}/sound/{mid}', {'start': '0.5', 'end': '1.1'}).status_code == 303
    assert client.get('/api/analysis/voices').json()['totals']['segments'] == 1
    with transaction() as db:
        db.get(Observation, oid).is_sample = True
    assert client.get('/api/analysis/voices').json()['totals']['segments'] == 0


def tone(hz_at, seconds=1.0):
    phase, out = 0.0, []
    for i in range(int(acoustics.RATE * seconds)):
        phase += 2 * math.pi * hz_at(i / (acoustics.RATE * seconds)) / acoustics.RATE
        out.append(0.3 * (math.sin(phase) + 0.5 * math.sin(2 * phase)) / 1.5)
    return out


@pytest.mark.parametrize('shape,expected', [
    (lambda t: 450 + 400 * t, 'rising'), (lambda t: 850 - 400 * t, 'falling'),
    (lambda t: 500 + 300 * math.sin(math.pi * t), 'arch'), (lambda t: 700, 'flat')])
def test_pitch_contours(shape, expected):
    features = acoustics.features([0.0] * 3200 + tone(shape) + [0.0] * 3200)
    assert features['contour'] == expected
    assert 900 <= features['voiced_ms'] <= 1100


def test_noise_and_silence_have_no_pitch():
    import random
    random.seed(3)
    assert acoustics.features([0.0] * 16000)['pitch_hz'] is None
    noisy = acoustics.features([random.gauss(0, 0.2) for _ in range(16000)])
    assert noisy['voiced_ms'] < 200
    assert acoustics.loud_spans([]) == [] and acoustics.features([])['contour'] == 'unknown'


@pytest.mark.parametrize('assessment', ['supports', 'contradicts'])
@pytest.mark.parametrize('change', ['changed', 'unchanged', 'reselected', 'clear'])
def test_form_meaning_assessment_binding(client, meow_video, assessment, change):
    oid, mid = saved(client, meow_video)
    created = api(client, f'/api/observations/{oid}/sounds', {
        'media_id': mid, 'start': '0.5', 'end': '1.1',
        'meaning': 'preset:food', 'assessment': assessment})
    assert created.status_code == 200, created.text
    body = created.json()
    old_meaning = f'item:{body["meaning"]["id"]}'
    page = client.get(f'/entries/observation/{oid}').text
    assert f'name="assessment_for" value="{old_meaning}"' in page
    assert 'name="assessment_for" value=""' in page
    meaning = old_meaning if change == 'unchanged' else '' if change == 'clear' else 'preset:play'
    response = post(client, f'/observations/{oid}/sound-result/{body["id"]}/labels', {
        'expected_revision': body['revision'], 'meaning': meaning, 'assessment': assessment,
        'assessment_for': meaning if change == 'reselected' else old_meaning})
    assert response.status_code == 303, response.text
    with transaction() as db:
        result = db.get(SoundAnalysis, body['id'])
        assert result.assessment == ('unknown' if change in {'changed', 'clear'} else assessment)
        if change == 'clear':
            assert result.dictionary_item_id is None
        else:
            assert (result.dictionary_item_id == body['meaning']['id']) == (change == 'unchanged')


@pytest.mark.parametrize('assessment_for,expected', [
    ('', 'unknown'), ('preset:food', 'unknown'), ('preset:play', 'supports')])
def test_create_form_meaning_assessment_binding(client, meow_video, assessment_for, expected):
    oid, mid = saved(client, meow_video)
    response = post(client, f'/observations/{oid}/sound/{mid}', {
        'start': '0.5', 'end': '1.1', 'meaning': 'preset:play',
        'assessment': 'supports', 'assessment_for': assessment_for})
    assert response.status_code == 303, response.text
    with transaction() as db:
        result = db.scalar(select(SoundAnalysis).where(SoundAnalysis.media_id == mid))
        assert result.assessment == expected
        assert db.get(DictionaryItem, result.dictionary_item_id).title == '遊びたい'
