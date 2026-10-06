import io
import json
from pathlib import Path
import runpy
from types import SimpleNamespace
import uuid
import pytest


@pytest.fixture
def poster():
    return runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/post.py'))


def test_draft_rejects_missing_media_before_posting(poster, tmp_path):
    draft = tmp_path / 'draft.json'
    draft.write_text(json.dumps({'kind': 'observation', 'entry': {'submission_key': str(uuid.uuid4())},
                                'media': [{'path': '/missing.mov'}]}))
    with pytest.raises(ValueError): poster['load_draft'](draft)


@pytest.mark.parametrize('status', ['failed', 'deleted'])
def test_attachment_retry_or_deleted_rejection(poster, tmp_path, status):
    file = tmp_path / 'video.mov'
    file.write_bytes(b'fake bytes; format validation is tested server-side')
    keys = []
    class Client:
        origin = 'http://localhost:8000'
        def request(self, method, path, body, size=None):
            if path.startswith('/api/entries'):
                return {'id': 1, 'url': '/entries/observation/1'}
            if method == 'PUT':
                assert hasattr(body, 'read') and size == file.stat().st_size
                return {'status': 'ready'}
            keys.append(body['upload_key'])
            return {'id': 2, 'status': status if len(keys) == 1 else 'pending'}
    draft = {'kind': 'observation', 'entry': {'submission_key': str(uuid.uuid4())}, 'media': [{'path': str(file)}]}
    if status == 'deleted':
        with pytest.raises(ValueError, match='削除'): poster['submit'](draft, Client())
        assert len(keys) == 1
    else:
        poster['submit'](draft, Client())
        assert len(keys) == 2 and keys[0] != keys[1]


def test_local_http_streaming_and_security_headers(poster, monkeypatch):
    calls = []
    class Connection:
        def __init__(self, host, port, timeout):
            assert host == '127.0.0.1' and port == 8000
        def request(self, method, path, body, headers):
            calls.append((method, path, body, headers))
        def getresponse(self):
            get = calls[-1][0] == 'GET'
            return SimpleNamespace(status=200,
                getheader=lambda *args: 'meme_browser=signed; HttpOnly',
                read=lambda limit: b'<meta name="csrf-token" content="token">' if get else b'{"status":"ready"}')
        def close(self): pass
    monkeypatch.setattr(poster['http'].client, 'HTTPConnection', Connection)
    client = poster['LocalClient'](8000)
    file = io.BytesIO(b'video')
    client.request('PUT', '/api/media/1', file, size=5)
    _, _, body, headers = calls[-1]
    assert body is file
    assert headers['Content-Length'] == '5'
    assert headers['X-CSRF-Token'] == 'token'
    assert headers['Cookie'] == 'meme_browser=signed'
    assert headers['Origin'] == 'http://localhost:8000'


@pytest.mark.parametrize('guesses', [
    'preset:attention', None, [None], [''], ['preset:invalid'],
    ['item:-1'], ['item:0'], ['item:9223372036854775808'],
    ['item:1/../../other'], ['item:１２'], ['preset:attention'] * 31,
])
def test_draft_rejects_invalid_guesses(poster, tmp_path, guesses):
    draft = tmp_path / 'draft.json'
    draft.write_text(json.dumps({'kind': 'observation',
                                'entry': {'submission_key': str(uuid.uuid4())},
                                'guesses': guesses}))
    with pytest.raises(ValueError, match='予想'):
        poster['load_draft'](draft)


def test_draft_accepts_presets_and_dictionary_ids(poster, tmp_path):
    guesses = ['preset:' + name for name in
               ['attention', 'play', 'food', 'door', 'follow', 'concern', 'unknown']]
    guesses += ['item:1', 'item:9223372036854775807']
    draft = tmp_path / 'draft.json'
    draft.write_text(json.dumps({'kind': 'observation',
                                'entry': {'submission_key': str(uuid.uuid4())},
                                'guesses': guesses}))
    assert poster['load_draft'](draft)['guesses'] == guesses


