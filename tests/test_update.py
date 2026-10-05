"""更新の成功と、各境界で失敗した際のデータ保護を確認する。"""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def updater(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    spec = importlib.util.spec_from_file_location('meme_update', ROOT / 'scripts/update.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('failure', [None, 'test', 'build', 'backup', 'migrate', 'health', 'interrupt'])
def test_delivery_failure_boundaries(updater, tmp_path, failure):
    events = []
    dest = tmp_path / 'snapshot'
    def command(args, image=None, **kwargs):
        if args[0] == sys.executable: stage = 'test'
        elif 'build' in args: stage = 'build'
        elif 'migrate' in args: stage = 'migrate'
        elif 'up' in args: stage = 'health'
        elif 'start' in args: stage = 'restart'
        elif 'stop' in args: stage = 'stop'
        else: stage = 'tag'
        events.append(stage)
        if stage == failure: raise subprocess.CalledProcessError(1, args)
        if failure == 'interrupt' and stage == 'migrate': raise KeyboardInterrupt()
        if stage in ('build', 'migrate', 'health'): assert image.startswith('project-meme:update-')
        return SimpleNamespace(stdout='container\n')
    def snapshot(path, restart, runner):
        assert restart is False
        events.append('backup')
        path.mkdir()
        if failure == 'backup': raise OSError('disk full')
        (path / 'manifest.json').write_text('{"complete": true}')
    if failure:
        with pytest.raises((subprocess.CalledProcessError, OSError, KeyboardInterrupt)):
            updater.deliver(command, snapshot, dest, 'revision', 'old-image')
    else:
        updater.deliver(command, snapshot, dest, 'revision', 'old-image')
        assert events == ['test', 'tag', 'build', 'backup', 'migrate', 'health', 'tag']
        assert json.loads((dest / 'update.json').read_text())['stage'] == 'complete'
    if failure in ('test', 'build'):
        assert not dest.exists()
        assert 'stop' not in events and 'restart' not in events
    if failure == 'backup':
        assert events[-1] == 'restart'
        assert 'migrate' not in events
    if failure in ('migrate', 'health', 'interrupt'):
        assert events[-1] == 'stop' and 'restart' not in events
        assert json.loads((dest / 'update.json').read_text())['stage'] == 'failed_after_migration_started'


@pytest.mark.parametrize('inside', ['media', 'repo'])
def test_backup_rejects_unsafe_destination(updater, tmp_path, monkeypatch, inside):
    backup = updater.snapshots
    media = tmp_path / 'media'
    repo = tmp_path / 'repo'
    monkeypatch.setattr(backup, 'ROOT', repo)
    monkeypatch.setattr(backup, 'env_values', lambda: {'MEDIA_DIR': str(media)})
    with pytest.raises(ValueError):
        backup.backup((media if inside == 'media' else repo) / 'snapshot')


@pytest.mark.parametrize('restart', [True, False])
def test_backup_is_consistent_and_controls_restart(updater, tmp_path, monkeypatch, restart):
    backup = updater.snapshots
    media = tmp_path / 'media'
    media.mkdir()
    (media / 'test.txt').write_text('private test fixture')
    monkeypatch.setattr(backup, 'ROOT', tmp_path / 'repo')
    monkeypatch.setattr(backup, 'env_values', lambda: {'MEDIA_DIR': str(media)})
    calls = []
    def runner(args, **kwargs):
        calls.append(args[0])
        if 'stdout' in kwargs: kwargs['stdout'].write(b'test dump')
        return SimpleNamespace(stdout='running')
    dest = tmp_path / 'snapshot'
    backup.backup(dest, restart=restart, runner=runner)
    assert calls == ['ps', 'stop', 'run', 'exec'] + (['start'] if restart else [])
    assert json.loads((dest / 'manifest.json').read_text())['complete']
    assert (dest / 'database.dump').read_bytes() == b'test dump'
    assert dest.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize('state', ['dirty', 'stopped', 'ready'])
def test_update_preflight_and_environment(updater, tmp_path, monkeypatch, state):
    monkeypatch.setattr(updater, 'STATE', tmp_path / 'state')
    monkeypatch.setattr(updater.snapshots, 'env_values', lambda: {'MEDIA_DIR': '/private-media'})
    monkeypatch.setenv('MEDIA_DIR', '/wrong-media')
    monkeypatch.setenv('COMPOSE_FILE', 'wrong.yaml')
    monkeypatch.setenv('APP_IMAGE', 'wrong-image')
    deployed = []
    def command(args, **kwargs):
        assert 'MEDIA_DIR' not in kwargs['env']
        assert 'COMPOSE_FILE' not in kwargs['env']
        if args[:2] == ['docker', 'compose']:
            assert args[args.index('-p') + 1] == 'project-meme'
            assert args[args.index('-f') + 1] == str(ROOT / 'compose.yaml')
            assert kwargs['env']['APP_IMAGE'] == 'project-meme:local'
        if 'status' in args: output = ' M file' if state == 'dirty' else ''
        elif 'rev-parse' in args: output = 'revision'
        elif 'ps' in args: output = '' if state == 'stopped' else 'container'
        else: output = 'old-image'
        return SimpleNamespace(stdout=output)
    monkeypatch.setattr(updater.subprocess, 'run', command)
    monkeypatch.setattr(updater, 'deliver', lambda *args: deployed.append(args))
    assert updater.main() == (0 if state == 'ready' else 1)
    assert bool(deployed) == (state == 'ready')


def test_update_rejects_concurrent_run(updater, tmp_path, monkeypatch):
    monkeypatch.setattr(updater, 'STATE', tmp_path)
    def busy(*args): raise BlockingIOError()
    monkeypatch.setattr(updater.fcntl, 'flock', busy)
    assert updater.main() == 1
