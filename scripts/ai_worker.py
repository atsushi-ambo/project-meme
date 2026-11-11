"""ホストで常駐し、アプリに積まれた動画の分析ジョブをAIで下書きにする。

アプリ（Docker）は外部へ送信しない。このワーカーが飼い主のMacで、ログイン済みの
Codex / Claude Code のCLI（サブスクリプション）またはローカルのOllamaを順に試す。
AIへ渡すのは scripts/ai_review.py と同じ report.json と静止画だけで、音声・動画の原本は渡さない。
結果はアプリに「未確認の下書き」として表示され、記録・声の区間・予想は変更しない。

  python3 scripts/ai_worker.py --check       # 使えるAIを確認（何も送らない）
  python3 scripts/ai_worker.py --once        # ジョブを一件だけ処理
  python3 scripts/ai_worker.py --install     # ログイン時に自動起動（launchd）
  python3 scripts/ai_worker.py --uninstall

順番は環境変数 MEME_AI_ORDER（既定 codex,claude,ollama）、Ollamaのモデルは MEME_OLLAMA_MODEL。
"""
import argparse
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from ai_review import BASE, TOOLS, bundle_path, generate_any, ollama_model  # noqa: E402
from post import LocalClient  # noqa: E402
from prepare_analysis import prepare  # noqa: E402

LABEL = 'jp.project-meme.ai-worker'
PLIST = Path.home() / 'Library/LaunchAgents' / f'{LABEL}.plist'
LOG = Path.home() / 'Library/Logs/project-meme-ai-worker.log'
INTERVAL = 30
IDLE_INTERVAL = 120
LIMIT_WORDS = ('usage limit', 'rate limit', '429', 'quota')
FIXED_HINT = '使用量の上限やログインを確認してください'
RESULT_RETRIES = 5


def log(*parts):
    print(time.strftime('%Y-%m-%d %H:%M:%S'), *parts, flush=True)


def read_env():
    path = ROOT / '.env'
    values = {}
    for line in path.read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip()
    if len(values.get('SECRET_KEY', '')) < 32:
        raise ValueError('.env の SECRET_KEY を読めません。scripts/setup.py を確認してください。')
    return values


def worker_token(secret):
    # app/ai_jobs.py と同じ導出。合言葉を別のファイルに保存しない。
    return hmac.new(secret.encode(), b'meme-ai-worker-v1', hashlib.sha256).hexdigest()


def order():
    tools = [t.strip() for t in os.environ.get('MEME_AI_ORDER', 'codex,claude,ollama').split(',') if t.strip()]
    unknown = [t for t in tools if t not in TOOLS]
    if unknown or not tools:
        raise ValueError(f'MEME_AI_ORDER には {", ".join(TOOLS)} を指定してください。')
    return tools


def codex_limited():
    """全セッション共通の codex-cc の制限記録があれば、その間はCodexを使わない。"""
    if shutil.which('codex-cc') is None:
        return False
    try:
        return subprocess.run(['codex-cc', 'limit-status'], stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=30).returncode == 1
    except (OSError, subprocess.SubprocessError):
        return False


def mark_codex_limited(message):
    # ai_review.pyの固定の案内文ではなく、CLIが出した文言だけで判定する。
    detail = message.replace(FIXED_HINT, '').lower()
    if shutil.which('codex-cc') and any(word in detail for word in LIMIT_WORDS):
        subprocess.run(['codex-cc', 'mark-limited'], stdin=subprocess.DEVNULL, capture_output=True,
                       timeout=30, check=False)


def available_tools():
    tools = []
    for tool in order():
        if tool == 'codex' and codex_limited():
            log('Codexは利用制限中のため飛ばします。')
            continue
        if tool != 'ollama' and shutil.which(tool) is None:
            continue
        tools.append(tool)
    return tools


def client(env):
    local = LocalClient(int(env.get('PORT', '8000')))
    local.extra_headers = {'X-Meme-Worker': worker_token(env['SECRET_KEY'])}
    return local


def process_one(env):
    """ジョブを一件処理する。ジョブがなければFalse。"""
    local = client(env)
    job = local.request('POST', '/api/ai/jobs/claim', {}).get('job')
    if not job:
        return False
    log(f"ジョブ{job['id']}: 記録{job['observation_id']}・添付{job['media_id']}を分析します。")
    result = {'lease': job['lease']}
    try:
        bundle = bundle_path(prepare(local, job['observation_id'], job['media_id'], BASE))
        tools = available_tools()
        if not tools:
            raise ValueError('使えるAIがありません（ログイン・利用制限・Ollamaを確認してください）。')
        def note(message):
            log(message)
            if message.startswith('codex '):
                mark_codex_limited(message)
        tool, text, images = generate_any(bundle, tools, log=note)
        label = f'ollama（{ollama_model()}）' if tool == 'ollama' else tool
        result.update(result=text[:20000], tool=label[:40], images_sent=images)
        log(f"ジョブ{job['id']}: {label} で下書きを作りました。")
    except Exception as error:  # noqa: BLE001 どんな失敗でも結果を返し、ジョブを止めたままにしない。
        result = {'lease': job['lease'], 'error': (str(error) or type(error).__name__)[:500]}
        log(f"ジョブ{job['id']}: 失敗しました: {error}")
    send_result(env, job['id'], result)
    return True


