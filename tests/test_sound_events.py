"""YAMNetによる猫の声らしい区間の候補を検証する。第三者の録音はリポジトリに入れない。"""
import math
import json
import os
from pathlib import Path
import subprocess
import sys
import wave

import pytest

from app import sound_events
from app.acoustics import RATE
from app.config import settings
from test_app import client
from test_voices import api, saved
from test_voice_match import chirp_video


def row(start, cat, meow=0.0, purr=0.0, speech=0.0, human=None):
    return {'start': start, 'cat': cat, 'speech': speech, 'human': speech if human is None else human,
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


def test_human_voice_and_weak_cat_spans_are_separate():
    levels = [0.2] * 40
    rows = [row(0.0, 0.05, speech=0.8), row(0.4875, 0.05, human=0.6), row(0.975, 0.2, meow=0.15),
            row(1.4625, 0.25, meow=0.2), row(1.95, 0.02), row(2.4375, 0.9, meow=0.8)]
    [human] = sound_events.human_spans(rows, levels)
    assert (human['start'], human['end']) == (0.0, 1.5) and human['strength'] == 'high' and human['cat_peak'] == 0.05
    [weak] = sound_events.weak_spans(rows, levels)
    assert weak['strength'] == 'low' and weak['start'] == 0.9 and weak['peak'] == 0.25
    # 強い窓の直前の弱い窓も、強い区間に飲み込まれず弱い候補として残る。
    near_strong = [row(0.0, 0.2), row(0.4875, 0.8)]
    assert [s['peak'] for s in sound_events.weak_spans(near_strong, levels)] == [0.2]
    # 猫らしさが人の声らしさより強い窓（叫び声など）は人の声に数えない。
    assert sound_events.human_spans([row(0.0, 0.9, human=0.5)], levels) == []
    many = [row(i * 0.975, 0.1 + i * 0.02) if i % 2 == 0 else row(i * 0.975, 0.0) for i in range(14)]
    assert len(sound_events.weak_spans(many, levels * 10)) == sound_events.WEAK_LIMIT
    [strong] = sound_events.spans(rows, levels)          # 通常の候補は0.3以上だけで、弱い候補と重複しない
    assert strong['start'] == 2.4
    assert sound_events.timeline(rows)[0] == {'start': 0.0, 'cat': 0.05, 'human': 0.8}


def test_model_checksum_and_labels_are_the_pinned_ones():
    model = sound_events.load()
    if model is None:
        pytest.fail('YAMNetモデルが必要です（scripts/fetch_sound_model.py）。')
    assert len(model['labels']) == 521
    assert {'Cat', 'Meow', 'Purr', 'Hiss', 'Caterwaul', 'Speech'} <= set(model['labels'])
    assert set(sound_events.HUMAN_LABELS) <= set(model['labels'])


def test_pure_tone_and_silence_are_not_cat_voices():
    tone = [0.3 * math.sin(2 * math.pi * 1000 * i / RATE) for i in range(RATE * 2)]
    for values in ([0.0] * RATE * 2, tone):
        result = sound_events.detect(values)
        assert result['available'] is True and result['spans'] == [] and result['weak_spans'] == []
        assert result['human_spans'] == [] and len(result['timeline']) == result['windows']
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


@pytest.mark.parametrize('missing_model', [False, True])
def test_standalone_classifier_needs_no_database_or_session_secret(tmp_path, missing_model):
    audio_path = tmp_path / 'silence.wav'
    with wave.open(str(audio_path), 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(RATE)
        audio.writeframes(b'\x00\x00' * RATE * 2)
    env = {key: value for key, value in os.environ.items() if key not in ('DATABASE_URL', 'SECRET_KEY')}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['SOUND_MODEL_PATH'] = str(tmp_path / 'missing.tflite' if missing_model else settings.sound_model_path)
    entry = "import runpy, sys; runpy.run_module('app.sound_events', run_name='__main__'); assert 'app.config' not in sys.modules"
    result = subprocess.run([sys.executable, '-c', entry, str(audio_path)],
                            cwd=Path(__file__).resolve().parents[1], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['available'] is (not missing_model) and report['spans'] == []
