"""声の区間を保存するCLIの下書き検証と送信内容。"""
import json
from pathlib import Path
import runpy

import pytest


@pytest.fixture
def module(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / 'scripts'))
    return runpy.run_path(str(root / 'scripts/voice.py'))


def write(tmp_path, data):
    path = tmp_path / 'draft.json'
    path.write_text(json.dumps(data, ensure_ascii=False))
    return path


@pytest.mark.parametrize('data', [
    {'observation_id': 1, 'media_id': 2, 'start': '0.5'},
    {'observation_id': 1, 'media_id': True, 'start': '0', 'end': '1'},
    {'observation_id': 0, 'media_id': 2, 'start': '0', 'end': '1'},
    {'observation_id': 1, 'media_id': 2, 'start': '0', 'end': '1', 'translation': 'ごはん'},
    {'observation_id': 1, 'sound_id': 3},
    {'observation_id': 1, 'sound_id': 3, 'media_id': 2},
    [],
])
def test_invalid_drafts_are_rejected(module, tmp_path, data):
    with pytest.raises((ValueError, TypeError)):
        module['load_draft'](write(tmp_path, data))


def test_new_and_update_requests(module, tmp_path):
    calls = []
    class Client:
        origin = 'http://localhost:8000'
        def request(self, method, path, body=None):
            calls.append((method, path, body))
            return {'observation_url': '/entries/observation/1#sound-5'}
    new = module['load_draft'](write(tmp_path, {'observation_id': 1, 'media_id': 2, 'start': 0.5, 'end': '1.1',
                                                'behaviors': ['look'], 'outcome': 'settled'}))
    module['submit'](new, Client())
    update = module['load_draft'](write(tmp_path, {'observation_id': 1, 'sound_id': 5, 'assessment': 'supports'}))
    module['submit'](update, Client())
    assert calls == [
        ('POST', '/api/observations/1/sounds', {'media_id': 2, 'start': '0.5', 'end': '1.1',
                                                'behaviors': ['look'], 'outcome': 'settled'}),
        ('POST', '/api/observations/1/sounds/5/labels', {'assessment': 'supports'})]
