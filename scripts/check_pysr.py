"""Run the synthetic PySR experiment or its dependency audit in disposable Docker."""
import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

from security_check import PYTHON_IMAGE, read_lock


TEST = '''
python -m venv /tmp/venv
/tmp/venv/bin/pip --isolated install --disable-pip-version-check --no-cache-dir --require-hashes --only-binary=:all: -r /input/requirements.lock
cd /input
/tmp/venv/bin/python -m unittest discover -s . -v
/tmp/venv/bin/python pysr_experiment.py --demo --niterations 2
'''
AUDIT = '''
python -m venv /tmp/venv
/tmp/venv/bin/pip --isolated install --disable-pip-version-check --no-cache-dir --require-hashes --no-deps --only-binary=:all: -r /input/security.lock
/tmp/venv/bin/python -m pip_audit --cache-dir /tmp/audit-cache --require-hashes --disable-pip --strict --progress-spinner off --timeout 20 --desc off -r /input/requirements.lock
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', action='store_true', help='audit experiment dependencies')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    exp = root / 'experiments' / 'pysr'
    # Mount only the experiment and verified public dependency lists; never mount .env or real data.
    _, lock = read_lock(exp / 'requirements.lock', hashed=True)
    direct, _ = read_lock(exp / 'requirements.in')
    resolved, _ = read_lock(exp / 'requirements.lock', hashed=True)
    if any(resolved.get(name) != version for name, version in direct.items()):
        raise ValueError('PySR requirements.in and requirements.lock disagree')
    _, security_lock = read_lock(root / 'requirements.security.lock', hashed=True)
    name = 'project-meme-pysr-' + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix=name + '-') as directory:
        snapshot = Path(directory)
        snapshot.chmod(0o755)
        for filename in ('pysr_experiment.py', 'test_experiment.py'):
            shutil.copyfile(exp / filename, snapshot / filename)
        (snapshot / 'requirements.lock').write_text(lock, encoding='utf-8')
        (snapshot / 'security.lock').write_text(security_lock, encoding='utf-8')
        for file in snapshot.iterdir():
            file.chmod(0o644)
        command = ['docker', 'run', '--rm', '--name', name, '--user', '65534:65534',
                   '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                   '--memory', '6g', '--memory-swap', '6g', '--cpus', '4', '--pids-limit', '512',
                   '--env', 'HOME=/tmp', '--env', 'JULIA_DEPOT_PATH=/tmp/julia',
                   '--env', 'PYTHONDONTWRITEBYTECODE=1', '--env', 'JULIA_NUM_THREADS=1',
                   '--env', 'JULIA_NUM_PRECOMPILE_TASKS=2',
                   '--mount', f'type=bind,source={snapshot},target=/input,readonly',
                   PYTHON_IMAGE, 'sh', '-ec', AUDIT if args.audit else TEST]
        try:
            return subprocess.run(command, timeout=1200).returncode
        except subprocess.TimeoutExpired:
            print('PySR verification timed out; not passed.', flush=True)
            return 124
        finally:
            subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30)


if __name__ == '__main__':
    raise SystemExit(main())
