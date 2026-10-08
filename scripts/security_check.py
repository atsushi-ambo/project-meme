"""公開されたPython依存一覧だけを、使い捨てDocker内で脆弱性監査する。"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid


PYTHON_IMAGE = ('python:3.13-slim-trixie@sha256:'
                'bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c')
PIN = re.compile(r'([A-Za-z0-9][A-Za-z0-9._-]*)(\[[A-Za-z0-9_,.-]+\])?==([A-Za-z0-9][A-Za-z0-9.!+_-]*)')
HASH = re.compile(r'--hash=sha256:[0-9a-f]{64}')
AUDIT_COMMAND = '''
python -m venv /audit/venv
/audit/venv/bin/python -m pip --isolated install --disable-pip-version-check --no-cache-dir --only-binary=:all: --require-hashes --no-deps -r /input/tools.lock
status=0
/audit/venv/bin/python -m pip_audit --cache-dir /tmp/audit-cache --require-hashes --disable-pip --strict --progress-spinner off --timeout 20 --desc off --aliases on -r /input/app.lock || status=$?
/audit/venv/bin/python -m pip_audit --cache-dir /tmp/audit-cache --require-hashes --disable-pip --strict --progress-spinner off --timeout 20 --desc off --aliases on -r /input/tools.lock || status=$?
exit "$status"
'''


def read_lock(path, *, hashed=False, extras=False):
    """URL・追加index・参照ファイルを拒否し、正確な公開パッケージ名だけを扱う。"""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 100_000:
        raise ValueError(path.name + ': 固定した依存一覧の通常ファイルを指定してください。')
    raw = path.read_text(encoding='utf-8').replace('\\\n', ' ')
    entries = {}
    lines = []
    for number, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        match = PIN.fullmatch(parts[0])
        if not match or (match.group(2) and not extras):
            raise ValueError(f'{path.name}:{number}: パッケージ名と固定バージョンだけを指定してください。')
        hashes = parts[1:]
        if hashed:
            if not hashes or not all(HASH.fullmatch(value) for value in hashes):
                raise ValueError(f'{path.name}:{number}: SHA-256ハッシュが必要です。')
        elif hashes:
            raise ValueError(f'{path.name}:{number}: 追加のpipオプションは受け付けません。')
        name = re.sub(r'[-_.]+', '-', match.group(1)).lower()
        if name in entries:
            raise ValueError(f'{path.name}:{number}: 同じパッケージを重複指定できません。')
        entries[name] = match.group(3)
        lines.append(' '.join(parts))
    if not entries or len(entries) > 500:
        raise ValueError(path.name + ': 1〜500件の固定した依存関係が必要です。')
    return entries, '\n'.join(lines) + '\n'


def read_inputs(root):
    app, app_text = read_lock(root / 'requirements.lock', hashed=True)
    direct, _ = read_lock(root / 'requirements.txt', extras=True)
    tools, tools_text = read_lock(root / 'requirements.security.lock', hashed=True)
    if any(app.get(name) != version for name, version in direct.items()):
        raise ValueError('requirements.txtとrequirements.lockの固定バージョンが一致していません。')
    if 'pip-audit' not in tools:
        raise ValueError('requirements.security.lockにpip-auditが必要です。')
    return app_text, tools_text


def audit(root):
    app_text, tools_text = read_inputs(root)
    project = 'project-meme-audit-' + uuid.uuid4().hex[:12]
    env = {key: value for key, value in os.environ.items() if not key.startswith('COMPOSE_')}
    # lockの内容を検証した時点で固定する。.envやアプリ本体はマウントしない。
    with tempfile.TemporaryDirectory(prefix=project + '-') as directory:
        snapshot = Path(directory)
        paths = []
        for name, content in [('app.lock', app_text), ('tools.lock', tools_text)]:
            path = snapshot / name
            path.write_text(content, encoding='utf-8')
            path.chmod(0o644)  # Docker内の非特権ユーザーが読み取るため。
            paths.append(path)
        command = ['docker', 'run', '--rm', '--name', project,
                   '--read-only', '--user', '65534:65534',
                   '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                   '--memory', '1g', '--cpus', '2', '--pids-limit', '128',
                   '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=256m',
                   '--tmpfs', '/audit:rw,nosuid,nodev,exec,size=512m']
        for path in paths:
            command += ['--mount', f'type=bind,source={path},target=/input/{path.name},readonly']
        command += [PYTHON_IMAGE, 'sh', '-ec', AUDIT_COMMAND]
        print('Security audit: public Python dependency names/versions only; no media or .env.', flush=True)
        try:
            return subprocess.run(command, cwd=root, env=env, timeout=600).returncode
        except KeyboardInterrupt:
            return 130
        except subprocess.TimeoutExpired:
            print('Security audit timed out; the audit did not complete.', flush=True)
            return 124
        finally:
            # --rmに加えて、中断・タイムアウトでもこの監査用コンテナだけを片付ける。
            subprocess.run(['docker', 'rm', '-f', project], cwd=root, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)


def main(argv=None):
    parser = argparse.ArgumentParser(description='DockerだけでPython依存の既知の脆弱性を監査します。')
    parser.add_argument('--check-lock', action='store_true', help='一覧の固定と整合性だけ確認（通信なし）')
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        if args.check_lock:
            read_inputs(root)
            print('Dependency locks are pinned and consistent.')
            return 0
        return audit(root)
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError) as exc:
        print('Security audit incomplete: ' + str(exc), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
