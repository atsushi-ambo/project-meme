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


@pytest.mark.parametrize('failure', [None, 'test', 'python_audit', 'build', 'os_audit',
                                     'database_build', 'database_audit', 'backup', 'database_update',
                                     'migrate', 'health', 'interrupt', 'database_interrupt'])
def test_delivery_failure_boundaries(updater, tmp_path, failure):
    events = []
    dest = tmp_path / 'snapshot'
    def command(args, image=None, **kwargs):
        if args[0] == sys.executable:
            stage = {'test.py': 'test', 'security_check.py': 'python_audit',
                     'container_check.py': 'database_audit' if args[-1].startswith('project-meme-db:update-')
                     else 'os_audit'}[Path(args[1]).name]
        elif args[:2] == ['docker', 'build']: stage = 'database_build'
        elif 'build' in args: stage = 'build'
        elif 'migrate' in args: stage = 'migrate'
        elif 'up' in args: stage = 'database_update' if args[-1] == 'db' else 'health'
        elif 'start' in args: stage = 'restart'
        elif 'stop' in args: stage = 'stop'
        else: stage = 'tag'
        events.append(stage)
        if stage == failure: raise subprocess.CalledProcessError(1, args)
        if failure == 'interrupt' and stage == 'migrate': raise KeyboardInterrupt()
        if failure == 'database_interrupt' and stage == 'database_update': raise KeyboardInterrupt()
        if stage in ('build', 'database_update', 'migrate', 'health'):
            assert image.startswith('project-meme:update-')
        if stage == 'build':
            assert args == ['compose', 'build', 'web', '--no-cache']
        if stage == 'database_update':
            assert args == ['compose', 'up', '-d', '--no-deps', '--no-build', '--wait',
                            '--wait-timeout', '90', 'db']
            assert json.loads((dest / 'update.json').read_text())['stage'] == 'database_update_started'
            assert kwargs['database'].startswith('project-meme-db:update-')
        if stage == 'os_audit':
            assert args[2] == '--image'
            assert args[3].startswith('project-meme:update-')
            assert image is None
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
        assert events == ['test', 'python_audit', 'tag', 'build', 'os_audit', 'database_build',
                          'database_audit', 'backup', 'database_update', 'migrate', 'health', 'tag', 'tag']
        assert json.loads((dest / 'update.json').read_text())['stage'] == 'complete'
    if failure in ('test', 'python_audit', 'build', 'os_audit', 'database_build', 'database_audit'):
        assert not dest.exists()
        assert 'stop' not in events and 'restart' not in events
    if failure == 'backup':
        assert events[-1] == 'restart'
        assert 'migrate' not in events
    if failure in ('database_update', 'database_interrupt'):
        assert events[-1] == 'stop' and 'restart' not in events and 'migrate' not in events
        assert json.loads((dest / 'update.json').read_text())['stage'] == 'failed_after_database_update_started'
        assert json.loads((dest / 'manifest.json').read_text())['complete']
    if failure in ('migrate', 'health', 'interrupt'):
        assert events[-1] == 'stop' and 'restart' not in events
        assert json.loads((dest / 'update.json').read_text())['stage'] == 'failed_after_migration_started'


@pytest.mark.parametrize('failure', ['python_audit', 'os_audit', 'database_audit'])
def test_security_gate_preserves_existing_app_and_backup(updater, tmp_path, failure):
    """検査対象は候補そのもの。検査失敗で既存アプリや既存バックアップに触れない。"""
    dest = tmp_path / 'existing-backup'
    dest.mkdir()
    sentinel = dest / 'update.json'
    sentinel.write_text('{"previous_backup": true}')
    calls = []
    candidate = None
    candidate_db = None

    def command(args, image=None, **kwargs):
        nonlocal candidate, candidate_db
        calls.append((args, image))
        if args[:3] == ['compose', 'build', 'web']:
            candidate = image
        if args[:2] == ['docker', 'build']:
            candidate_db = args[args.index('-t') + 1]
        if args[0] == sys.executable:
            script = Path(args[1]).name
            if script == 'container_check.py':
                assert candidate is not None
                if args[-1] == candidate_db:
                    assert args == [sys.executable, str(ROOT / 'scripts/container_check.py'), '--image', candidate_db]
                    stage = 'database_audit'
                else:
                    assert args == [sys.executable, str(ROOT / 'scripts/container_check.py'),
                                    '--image', candidate]
                    stage = 'os_audit'
            else:
                stage = 'python_audit' if script == 'security_check.py' else 'test'
            if stage == failure:
                raise subprocess.CalledProcessError(2, args)
        return SimpleNamespace(stdout='')

    def snapshot(*args, **kwargs):
        pytest.fail('検査に失敗した更新でバックアップを開始した')

    with pytest.raises(subprocess.CalledProcessError):
        updater.deliver(command, snapshot, dest, 'revision', 'old-image')
    assert sentinel.read_text() == '{"previous_backup": true}'
    assert sorted(path.name for path in dest.iterdir()) == ['update.json']
    assert not any(args[0] == 'compose' and args[1] != 'build' for args, _ in calls)
    if failure == 'python_audit':
        assert [Path(args[1]).name for args, _ in calls] == ['test.py', 'security_check.py']
    else:
        assert candidate is not None


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
