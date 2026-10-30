"""YAMNetによる猫の声らしい区間の候補を検証する。第三者の録音はリポジトリに入れない。"""
import math

import pytest

from app import sound_events
from app.acoustics import RATE
from app.config import settings
from test_app import client
from test_voices import api, saved
from test_voice_match import chirp_video


def row(start, cat, meow=0.0, purr=0.0, speech=0.0):
    return {'start': start, 'cat': cat, 'speech': speech,
            'kinds': {'Meow': meow, 'Purr': purr, 'Hiss': 0.0, 'Caterwaul': 0.0}}


def test_spans_merge_windows_trim_silence_and_name_the_kind():
    levels = [0.0] * 10 + [0.2] * 6 + [0.0] * 24          # 1.0〜1.6秒だけ音がある
    rows = [row(0.0, 0.05), row(0.4875, 0.8, meow=0.7), row(0.975, 0.6, meow=0.5, speech=0.4), row(1.4625, 0.1)]
    [span] = sound_events.spans(rows, levels)
    assert (span['start'], span['end']) == (1.0, 1.6)
    assert span['kind'] == 'Meow' and span['kind_label'] == 'ニャー'
    assert span['strength'] == 'high' and span['speech'] is True
    weak = sound_events.spans([row(0.0, 0.4, purr=0.05)], [0.1] * 10)
    assert weak[0]['kind_label'] == '猫の声' and weak[0]['strength'] == 'mid'
    assert sound_events.spans([row(0.0, 0.2)], [0.1] * 10) == []


def test_model_checksum_and_labels_are_the_pinned_ones():
    model = sound_events.load()
    if model is None:
        pytest.fail('YAMNetモデルが必要です（scripts/fetch_sound_model.py）。')
    assert len(model['labels']) == 521
    assert {'Cat', 'Meow', 'Purr', 'Hiss', 'Caterwaul', 'Speech'} <= set(model['labels'])


def test_pure_tone_and_silence_are_not_cat_voices():
    tone = [0.3 * math.sin(2 * math.pi * 1000 * i / RATE) for i in range(RATE * 2)]
    for values in ([0.0] * RATE * 2, tone):
        result = sound_events.detect(values)
        assert result['available'] is True and result['spans'] == []
    assert sound_events.detect([0.0] * 100)['windows'] == 1        # 1窓に満たない音も0で埋めて判定する


def test_missing_model_falls_back_to_loudness(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'sound_model_path', tmp_path / 'missing.tflite')
    result = sound_events.detect([0.0] * RATE)
    assert result == {'available': False, 'spans': [], 'notice': '猫の声の判定モデルがありません。音量の候補を使ってください。'}


def test_loudness_api_includes_cat_candidates(client, tmp_path):
    oid, mid = saved(client, chirp_video(tmp_path / 'chirp.mp4', '600*(t-0.4)'))
    data = api(client, f'/api/observations/{oid}/media/{mid}/loudness', {}).json()
    assert data['cat_available'] is True and data['cat_candidates'] == []
    assert 'メメの声か' in data['cat_notice'] and isinstance(data['candidates'], list)
