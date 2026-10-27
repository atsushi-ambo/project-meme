"""個人データ・.envを使わない、一回限りのDockerテスト環境。"""
import os
from pathlib import Path
import subprocess
import uuid


def main():
    root = Path(__file__).resolve().parents[1]
    project = 'project-meme-test-' + uuid.uuid4().hex[:12]
    compose = ['docker', 'compose', '--env-file', os.devnull,
               '-p', project, '-f', str(root / 'compose.test.yaml')]
    # 外からのCompose設定で接続先ファイルやenv fileが混ざるのを防ぐ。
    env = {key: value for key, value in os.environ.items() if not key.startswith('COMPOSE_')}
    def run(args):
        return subprocess.run(args, cwd=root, env=env).returncode
    print('Test environment: ' + project, flush=True)
    result = 1
    try:
        result = run(compose + ['run', '--build', '--rm', '-T', 'tests'])
        if result == 0:
            # HTTPサーバー起動と実PostgreSQLへの接続も毎回検証する。
            result = run(compose + ['up', '-d', '--no-deps', '--no-build',
                                    '--wait', '--wait-timeout', '90', 'web'])
        if result == 0:
            result = run(compose + ['exec', '-T', 'web', 'python', 'scripts/smoke_post.py'])
        if result == 0:
            # テストDB内だけで、本物のpg_dump/pg_restoreによる復元を検証する。
            result = run(compose + ['exec', '-T', 'db', 'sh', '-ec', """
                dump=$(mktemp)
                trap 'rm -f "$dump"; dropdb -U meme_owner --if-exists meme_restore_test' EXIT
                pg_dump -U meme_owner -d meme -Fc -f "$dump"
                createdb -U meme_owner -O meme_owner meme_restore_test
                pg_restore -U meme_owner -d meme_restore_test --exit-on-error --no-owner "$dump"
                psql -U meme_owner -d meme_restore_test -v ON_ERROR_STOP=1 -c 'SELECT version_num FROM alembic_version'
            """])

    except KeyboardInterrupt:
        result = 130
    finally:
        cleaned = run(compose + ['down', '--volumes', '--remove-orphans', '--timeout', '10'])
        # この実行だけに作ったタグ。通常アプリのイメージには触れない。
        subprocess.run(['docker', 'image', 'rm', project + ':test'], cwd=root, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['docker', 'image', 'rm', project + ':db'], cwd=root, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if cleaned and result == 0:
            result = cleaned
    return result


if __name__ == '__main__':
    raise SystemExit(main())
