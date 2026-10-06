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