def send_result(env, job_id, result):
    """AIの出力を失わないよう、保守中などの一時的な失敗は少し待って再送する。"""
    if isinstance(result.get('result'), str):
        result['result'] = result['result'].replace('\x00', '')
    for attempt in range(RESULT_RETRIES):
        try:
            return client(env).request('POST', f'/api/ai/jobs/{job_id}/result', result)
        except (OSError, ValueError, http.client.HTTPException) as error:
            message = str(error)
            rejected = 'HTTP 422' in message or ('HTTP 409' in message and 'ジョブ' in message)
            if rejected or attempt == RESULT_RETRIES - 1:
                log(f'ジョブ{job_id}: 結果を保存できませんでした: {error}')
                if 'HTTP 422' in message and 'result' in result:
                    return send_result(env, job_id, {'lease': result['lease'], 'error': 'AIの出力を保存できませんでした。'})
                return None
            time.sleep(min(60, 5 * 2 ** attempt))


def check():
    env = read_env()
    print('AIの順番:', ', '.join(order()))
    for tool in order():
        if tool == 'ollama':
            try:
                connection = http.client.HTTPConnection('127.0.0.1', int(os.environ.get('MEME_OLLAMA_PORT', '11434')), timeout=5)
                connection.request('GET', '/api/tags')
                names = [m.get('name') for m in json.loads(connection.getresponse().read()).get('models', [])]
                state = f"起動中・モデル {ollama_model()} {'あり' if ollama_model() in names else 'なし（ollama pull が必要）'}"
            except (OSError, ValueError, http.client.HTTPException):
                state = '起動していません（任意）'
        elif shutil.which(tool) is None:
            state = '見つかりません'
        else:
            state = '利用制限中' if tool == 'codex' and codex_limited() else 'あり'
        print(f'  {tool}: {state}')
    try:
        client(env)
        print('アプリ: 接続できました')
    except (OSError, ValueError, http.client.HTTPException) as error:
        print('アプリ: 接続できません', error)


def install():
    LOG.parent.mkdir(parents=True, exist_ok=True)
    path = ':'.join([str(Path.home() / '.local/bin'), '/opt/homebrew/bin', '/usr/local/bin',
                     '/usr/bin', '/bin', '/usr/sbin', '/sbin'])
    environment = {'PATH': path, 'HOME': str(Path.home()), 'LANG': 'ja_JP.UTF-8'}
    for key in ('MEME_AI_ORDER', 'MEME_OLLAMA_MODEL'):
        if os.environ.get(key):
            environment[key] = os.environ[key]
    plist = {'Label': LABEL, 'ProgramArguments': [sys.executable, str(Path(__file__).resolve())],
             'WorkingDirectory': str(ROOT), 'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 60,
             'ProcessType': 'Background', 'EnvironmentVariables': environment,
             'StandardOutPath': str(LOG), 'StandardErrorPath': str(LOG)}
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}', str(PLIST)], capture_output=True, check=False)
    with open(os.open(PLIST, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'wb') as file:
        plistlib.dump(plist, file)
    subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(PLIST)], check=True)
    print(f'自動起動を設定しました。ログ: {LOG}')


def uninstall():
    subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}', str(PLIST)], capture_output=True, check=False)
    PLIST.unlink(missing_ok=True)
    print('自動起動を解除しました。')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    action = parser.add_mutually_exclusive_group()
    for flag in ('--check', '--once', '--install', '--uninstall'):
        action.add_argument(flag, action='store_true')
    args = parser.parse_args()
    try:
        if args.check:
            return check()
        if args.install:
            return install()
        if args.uninstall:
            return uninstall()
        env = read_env()
        if args.once:
            print('処理しました。' if process_one(env) else '待っているジョブはありません。')
            return 0
        log('AIワーカーを開始しました。')
        while True:
            try:
                time.sleep(INTERVAL if process_one(env) is False else 1)
            except Exception as error:  # noqa: BLE001 常駐を止めない。
                # アプリやDockerが止まっている間は静かに待つ。
                log('アプリに接続できません。後で再試行します:', str(error)[:200])
                time.sleep(IDLE_INTERVAL)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print('AIワーカーを実行できません:', error, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