def test_health_guesses_fail_before_client_is_created(poster, tmp_path, monkeypatch, capsys):
    draft = tmp_path / 'draft.json'
    draft.write_text(json.dumps({'kind': 'health',
                                'entry': {'submission_key': str(uuid.uuid4())},
                                'guesses': ['preset:attention']}))
    monkeypatch.setattr(poster['sys'], 'argv', ['post.py', str(draft), '--submit'])
    def forbidden_client(port):
        pytest.fail('不正な下書きの保存前に通信してはいけません。')
    monkeypatch.setitem(poster['main'].__globals__, 'LocalClient', forbidden_client)
    assert poster['main']() == 1
    assert '予想は観察の記録' in capsys.readouterr().err


@pytest.mark.parametrize('guesses', [None, []])
def test_no_guesses_keeps_existing_submission_path(poster, guesses):
    calls = []
    class Client:
        origin = 'http://localhost:8000'
        def request(self, method, path, body):
            calls.append(path)
            return {'id': 1, 'url': '/entries/observation/1'}
    draft = {'kind': 'observation', 'entry': {'submission_key': str(uuid.uuid4())}}
    if guesses is not None:
        draft['guesses'] = guesses
    poster['submit'](draft, Client())
    assert calls == ['/api/entries/observation']


def test_guesses_are_sent_after_entry_without_assessment(poster):
    calls = []
    class Client:
        origin = 'http://localhost:8000'
        def request(self, method, path, body):
            calls.append((path, body))
            return {'id': 9, 'url': '/entries/observation/9'}
    draft = {'kind': 'observation', 'entry': {'submission_key': str(uuid.uuid4())},
             'guesses': ['preset:attention', 'item:2']}
    client = Client()
    poster['submit'](draft, client)
    poster['submit'](draft, client)
    assert calls[0] == calls[2] == ('/api/entries/observation', draft['entry'])
    assert calls[1] == calls[3] == ('/api/observations/9/guesses', {'guesses': draft['guesses']})


def test_guess_failure_leaves_entry_url_and_does_not_upload_media(poster, tmp_path, capsys):
    photo = tmp_path / 'photo.png'
    photo.write_bytes(b'fake media validated server-side')
    calls = []
    class Client:
        origin = 'http://localhost:8000'
        def request(self, method, path, body):
            calls.append(path)
            if path == '/api/entries/observation':
                return {'id': 9, 'url': '/entries/observation/9'}
            raise ValueError('この予想は選択できません。')
    draft = {'kind': 'observation', 'entry': {'submission_key': str(uuid.uuid4())},
             'guesses': ['item:2'], 'media': [{'path': str(photo)}]}
    with pytest.raises(ValueError, match='予想'):
        poster['submit'](draft, Client())
    assert calls == ['/api/entries/observation', '/api/observations/9/guesses']
    assert 'http://localhost:8000/entries/observation/9' in capsys.readouterr().out


@pytest.mark.parametrize('keep', ['true', 'false', 1, 0, None, [], {}])
def test_draft_rejects_keep_without_strict_boolean(poster, tmp_path, keep):
    photo = tmp_path / 'photo.png'
    photo.write_bytes(b'fake media validated server-side')
    draft = tmp_path / 'draft.json'
    draft.write_text(json.dumps({'kind': 'observation',
                                'entry': {'submission_key': str(uuid.uuid4())},
                                'media': [{'path': str(photo), 'keep': keep}]}))
    with pytest.raises(ValueError, match='keep'):
        poster['load_draft'](draft)


@pytest.mark.parametrize('kind, keep, extension', [
    ('observation', True, '.MOV'), ('observation', False, '.png'),
    ('health', True, '.m4a'), ('health', False, '.jpg'),
])
def test_draft_accepts_explicit_keep_for_all_record_types(poster, tmp_path, kind, keep, extension):
    file = tmp_path / ('media' + extension)
    file.write_bytes(b'fake media validated server-side')
    draft = tmp_path / 'draft.json'
    draft.write_text(json.dumps({'kind': kind,
                                'entry': {'submission_key': str(uuid.uuid4())},
                                'media': [{'path': str(file), 'keep': keep}]}))
    assert poster['load_draft'](draft)['media'][0]['keep'] is keep


