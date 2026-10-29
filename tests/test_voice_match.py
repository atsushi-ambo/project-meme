"""声のかたちの比較、似ている声の一覧、声の地図を検証する。"""
import math
import subprocess

import pytest
from sqlalchemy import select

from app import acoustics
from app.db import transaction
from app.models import Observation, SoundAnalysis
from test_app import client, csrf, post
from test_voices import api, call_type_id, saved, tone


def chirp_video(path, expression, seconds=1.0):
    """0.4秒の無音のあと、式で決めた周波数の声のような音。"""
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f'color=c=gray:s=64x48:r=10:d={seconds + 0.8}',
        '-f', 'lavfi', '-i', f"aevalsrc='if(between(t,0.4,{0.4 + seconds}),0.3*sin(2*PI*({expression})),0)':s=48000:d={seconds + 0.8}",
        '-c:v', 'libx264', '-threads:v', '1', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(path)],
        check=True, capture_output=True, timeout=30)
    return path.read_bytes()


@pytest.fixture(scope='module')
def videos(tmp_path_factory):
    folder = tmp_path_factory.mktemp('chirps')
    # 位相の式。周波数は 上昇: 450→850Hz、少し高い上昇: 500→900Hz、平ら: 900Hz。
    return {
        'rise': chirp_video(folder / 'rise.mp4', '450*(t-0.4)+200*(t-0.4)^2'),
        'rise2': chirp_video(folder / 'rise2.mp4', '500*(t-0.4)+200*(t-0.4)^2'),
        'flat': chirp_video(folder / 'flat.mp4', '900*(t-0.4)', seconds=0.4),
    }


def segment(client, video, call_type=None, meaning=None, assessment='unknown', end='1.4'):
    oid, mid = saved(client, video)
    body = {'media_id': mid, 'start': '0.4', 'end': end}
    if call_type:
        body['call_type_id'] = call_type_id(call_type)
    if meaning:
        body.update(meaning=meaning, assessment=assessment)
    response = api(client, f'/api/observations/{oid}/sounds', body)
    assert response.status_code == 200, response.text
    return oid, response.json()['id']


def test_voice_distance_prefers_the_same_shape():
    def features(shape, seconds=1.0):
        return acoustics.features([0.0] * 3200 + tone(shape, seconds) + [0.0] * 3200)
    arch = features(lambda t: 500 + 300 * math.sin(math.pi * t))
    arch_higher = features(lambda t: 560 + 330 * math.sin(math.pi * t), 0.9)
    rise = features(lambda t: 450 + 400 * t)
    short_flat = features(lambda t: 900, 0.4)
    assert len(arch['shape_st']) == acoustics.SHAPE_POINTS and arch['version'] == 2
    near = acoustics.voice_distance(arch, arch_higher)
    middle = acoustics.voice_distance(arch, rise)
    far = acoustics.voice_distance(arch, short_flat)
    assert near['distance'] < middle['distance'] < far['distance']
    assert near['shape_st'] < 0.5 and acoustics.closeness(near['distance']) in ('とても近い', '近い')
    assert acoustics.closeness(far['distance']) == '離れている'
    assert acoustics.voice_distance(arch, {'version': 1, 'pitch_hz': None}) is None
    assert acoustics.voice_distance(arch, None) is None
    assert acoustics.features([0.0] * 16000)['shape_st'] is None


def test_similar_voices_rank_hint_and_stay_read_only(client, videos):
    _, rise_id = segment(client, videos['rise'], '短い「ニャッ」', 'preset:food', 'supports')
    _, flat_id = segment(client, videos['flat'], 'うなる・「シャー」', end='0.8')
    oid, target_id = segment(client, videos['rise2'])

    report = client.get(f'/api/analysis/voices/{target_id}/similar').json()
    assert report['measured'] is True
    assert [n['id'] for n in report['neighbors']] == [rise_id, flat_id]
    first, second = report['neighbors']
    assert first['distance'] < second['distance'] and first['closeness'] in ('とても近い', '近い')
    assert first['meaning']['title'] == 'ごはんがほしい' and first['meaning']['assessment'] == 'supports'
    assert report['hints']['call_types'] == [{'name': '短い「ニャッ」', 'count': 1}]
    assert report['hints']['meanings'][0]['counts'] == {'supports': 1, 'contradicts': 0, 'unknown': 0}

    # 参考を表示しても、種類や予想は自動で付かない。
    with transaction() as db:
        target = db.get(SoundAnalysis, target_id)
        assert target.call_type_id is None and target.dictionary_item_id is None

    page = client.get(f'/entries/observation/{oid}').text
    assert '似ている声' in page and '参考：「近い」以上の声' in page and '<polyline' in page
    assert page.index(f'#sound-{rise_id}') < page.index(f'#sound-{flat_id}')

    voices = client.get('/voices').text
    assert '声の地図' in voices and 'voice-map' in voices and '声のかたち' in voices
    assert client.get('/api/analysis/voices').json()['totals']['with_shape'] == 3
    assert client.get('/api/analysis/voices/999999/similar').status_code == 404


def test_samples_are_not_suggested_and_old_measurements_can_be_upgraded(client, videos):
    sample_oid, sample_id = segment(client, videos['rise'], '短い「ニャッ」')
    oid, target_id = segment(client, videos['rise2'])
    with transaction() as db:
        db.get(Observation, sample_oid).is_sample = True
    assert client.get(f'/api/analysis/voices/{target_id}/similar').json()['neighbors'] == []

    # 以前の版（声のかたちなし）は比較できず、測り直しで比較できるようになる。
    with transaction() as db:
        result = db.get(SoundAnalysis, target_id)
        result.features = {key: value for key, value in result.features.items() if key != 'shape_st'} | {'version': 1}
    assert client.get(f'/api/analysis/voices/{target_id}/similar').json()['measured'] is False
    assert '声のかたちを測って似ている声を探す' in client.get(f'/entries/observation/{oid}').text
    assert post(client, f'/observations/{oid}/sound-result/{target_id}/measure', {}).status_code == 303
    assert client.get(f'/api/analysis/voices/{target_id}/similar').json()['measured'] is True
