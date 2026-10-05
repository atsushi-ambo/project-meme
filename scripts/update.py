"""チェックアウト済みコードを、テスト・バックアップ後にローカルへ反映する。"""
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import backup as snapshots

ROOT = Path(__file__).resolve().parents[1]
STATE = Path.home() / 'Library/Application Support/project-meme'


def deliver(command, snapshot, destination, revision, previous):
    """副作用の入口を引数にし、各失敗段階をDockerなしでも検証できるようにする。"""
    candidate = 'project-meme:update-' + uuid.uuid4().hex[:12]
    recovery = 'project-meme:before-' + destination.name
    record = {'revision': revision, 'previous_image': previous,
              'candidate_image': candidate, 'recovery_image': recovery}
    migration_started = False
    stopped = False
    try:
        print('1/5 隔離環境でテストします。', flush=True)
        command([sys.executable, str(ROOT / 'scripts/test.py')])
        print('2/5 新しいイメージを準備します。', flush=True)
        command(['docker', 'image', 'tag', previous, recovery])
        command(['compose', 'build', 'web'], image=candidate)
        print('3/5 アプリを止めてバックアップします。', flush=True)
        stopped = True
        snapshot(destination, restart=False,
                 runner=lambda args, **kw: command(['compose', *args], image=previous, **kw))
        record['stage'] = 'backed_up'
        (destination / 'update.json').write_text(json.dumps(record, indent=2))
        print('4/5 DB構造を更新します。', flush=True)
        migration_started = True
        command(['compose', 'run', '--rm', '--no-deps', '-T', 'migrate'], image=candidate)
        print('5/5 起動とDB接続を確認します。', flush=True)
        command(['compose', 'up', '-d', '--no-deps', '--no-build', '--wait',
                 '--wait-timeout', '90', 'web'], image=candidate)
        command(['docker', 'image', 'tag', candidate, 'project-meme:local'])
        record['stage'] = 'complete'
        (destination / 'update.json').write_text(json.dumps(record, indent=2))
    except BaseException:
        if stopped:
            if migration_started:
                # 新DBと旧コードの互換性を仮定しない。自動復元で記録を失わせない。
                command(['compose', 'stop', 'web'], image=previous)
            else:
                command(['compose', 'start', 'web'], image=previous)
        if destination.exists():
            record['stage'] = 'failed_after_migration_started' if migration_started else 'failed_before_migration'
            (destination / 'update.json').write_text(json.dumps(record, indent=2))
        raise
    print(f'更新完了。バックアップ: {destination}', flush=True)


def main():
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'update.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('別の更新処理が実行中です。', file=sys.stderr)
            return 1
        values = snapshots.env_values()
        # 別のComposeプロジェクトや外部環境変数で保存先が変わることを防ぐ。
        env = {k: v for k, v in os.environ.items()
               if not k.startswith('COMPOSE_') and k not in values and k != 'APP_IMAGE'}

        def command(args, image=None, **kwargs):
            local_env = dict(env)
            if args[0] == 'compose':
                args = ['docker', 'compose', '--env-file', str(ROOT / '.env'),
                        '-p', 'project-meme', '-f', str(ROOT / 'compose.yaml'), *args[1:]]
                local_env['APP_IMAGE'] = image or 'project-meme:local'
            return subprocess.run(args, cwd=ROOT, env=local_env, check=True, **kwargs)

        def output(args):
            return command(args, capture_output=True, text=True).stdout.strip()

        if output(['git', 'status', '--porcelain']):
            print('未コミットの変更があります。コミット後に更新してください。', file=sys.stderr)
            return 1
        revision = output(['git', 'rev-parse', 'HEAD'])
        container = output(['compose', 'ps', '--status', 'running', '-q', 'web'])
        if not container:
            print('起動中の既存アプリが必要です。初回はREADMEの起動手順を使ってください。', file=sys.stderr)
            return 1
        previous = output(['docker', 'inspect', '--format', '{{.Image}}', container])
        destination = STATE / 'backups' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
        try:
            deliver(command, snapshots.backup, destination, revision, previous)
        except (subprocess.CalledProcessError, OSError, ValueError, KeyboardInterrupt):
            print(f'更新を完了できませんでした。バックアップ・段階の記録: {destination}\n'
                  'DB更新開始後の失敗ではアプリを停止します。READMEの復元手順で別環境に復元してください。', file=sys.stderr)
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
