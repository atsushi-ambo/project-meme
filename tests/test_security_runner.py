"""監査が未完了でも成功扱いせず、個人データを監査コンテナに渡さない。"""
from pathlib import Path
import runpy
import subprocess
from types import SimpleNamespace

import pytest


@pytest.fixture
def scanner():
    return runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/security_check.py'))


def locks(root, app=None, direct='example==1.0\n', tools=None):
    if app is None:
        app = 'example==1.0 --hash=sha256:' + 'a' * 64 + '\n'
    (root / 'requirements.lock').write_text(app)
    (root / 'requirements.txt').write_text(direct)
    (root / 'requirements.security.lock').write_text(
        tools or ('pip-audit==2.10.1 --hash=sha256:' + 'a' * 64 + '\n'))


@pytest.mark.parametrize('result,expected', [(0, 0), (1, 1), (7, 7), ('timeout', 124), ('interrupt', 130)])
def test_only_public_locks_are_snapshotted_and_failure_preserved(scanner, tmp_path, monkeypatch, result, expected):
    locks(tmp_path)
    (tmp_path / '.env').write_text('SECRET_KEY=private-and-must-not-be-read\n')
    (tmp_path / 'private-video.mov').write_bytes(b'private video')
    monkeypatch.setenv('COMPOSE_FILE', 'private-project.yaml')
    monkeypatch.setenv('SECRET_KEY', 'never-forwarded-to-container')
    calls = []
    snapshots = []

    def fake_run(command, **kwargs):
        calls.append(command)
        assert 'COMPOSE_FILE' not in kwargs['env']
        if command[:2] == ['docker', 'run']:
            assert kwargs['timeout'] == 600
            mounts = [command[index + 1] for index, value in enumerate(command) if value == '--mount']
            assert len(mounts) == 2
            for mount in mounts:
                source = Path(mount.split('source=')[1].split(',target=')[0])
                snapshots.append(source)
                assert source.parent != tmp_path
                assert source.name in ('app.lock', 'tools.lock')
                assert source.is_file() and (source.stat().st_mode & 0o777) == 0o644
                assert 'private' not in source.read_text()
                assert mount.endswith(',readonly')
            assert '--read-only' in command
            assert command[command.index('--user') + 1] == '65534:65534'
            assert command[command.index('--cap-drop') + 1] == 'ALL'
            assert command[command.index('--security-opt') + 1] == 'no-new-privileges:true'
            assert '--memory' in command and '--cpus' in command and '--pids-limit' in command
            assert '--env' not in command and '-e' not in command
            assert scanner['PYTHON_IMAGE'] in command
            assert '--disable-pip' in command[-1] and '--strict' in command[-1]
            assert '--fix' not in command[-1] and '--ignore-vuln' not in command[-1]
            if result == 'timeout':
                raise subprocess.TimeoutExpired(command, 600)
            if result == 'interrupt':
                raise KeyboardInterrupt()
            return SimpleNamespace(returncode=result)
        assert command[:3] == ['docker', 'rm', '-f']
        assert kwargs['timeout'] == 30
        return SimpleNamespace(returncode=1)  # --rmで既に消えている通常のケース。

    monkeypatch.setattr(scanner['subprocess'], 'run', fake_run)
    assert scanner['audit'](tmp_path) == expected
    assert len(calls) == 2
    project = calls[0][calls[0].index('--name') + 1]
    assert project.startswith('project-meme-audit-')
    assert calls[1] == ['docker', 'rm', '-f', project]
    assert all(not path.exists() for path in snapshots)
    assert (tmp_path / '.env').read_text().startswith('SECRET_KEY=')
    assert (tmp_path / 'private-video.mov').exists()


@pytest.mark.parametrize('bad', [
    'example==1.0\n',
    'example>=1.0\n',
    'example @ https://example.com/package.whl\n',
    '--extra-index-url https://example.com/simple\n',
    '-r /private/path/secret\n',
    'example==1.0 --hash=sha256:' + 'a' * 64 + ' --index-url https://example.com/simple\n',
    'example==1.0\nExample==1.0\n',
    'example[extra]==1.0\n',
    '',
])
def test_unsafe_or_incomplete_input_rejected_before_docker(scanner, tmp_path, monkeypatch, bad):
    locks(tmp_path, app=bad)
    calls = []
    monkeypatch.setattr(scanner['subprocess'], 'run', lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError):
        scanner['audit'](tmp_path)
    assert calls == []


@pytest.mark.parametrize('tools', [
    'pip-audit==2.10.1\n',
    'pip-audit==2.10.1 --hash=sha256:abc\n',
    'pip-audit==2.10.1 --hash=md5:' + 'a' * 32 + '\n',
    'other==1.0 --hash=sha256:' + 'a' * 64 + '\n',
])
def test_tool_hashes_and_scanner_required(scanner, tmp_path, tools):
    locks(tmp_path, tools=tools)
    with pytest.raises(ValueError):
        scanner['read_inputs'](tmp_path)


def test_direct_versions_must_match_complete_lock(scanner, tmp_path):
    locks(tmp_path, direct='example==2.0\n')
    with pytest.raises(ValueError, match='一致'):
        scanner['read_inputs'](tmp_path)


def test_extras_and_continued_hashes_are_validated_without_pip_options(scanner, tmp_path):
    locks(tmp_path, direct='example[binary]==1.0\n',
          tools='# a public comment\npip-audit==2.10.1 \\\n    --hash=sha256:' + 'a' * 64 + ' \\\n    --hash=sha256:' + 'b' * 64 + '\n')
    app, tools = scanner['read_inputs'](tmp_path)
    assert app == 'example==1.0 --hash=sha256:' + 'a' * 64 + '\n'
    assert tools.startswith('pip-audit==2.10.1 --hash=sha256:')
    assert '#' not in tools and '\\' not in tools


def test_symbolic_lock_and_oversized_files_refused(scanner, tmp_path):
    locks(tmp_path)
    lock = tmp_path / 'requirements.lock'
    lock.unlink()
    lock.symlink_to(tmp_path / 'requirements.txt')
    with pytest.raises(ValueError):
        scanner['read_inputs'](tmp_path)
    lock.unlink()
    lock.write_text('#' + 'x' * 100_000)
    with pytest.raises(ValueError):
        scanner['read_inputs'](tmp_path)
