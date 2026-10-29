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
    update = module['load_draft'](write(tmp_path, {'observation_id': 1, 'sound_id': 5, 'expected_revision': 3, 'assessment': 'supports'}))
    module['submit'](update, Client())
    assert calls == [
        ('POST', '/api/observations/1/sounds', {'media_id': 2, 'start': '0.5', 'end': '1.1',
                                                'behaviors': ['look'], 'outcome': 'settled'}),
        ('POST', '/api/observations/1/sounds/5/labels', {'expected_revision': 3, 'assessment': 'supports'})]


@pytest.mark.parametrize('revision', [None, True, 0, -1, '2', 1.5, 9223372036854775808])
def test_update_requires_confirmed_integer_revision(module, tmp_path, revision):
    draft = {'observation_id': 1, 'sound_id': 5, 'assessment': 'supports'}
    if revision is not None:
        draft['expected_revision'] = revision
    with pytest.raises(ValueError, match='版番号'):
        module['load_draft'](write(tmp_path, draft))


def test_revision_alone_is_not_a_label_update(module, tmp_path):
    with pytest.raises(ValueError, match='分類'):
        module['load_draft'](write(tmp_path, {'observation_id': 1, 'sound_id': 5, 'expected_revision': 2}))


def test_show_reads_current_labels_and_revision(module, monkeypatch, capsys):
    import sys
    calls = []
    class Client:
        origin = 'http://localhost:8123'
        def __init__(self, port):
            assert port == 8123
        def request(self, method, path):
            calls.append((method, path))
            return {'id': 5, 'revision': 3, 'observation': '架空の確認メモ'}
    monkeypatch.setitem(module['main'].__globals__, 'LocalClient', Client)
    monkeypatch.setattr(sys, 'argv', ['voice.py', '--show', '5', '--observation', '1', '--port', '8123'])
    assert module['main']() == 0
    assert calls == [('GET', '/api/observations/1/sounds/5')]
    assert json.loads(capsys.readouterr().out) == {
        'id': 5, 'revision': 3, 'observation': '架空の確認メモ', 'base_url': Client.origin}


def test_conflict_does_not_refresh_retry_or_rewrite_draft(module, tmp_path, monkeypatch, capsys):
    import sys
    calls = []
    draft = {'observation_id': 1, 'sound_id': 5, 'expected_revision': 2, 'assessment': 'supports'}
    path = write(tmp_path, draft)
    class Client:
        def __init__(self, port):
            pass
        def request(self, method, url, body):
            calls.append((method, url, body))
            raise ValueError('HTTP 409: 画面を再読み込みしてください。')
    monkeypatch.setitem(module['main'].__globals__, 'LocalClient', Client)
    monkeypatch.setattr(sys, 'argv', ['voice.py', str(path), '--submit'])
    assert module['main']() == 1
    assert calls == [('POST', '/api/observations/1/sounds/5/labels', {'expected_revision': 2, 'assessment': 'supports'})]
    assert json.loads(path.read_text()) == draft
    assert '自動で差し替えて強制再送しない' in capsys.readouterr().err


def test_preview_does_not_read_latest_revision(module, tmp_path, monkeypatch, capsys):
    import sys
    draft = {'observation_id': 1, 'sound_id': 5, 'expected_revision': 2, 'assessment': 'supports'}
    def forbidden_client(port):
        pytest.fail('確認モードで版番号を取り直してはいけません。')
    monkeypatch.setitem(module['main'].__globals__, 'LocalClient', forbidden_client)
    monkeypatch.setattr(sys, 'argv', ['voice.py', str(write(tmp_path, draft))])
    assert module['main']() == 0
    assert '"expected_revision": 2' in capsys.readouterr().out


@pytest.mark.parametrize('args', [
    ['--show', '5'], ['--show', '5', '--observation', '1', '--submit'],
    ['draft.json', '--show', '5', '--observation', '1'], ['--observation', '1'],
    ['--show', '0', '--observation', '1'], ['--show', '5', '--observation', '-1'],
])
def test_show_rejects_invalid_arguments_without_network(module, monkeypatch, args):
    import sys
    def forbidden_client(port):
        pytest.fail('不正な引数で通信してはいけません。')
    monkeypatch.setitem(module['main'].__globals__, 'LocalClient', forbidden_client)
    monkeypatch.setattr(sys, 'argv', ['voice.py', *args])
    with pytest.raises(SystemExit) as error:
        module['main']()
    assert error.value.code == 2
