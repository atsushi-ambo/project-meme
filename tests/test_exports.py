"""出力ZIPの保存先・権限と、失敗・異常停止後の後片付けを確認する。"""
import asyncio
from contextlib import contextmanager
import fcntl
import io
import os
from pathlib import Path
from types import SimpleNamespace
import zipfile

import anyio
import pytest

from app import exports
from app.config import settings
from app.db import transaction
from app.media import paths, run_jobs
from app.models import Media, now
from test_app import client, observation, png, post, upload


def test_export_uses_private_disk_backed_media_directory(client, monkeypatch):
    oid = observation(client)
    _, uploaded = upload(client, oid, png())
    assert uploaded.status_code == 200
    directory = settings.media_root / '.exports'
    checked = []
    disk_usage = exports.shutil.disk_usage

    def limited_tmp(path):
        checked.append(Path(path))
        # 本番の/tmpは64MB。空き容量の判定もメディア側に向ける必要がある。
        return disk_usage(path) if Path(path) == directory else SimpleNamespace(free=64 * 1024**2)

    original = exports.ExportResponse.__call__

    async def inspect_response(self, scope, receive, send):
        path = Path(self.path)
        assert path.parent == directory
        assert directory.stat().st_mode & 0o777 == 0o700
        assert path.stat().st_mode & 0o777 == 0o600
        await original(self, scope, receive, send)

    monkeypatch.setattr(exports.shutil, 'disk_usage', limited_tmp)
    monkeypatch.setattr(exports.ExportResponse, '__call__', inspect_response)
    response = post(client, '/export', {'confirm': '出力'})
    assert response.status_code == 200, response.text
    assert checked == [directory]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert 'records.json' in archive.namelist()
        assert any(name.startswith('media/') for name in archive.namelist())
    assert list(directory.iterdir()) == []


def test_export_generation_failure_removes_partial_archive(client):
    oid = observation(client)
    mid, uploaded = upload(client, oid, png())
    assert uploaded.status_code == 200
    with transaction() as db:
        path, _ = paths(db.get(Media, mid).storage_key)
    path.unlink()
    assert post(client, '/export', {'confirm': '出力'}).status_code == 409
    assert list((settings.media_root / '.exports').iterdir()) == []


def test_export_transaction_failure_removes_completed_archive(client, monkeypatch):
    @contextmanager
    def failing_transaction(**kwargs):
        with transaction(**kwargs) as db:
            yield db
        raise OSError('transaction completion failed')

    monkeypatch.setattr(exports, 'transaction', failing_transaction)
    with pytest.raises(OSError, match='transaction completion failed'):
        exports.export_zip()
    assert list((settings.media_root / '.exports').iterdir()) == []


@pytest.mark.parametrize('failure', [OSError, asyncio.CancelledError])
def test_export_response_failure_removes_archive(tmp_path, failure):
    path = tmp_path / 'export-test.zip'
    path.write_bytes(b'zip fixture')

    async def receive():
        return {'type': 'http.disconnect'}

    async def send(message):
        if message['type'] == 'http.response.body':
            raise failure()

    async def respond():
        await exports.ExportResponse(path)({'type': 'http', 'method': 'GET', 'headers': []}, receive, send)

    with pytest.raises(failure):
        anyio.run(respond)
    assert not path.exists()


def test_export_cleanup_preserves_recent_and_active_archives(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'media_root', tmp_path)
    directory = tmp_path / '.exports'
    directory.mkdir()
    old, active, recent, unrelated = [directory / name for name in
                                     ('export-old.zip', 'export-active.zip', 'export-recent.zip', 'keep.txt')]
    for path in (old, active, recent, unrelated):
        path.write_bytes(b'fixture')
    at = now()
    for path in (old, active, unrelated):
        os.utime(path, (at.timestamp() - 86401,) * 2)
    with active.open('rb') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        exports.cleanup_exports(at)
        assert not old.exists()
        assert active.exists() and recent.exists() and unrelated.exists()
    exports.cleanup_exports(at)
    assert not active.exists()
    assert recent.exists() and unrelated.exists()


def test_maintenance_removes_stale_exports(client):
    directory = settings.media_root / '.exports'
    directory.mkdir(exist_ok=True)
    path = directory / 'export-leftover.zip'
    path.write_bytes(b'fixture')
    at = now()
    os.utime(path, (at.timestamp() - 86401,) * 2)
    run_jobs(at)
    assert not path.exists()