def test_keep_replay_sets_state_without_reupload_or_new_key(poster, tmp_path, capsys):
    video = tmp_path / 'video.mov'
    video.write_bytes(b'fake media validated server-side')
    calls = []
    upload_keys = []
    class Client:
        origin = 'http://localhost:8000'
        ready = False
        keep = False
        def request(self, method, path, body, size=None):
            calls.append((method, path))
            if path == '/api/entries/observation':
                return {'id': 9, 'url': '/entries/observation/9'}
            if path == '/api/media/reserve':
                upload_keys.append(body['upload_key'])
                return {'id': 3, 'status': 'ready' if self.ready else 'pending'}
            if method == 'PUT':
                assert size == video.stat().st_size and body.read() == video.read_bytes()
                self.ready = True
                return {'status': 'ready'}
            assert path == '/api/media/3/keep' and method == 'POST'
            assert set(body) == {'keep'} and type(body['keep']) is bool
            self.keep = body['keep']
            return {'id': 3, 'keep': self.keep}
    item = {'path': str(video), 'keep': True}
    draft = {'kind': 'observation', 'entry': {'submission_key': str(uuid.uuid4())},
             'media': [item]}
    client = Client()
    poster['submit'](draft, client)
    assert calls.index(('PUT', '/api/media/3')) < calls.index(('POST', '/api/media/3/keep'))
    assert client.keep is True
    poster['submit'](draft, client)
    assert client.keep is True
    item['keep'] = False
    poster['submit'](draft, client)
    assert client.keep is False
    assert len(upload_keys) == 3 and len(set(upload_keys)) == 1
    assert calls.count(('PUT', '/api/media/3')) == 1
    assert calls.count(('POST', '/api/media/3/keep')) == 3
    output = capsys.readouterr().out
    assert output.count('期限を付けず保管') == 2
    assert output.count('通常の保存期限に戻す') == 1


def test_missing_keep_does_not_reset_existing_retention(poster, tmp_path):
    video = tmp_path / 'video.mov'
    video.write_bytes(b'fake media validated server-side')
    calls = []
    class Client:
        origin = 'http://localhost:8000'
        def request(self, method, path, body):
            calls.append(path)
            if path == '/api/entries/observation':
                return {'id': 9, 'url': '/entries/observation/9'}
            assert path == '/api/media/reserve'
            return {'id': 3, 'status': 'ready'}
    draft = {'kind': 'observation', 'entry': {'submission_key': str(uuid.uuid4())},
             'media': [{'path': str(video)}]}
    poster['submit'](draft, Client())
    assert calls == ['/api/entries/observation', '/api/media/reserve']


def test_keep_failure_preserves_uploaded_file_and_retry_uses_same_key(poster, tmp_path, capsys):
    photo = tmp_path / 'photo.png'
    photo.write_bytes(b'fake media validated server-side')
    calls = []
    keys = []
    class Client:
        origin = 'http://localhost:8000'
        ready = False
        fail = True
        def request(self, method, path, body, size=None):
            calls.append((method, path))
            if path == '/api/entries/observation':
                return {'id': 9, 'url': '/entries/observation/9'}
            if path == '/api/media/reserve':
                keys.append(body['upload_key'])
                return {'id': 3, 'status': 'ready' if self.ready else 'pending'}
            if method == 'PUT':
                self.ready = True
                return {'status': 'ready'}
            assert path == '/api/media/3/keep'
            if self.fail:
                raise ValueError('HTTP 409: 処理中です。')
            return {'id': 3, 'keep': body['keep']}
    draft = {'kind': 'observation', 'entry': {'submission_key': str(uuid.uuid4())},
             'media': [{'path': str(photo), 'keep': True}]}
    client = Client()
    with pytest.raises(ValueError, match='添付は保存済み.*保管設定'):
        poster['submit'](draft, client)
    output = capsys.readouterr().out
    assert '本文保存済み: http://localhost:8000/entries/observation/9' in output
    assert '添付保存済み: photo.png' in output
    assert '保管設定済み' not in output
    client.fail = False
    poster['submit'](draft, client)
    assert keys[0] == keys[1]
    assert calls.count(('PUT', '/api/media/3')) == 1
    assert '保管設定済み' in capsys.readouterr().out
