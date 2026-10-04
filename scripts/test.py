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
    except KeyboardInterrupt:
        result = 130
    finally:
        cleaned = run(compose + ['down', '--volumes', '--remove-orphans', '--timeout', '10'])
        # この実行だけに作ったタグ。通常アプリのイメージには触れない。
        subprocess.run(['docker', 'image', 'rm', project + ':test'], cwd=root, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if cleaned and result == 0:
            result = cleaned
    return result


if __name__ == '__main__':
    raise SystemExit(main())
