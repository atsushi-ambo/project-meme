import json
from pathlib import Path
import runpy
import sys
import uuid
import pytest


@pytest.fixture
def appender(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    return runpy.run_path(str(scripts / 'append.py'))


@pytest.fixture
def draft():
    return {'observation_id': 5, 'operation_key': str(uuid.uuid4()),
            'expected_revision': 2, 'text': '遊んだ後は鳴きやみ、ベッドの横で寝ていた。'}


def write_draft(tmp_path, draft):
    file = tmp_path / 'append.json'
    file.write_text(json.dumps(draft, ensure_ascii=False), encoding='utf-8')
    return file


def test_preview_normalizes_text_without_network(appender, draft, tmp_path, monkeypatch, capsys):
    draft['text'] = '  ' + draft['text'] + '\n'
    file = write_draft(tmp_path, draft)
    monkeypatch.setattr(sys, 'argv', ['append.py', str(file)])
    def forbidden_client(port):
        pytest.fail('確認のみではアプリへ通信してはいけません。')
    monkeypatch.setitem(appender['main'].__globals__, 'LocalClient', forbidden_client)
    assert appender['main']() == 0
    output = capsys.readouterr().out
    preview, notice = output.rsplit('\n確認のみ', 1)
    assert json.loads(preview) == {**draft, 'text': draft['text'].strip()}
    assert draft['operation_key'] in output
    assert '末尾に追記' in notice
    assert json.loads(file.read_text())['text'] == draft['text']


@pytest.mark.parametrize('field,value', [
    ('observation_id', True), ('observation_id', False), ('observation_id', 0),
    ('observation_id', -1), ('observation_id', '5'), ('observation_id', 1.5),
    ('observation_id', 9223372036854775808),
    ('expected_revision', True), ('expected_revision', 0), ('expected_revision', -1),
    ('expected_revision', '2'), ('expected_revision', 9223372036854775808),
    ('operation_key', True), ('operation_key', None), ('operation_key', 123),
    ('operation_key', 'not-a-uuid'),
    ('text', True), ('text', None), ('text', 123), ('text', ''), ('text', ' \n\t '),
    ('text', 'あ' * 10001),
])
def test_invalid_draft_refused_before_network(appender, draft, tmp_path, monkeypatch, field, value):
    draft[field] = value
    file = write_draft(tmp_path, draft)
    monkeypatch.setattr(sys, 'argv', ['append.py', str(file), '--submit'])
    def forbidden_client(port):
        pytest.fail('入力を検証する前に通信してはいけません。')
    monkeypatch.setitem(appender['main'].__globals__, 'LocalClient', forbidden_client)
    assert appender['main']() == 1


@pytest.mark.parametrize('mutation', ['missing', 'unknown', 'array'])
def test_exact_draft_structure_required(appender, draft, tmp_path, mutation):
    if mutation == 'missing':
        draft.pop('expected_revision')
    elif mutation == 'unknown':
        draft['description'] = '元の本文を置き換えない'
    else:
        draft = [draft]
    with pytest.raises(ValueError, match='下書き'):
        appender['load_draft'](write_draft(tmp_path, draft))


def test_maximum_text_and_id_are_accepted(appender, draft, tmp_path):
    draft.update(observation_id=9223372036854775807,
                 expected_revision=9223372036854775807, text='あ' * 10000)
    assert appender['load_draft'](write_draft(tmp_path, draft)) == draft


def test_large_draft_is_refused(appender, tmp_path):
    file = tmp_path / 'large.json'
    file.write_bytes(b' ' * 65537)
    with pytest.raises(ValueError, match='64KB'):
        appender['load_draft'](file)


@pytest.mark.parametrize('replayed', [False, True])
def test_submit_sends_same_operation_and_revision_on_retry(appender, draft, capsys, replayed):
    calls = []
    result = {'id': 5, 'url': '/entries/observation/5', 'revision': 3, 'replayed': replayed}
    class Client:
        origin = 'http://localhost:8000'
        def request(self, method, path, body):
            calls.append((method, path, body))
            return result
    client = Client()
    assert appender['submit'](draft, client) is result
    assert appender['submit'](draft, client) is result
    expected = ('POST', '/api/observations/5/append',
                {key: draft[key] for key in ('operation_key', 'expected_revision', 'text')})
    assert calls == [expected, expected]
    assert draft['expected_revision'] == 2
    output = capsys.readouterr().out
    assert 'http://localhost:8000/entries/observation/5' in output
    assert ('同じ追記を確認' in output) is replayed


def test_show_reads_snapshot_only(appender, monkeypatch, capsys):
    calls = []
    class Client:
        origin = 'http://localhost:8123'
        def __init__(self, port):
            assert port == 8123
        def request(self, method, path):
            calls.append((method, path))
            return {'id': 5, 'description': '元のメモ', 'revision': 2,
                    'url': '/entries/observation/5', 'media': [{'id': 7, 'keep': False}]}
    monkeypatch.setitem(appender['main'].__globals__, 'LocalClient', Client)
    monkeypatch.setattr(sys, 'argv', ['append.py', '--show', '5', '--port', '8123'])
    assert appender['main']() == 0
    assert calls == [('GET', '/api/observations/5')]
    output = json.loads(capsys.readouterr().out)
    assert output['description'] == '元のメモ'
    assert output['revision'] == 2
    assert output['base_url'] == 'http://localhost:8123'
    assert output['media'][0]['keep'] is False


def test_conflict_does_not_refresh_revision_or_retry(appender, draft, tmp_path, monkeypatch, capsys):
    calls = []
    class Client:
        origin = 'http://localhost:8000'
        def __init__(self, port):
            assert port == 8000
        def request(self, method, path, body):
            calls.append((method, path, body))
            raise ValueError('HTTP 409: メモが更新されています。現在の内容を確認してください。')
    monkeypatch.setitem(appender['main'].__globals__, 'LocalClient', Client)
    file = write_draft(tmp_path, draft)
    monkeypatch.setattr(sys, 'argv', ['append.py', str(file), '--submit'])
    assert appender['main']() == 1
    assert len(calls) == 1 and calls[0][0] == 'POST'
    assert calls[0][2]['expected_revision'] == 2
    assert json.loads(file.read_text()) == draft
    output = capsys.readouterr()
    assert '保存済み' not in output.out
    assert 'HTTP 409' in output.err and '版番号の競合時' in output.err


@pytest.mark.parametrize('argv', [
    ['--show', '0'], ['--show', '-1'], ['--show', 'true'], ['--show', '５'],
    ['--show', '9223372036854775808'], ['--show', '5', '--submit'],
    ['--show', '5', 'draft.json'], [], ['--submit'],
])
def test_invalid_commands_fail_without_network(appender, monkeypatch, argv):
    def forbidden_client(port):
        pytest.fail('不正なコマンドでは通信してはいけません。')
    monkeypatch.setitem(appender['main'].__globals__, 'LocalClient', forbidden_client)
    monkeypatch.setattr(sys, 'argv', ['append.py'] + argv)
    with pytest.raises(SystemExit) as error:
        appender['main']()
    assert error.value.code == 2


def test_submit_command_reports_success(appender, draft, tmp_path, monkeypatch, capsys):
    calls = []
    class Client:
        origin = 'http://localhost:8000'
        def __init__(self, port):
            assert port == 8000
        def request(self, method, path, body):
            calls.append((method, path, body))
            return {'id': 5, 'url': '/entries/observation/5', 'revision': 3, 'replayed': False}
    monkeypatch.setitem(appender['main'].__globals__, 'LocalClient', Client)
    file = write_draft(tmp_path, draft)
    monkeypatch.setattr(sys, 'argv', ['append.py', str(file), '--submit'])
    assert appender['main']() == 0
    assert len(calls) == 1 and calls[0][0] == 'POST'
    assert calls[0][2]['operation_key'] == draft['operation_key']
    output = capsys.readouterr()
    assert '追記保存済み: http://localhost:8000/entries/observation/5' in output.out
    assert not output.err


def test_show_failure_does_not_suggest_submission_retry(appender, monkeypatch, capsys):
    class Client:
        def __init__(self, port):
            raise OSError('アプリへ接続できません。')
    monkeypatch.setitem(appender['main'].__globals__, 'LocalClient', Client)
    monkeypatch.setattr(sys, 'argv', ['append.py', '--show', '5'])
    assert appender['main']() == 1
    error = capsys.readouterr().err
    assert '記録を取得できませんでした' in error
    assert '下書きで再実行' not in error
