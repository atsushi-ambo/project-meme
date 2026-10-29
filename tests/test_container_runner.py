"""OS監査の隔離・不完全な結果の拒否・失敗時の整理を確認する。"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import runpy
import subprocess
from types import SimpleNamespace

import pytest


@pytest.fixture
def scanner():
    return runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/container_check.py'))


def report(findings=None):
    return {'SchemaVersion': 2, 'Metadata': {'OS': {'Family': 'debian', 'Name': '13'}},
            'Results': [{'Class': 'os-pkgs', 'Type': 'debian', 'Packages': [{'Name': 'example', 'Version': '1'}],
                         'Vulnerabilities': findings or []}]}


def finding(severity, fixed=''):
    return {'PkgName': 'example', 'InstalledVersion': '1.0', 'VulnerabilityID': 'CVE-2026-1234',
            'Severity': severity, 'FixedVersion': fixed}


def cache_from(command):
    mounts = [command[index + 1] for index, value in enumerate(command) if value == '--mount']
    cache_mount = next(value for value in mounts if ',dst=/cache' in value)
    return Path(cache_mount.split('src=')[1].split(',dst=')[0])


def fake_docker(monkeypatch, scanner, *, findings=None, failure=None, image_remove_failure=False, database_reference=None):
    calls = []
    temporary_paths = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:2] == ['docker', 'build']:
            if failure == 'build':
                raise subprocess.CalledProcessError(1, command)
        elif command[:3] == ['docker', 'image', 'save']:
            archive = Path(command[command.index('--output') + 1])
            temporary_paths.append(archive)
            archive.write_bytes(b'public-image-archive')
            if failure == 'save':
                raise subprocess.CalledProcessError(1, command)
        elif command[:2] == ['docker', 'pull']:
            assert command[-1] in {scanner['TRIVY_IMAGE'], database_reference}
            if (failure == 'pull' and command[-1] == scanner['TRIVY_IMAGE']) or (failure == 'database-pull' and command[-1] == database_reference):
                raise subprocess.CalledProcessError(1, command)
        elif command[:2] == ['docker', 'run']:
            cache = cache_from(command)
            temporary_paths.append(cache)
            mounts = [command[index + 1] for index, value in enumerate(command) if value == '--mount']
            assert '--read-only' in command and '--memory' in command and '--pids-limit' in command
            assert command[command.index('--cap-drop') + 1] == 'ALL'
            assert command[command.index('--security-opt') + 1] == 'no-new-privileges'
            assert command[command.index('--user') + 1] == f"{scanner['os'].getuid()}:{scanner['os'].getgid()}"
            assert all('docker.sock' not in value and 'project-meme/media' not in value for value in mounts)
            assert '-e' not in command and '--env' not in command
            if '--download-db-only' in command:
                assert len(mounts) == 1 and '--network' not in command
                assert command[command.index('--db-repository') + 1] == scanner['DB_REPOSITORY']
                if failure == 'db':
                    raise subprocess.CalledProcessError(1, command)
                (cache / 'db').mkdir()
                age = timedelta(days=4) if failure == 'stale-db' else timedelta()
                (cache / 'db' / 'metadata.json').write_text(json.dumps({
                    'Version': 2, 'UpdatedAt': (datetime.now(timezone.utc) - age).isoformat()}))
            else:
                assert len(mounts) == 2
                assert command[command.index('--network') + 1] == 'none'
                assert '--offline-scan' in command and '--skip-db-update' in command
                assert '--skip-java-db-update' in command and '--ignore-unfixed' not in command
                assert command[command.index('--scanners') + 1] == 'vuln'
                assert command[command.index('--pkg-types') + 1] == 'os'
                input_mount = next(value for value in mounts if ',dst=/input' in value)
                assert input_mount.endswith(',readonly')
                source = Path(input_mount.split('src=')[1].split(',dst=')[0])
                assert list(source.iterdir()) == [source / 'image.tar']
                if failure == 'timeout':
                    raise subprocess.TimeoutExpired(command, 240)
                if failure == 'interrupt':
                    raise KeyboardInterrupt()
                if failure == 'scan':
                    raise subprocess.CalledProcessError(1, command)
                payload = b'{' if failure == 'json' else json.dumps(report(findings)).encode()
                kwargs['stdout'].write(payload)
        elif command[:3] == ['docker', 'rm', '-f']:
            return SimpleNamespace(returncode=1)  # successful --rm already removed it
        elif command[:3] == ['docker', 'ps', '-aq']:
            return SimpleNamespace(returncode=0, stdout='leftover' if failure == 'cleanup' else '')
        elif command[:3] == ['docker', 'image', 'rm']:
            return SimpleNamespace(returncode=1 if image_remove_failure else 0)
        else:
            raise AssertionError(command)
        return SimpleNamespace(returncode=0, stdout='')

    monkeypatch.setattr(scanner['subprocess'], 'run', fake_run)
    return calls, temporary_paths


@pytest.mark.parametrize('findings,expected', [
    ([], 0), ([finding('HIGH')], 0), ([finding('CRITICAL')], 0),
    ([finding('LOW', '2')], 0), ([finding('MEDIUM', '2')], 0),
    ([finding('HIGH', '2')], 1), ([finding('CRITICAL', '2')], 1),
])
def test_only_fixable_high_critical_block_and_unfixed_counts_disclosed(scanner, findings, expected, capsys):
    assert scanner['summarize'](report(findings)) == expected
    output = capsys.readouterr().out
    assert '修正版のないHIGH/CRITICAL:' in output
    assert '攻撃可能性の確定ではありません' in output
    if expected:
        assert 'CVE-2026-1234' in output and 'FixedVersion' in output


@pytest.mark.parametrize('value', [
    {}, [], {'SchemaVersion': 1, 'Results': []},
    {'SchemaVersion': 2, 'Metadata': {}, 'Results': []},
    {'SchemaVersion': 2, 'Metadata': {'OS': {'Family': 'debian', 'Name': '13'}}, 'Results': []},
])
def test_incomplete_scan_never_passes(scanner, value):
    with pytest.raises(ValueError):
        scanner['summarize'](value)


@pytest.mark.parametrize('change', ['eosl', 'no-packages', 'bad-severity', 'missing-version', 'bad-fixed-version'])
def test_corrupted_os_and_vulnerability_metadata_rejected(scanner, change):
    value = report([finding('HIGH', '2')])
    if change == 'eosl': value['Metadata']['OS']['EOSL'] = True
    elif change == 'no-packages': value['Results'][0]['Packages'] = []
    elif change == 'bad-severity': value['Results'][0]['Vulnerabilities'][0]['Severity'] = 'new-unknown'
    elif change == 'missing-version': del value['Results'][0]['Vulnerabilities'][0]['InstalledVersion']
    else: value['Results'][0]['Vulnerabilities'][0]['FixedVersion'] = []
    with pytest.raises(ValueError):
        scanner['summarize'](value)


@pytest.mark.parametrize('image', ['--help', '-o/tmp/secret', 'image;echo bad', 'image name', 'image\n', '', 'a' * 257])
def test_invalid_image_reference_refused_before_docker(scanner, image, monkeypatch):
    calls, paths = fake_docker(monkeypatch, scanner)
    assert scanner['check'](image) == 2
    assert calls == [] and paths == []


@pytest.mark.parametrize('image', ['project-meme:local', 'registry.example/app:tag', 'app@sha256:' + 'a' * 64])
def test_existing_image_not_executed_or_removed(scanner, image, monkeypatch):
    calls, paths = fake_docker(monkeypatch, scanner)
    assert scanner['check'](image) == 0
    assert not any(command[:2] == ['docker', 'build'] for command in calls)
    assert not any(command[:3] == ['docker', 'image', 'rm'] for command in calls)
    assert all(command[-1] != image for command in calls if command[:2] == ['docker', 'run'])
    assert paths and all(not path.exists() for path in paths)


@pytest.mark.parametrize('failure', ['build', 'save', 'pull', 'db', 'stale-db', 'scan', 'timeout', 'json', 'cleanup', 'interrupt'])
def test_failure_cleanup_only_touches_owned_resources(scanner, monkeypatch, failure):
    calls, paths = fake_docker(monkeypatch, scanner, failure=failure)
    assert scanner['check']() == (130 if failure == 'interrupt' else 2)
    build = calls[0]
    assert '--pull' in build and '--no-cache' in build
    image = build[build.index('-t') + 1]
    assert image.startswith('project-meme-security-')
    assert calls[-1] == ['docker', 'image', 'rm', image]
    names = [command[command.index('--name') + 1] for command in calls if command[:2] == ['docker', 'run']]
    for name in names:
        assert ['docker', 'rm', '-f', name] in calls
        assert name.startswith('project-meme-security-')
    assert all(not path.exists() for path in paths)


def test_build_success_still_fails_if_owned_image_cannot_be_removed(scanner, monkeypatch):
    fake_docker(monkeypatch, scanner, image_remove_failure=True)
    assert scanner['check']() == 2


@pytest.mark.parametrize('hours,valid', [(0, True), (24, True), (49, False), (-2, False)])
def test_vulnerability_database_freshness(scanner, tmp_path, hours, valid):
    (tmp_path / 'db').mkdir()
    (tmp_path / 'db' / 'metadata.json').write_text(json.dumps({
        'Version': 2, 'UpdatedAt': (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()}))
    if valid:
        assert scanner['database_is_fresh'](tmp_path).tzinfo is not None
    else:
        with pytest.raises(ValueError):
            scanner['database_is_fresh'](tmp_path)


def test_rfc3339_nanoseconds_are_compatible_with_macbook_python(scanner, tmp_path):
    (tmp_path / 'db').mkdir()
    now = datetime.now(timezone.utc).isoformat(timespec='microseconds')
    nanoseconds = now.replace('+00:00', '123Z')
    (tmp_path / 'db' / 'metadata.json').write_text(json.dumps({'Version': 2, 'UpdatedAt': nanoseconds}))
    updated = scanner['database_is_fresh'](tmp_path)
    assert updated == datetime.fromisoformat(now)


DATABASE_IMAGE = 'postgres:18-bookworm@sha256:' + 'a' * 64


def compose_text(image=DATABASE_IMAGE):
    return 'name: project-meme\nservices:\n  db:\n    image: ' + image + '\n    environment:\n      POSTGRES_PASSWORD: ${DB_PASSWORD}\n  web:\n    image: local-app\nvolumes:\n  postgres:\n'


@pytest.mark.parametrize('image', [DATABASE_IMAGE, DATABASE_IMAGE.replace('bookworm', 'trixie')])
@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_pinned_database_image_is_read_without_environment_expansion(scanner, tmp_path, image, newline):
    compose = tmp_path / 'compose.yaml'
    compose.write_bytes(compose_text(image).replace('\n', newline).encode())
    assert scanner['database_image'](compose) == image


@pytest.mark.parametrize('text', [
    '', 'services:\n  web:\n    image: postgres:18-bookworm\n',
    compose_text().replace('services:', 'services: &services'),
    compose_text() + 'services:\n  db:\n    image: ' + DATABASE_IMAGE + '\n',
    compose_text().replace('  db:', '  db: *database'),
    compose_text().replace('  db:', '  db:\n    image: ' + DATABASE_IMAGE + '\n  db:'),
    compose_text().replace('    image:', '    image: ' + DATABASE_IMAGE + '\n    image:', 1),
    compose_text('postgres:18-bookworm'),
    compose_text('postgres:17-bookworm@sha256:' + 'a' * 64),
    compose_text('attacker/postgres:18-bookworm@sha256:' + 'a' * 64),
    compose_text('${DATABASE_IMAGE}'),
    compose_text('"' + DATABASE_IMAGE + '"'),
    compose_text(DATABASE_IMAGE + ' # extra'),
])
def test_ambiguous_or_unpinned_compose_database_refused(scanner, tmp_path, text):
    compose = tmp_path / 'compose.yaml'
    compose.write_text(text)
    with pytest.raises(ValueError):
        scanner['database_image'](compose)


@pytest.mark.parametrize('kind', ['large', 'directory', 'symlink', 'fifo', 'invalid-utf8'])
def test_compose_is_bounded_regular_utf8_file(scanner, tmp_path, kind):
    compose = tmp_path / 'compose.yaml'
    if kind == 'large':
        compose.write_bytes(b'a' * (scanner['MAX_COMPOSE_BYTES'] + 1))
    elif kind == 'directory':
        compose.mkdir()
    elif kind == 'symlink':
        target = tmp_path / 'target'
        target.write_text(compose_text())
        compose.symlink_to(target)
    elif kind == 'fifo':
        scanner['os'].mkfifo(compose)
    else:
        compose.write_bytes(b'\xff')
    with pytest.raises((ValueError, OSError)):
        scanner['database_image'](compose)


@pytest.mark.parametrize('failure', [None, 'database-pull', 'pull', 'db', 'scan'])
def test_database_check_pulls_only_pinned_image_and_never_starts_database(scanner, tmp_path, monkeypatch, failure):
    (tmp_path / 'compose.yaml').write_text(compose_text())
    monkeypatch.setitem(scanner['database_image'].__globals__, 'ROOT', tmp_path)
    calls, paths = fake_docker(monkeypatch, scanner, failure=failure, database_reference=DATABASE_IMAGE)
    assert scanner['check'](database=True) == (0 if failure is None else 2)
    assert calls[0] == ['docker', 'pull', DATABASE_IMAGE]
    assert not any(command[:2] == ['docker', 'build'] for command in calls)
    assert not any(command[:3] == ['docker', 'image', 'rm'] for command in calls)
    for command in calls:
        if command[:2] == ['docker', 'run']:
            assert scanner['TRIVY_IMAGE'] in command
            assert DATABASE_IMAGE not in command
    assert all(not path.exists() for path in paths)


def test_database_check_builds_and_scans_patched_image(scanner, tmp_path, monkeypatch):
    compose = compose_text('${DB_IMAGE:-project-meme-db:local}')
    (tmp_path / 'compose.yaml').write_text(compose)
    (tmp_path / 'Dockerfile.db').write_text('FROM ' + DATABASE_IMAGE + '\nRUN apt-get update\n')
    monkeypatch.setitem(scanner['database_image'].__globals__, 'ROOT', tmp_path)
    calls, paths = fake_docker(monkeypatch, scanner, database_reference=DATABASE_IMAGE)
    assert scanner['check'](database=True) == 0
    build = next(c for c in calls if c[:2] == ['docker', 'build'])
    assert '--no-cache' in build and build[build.index('-f') + 1] == 'Dockerfile.db'
    scanned = build[build.index('-t') + 1]
    assert any(c[:3] == ['docker', 'image', 'save'] and c[-1] == scanned for c in calls)
    assert not any(c[:2] == ['docker', 'pull'] and c[-1] == DATABASE_IMAGE for c in calls)
    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize('case', ['missing', 'invalid', 'both'])
def test_database_parse_errors_do_not_run_docker(scanner, tmp_path, monkeypatch, case):
    monkeypatch.setitem(scanner['database_image'].__globals__, 'ROOT', tmp_path)
    if case != 'missing':
        (tmp_path / 'compose.yaml').write_text(compose_text('${DATABASE_IMAGE}'))
    calls, paths = fake_docker(monkeypatch, scanner)
    image = 'project-meme:local' if case == 'both' else None
    assert scanner['check'](image, database=True) == 2
    assert calls == [] and paths == []


def test_cli_rejects_image_and_database_combination(scanner, monkeypatch):
    monkeypatch.setattr(scanner['sys'], 'argv', ['container_check.py', '--database', '--image', 'project-meme:local'])
    with pytest.raises(SystemExit) as error:
        scanner['main']()
    assert error.value.code == 2


def test_primary_package_log_keeps_epoch_and_debian_release(scanner, capsys):
    value = report()
    value['Results'][0]['Packages'] = [
        {'Name': 'ffmpeg', 'Version': '7.1.5', 'Epoch': 7, 'Release': '0+deb13u1'},
        {'Name': 'libpcre2-8-0', 'Version': '10.46', 'Release': '1~deb13u3'},
    ]
    assert scanner['summarize'](value) == 0
    output = capsys.readouterr().out
    assert '7:7.1.5-0+deb13u1' in output and '10.46-1~deb13u3' in output
