"""標準ライブラリだけでローカル設定を作成。既存の秘密値は上書きしない。"""
import os
from pathlib import Path
import secrets
root = Path(__file__).resolve().parents[1]
env = root / '.env'
media = Path.home() / 'Movies/project-meme'
private = Path.home() / 'Library/Application Support/project-meme'


def private_directories(media_dir):
    """記録・動画・下書き・分析資料を、同じPCの他のOSユーザーから読めないようにする。"""
    paths = [media_dir, media_dir / 'staging', private, private / 'drafts', private / 'analysis']
    if media_dir == media / 'media':
        paths[:0] = [media, media / 'inbox']
    for path in paths:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.is_dir():
            raise SystemExit(f'{path} がフォルダではありません。確認してください。')
        path.chmod(0o700)


if env.exists():
    configured = dict(line.split('=', 1) for line in env.read_text().splitlines() if '=' in line and not line.startswith('#'))
    private_directories(Path(configured.get('MEDIA_DIR', media / 'media')).expanduser())
    env.chmod(0o600)
    print('.env は既に存在します。秘密値は変更せず、保存先と.envの権限だけを確認しました。')
else:
    private_directories(media / 'media')
    values = {
        'DB_PASSWORD': secrets.token_hex(32), 'APP_DB_PASSWORD': secrets.token_hex(32),
        'SECRET_KEY': secrets.token_hex(32), 'MEDIA_DIR': str(media / 'media'),
        'MAX_FILE_BYTES': '1000000000', 'MAX_TOTAL_BYTES': '30000000000',
        'RETENTION_DAYS': '60', 'PORT': '8000',
    }
    with open(os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as f:
        f.write(''.join(f'{k}={v}\n' for k,v in values.items()))
    print('ローカル設定と保存先を用意しました。秘密値は表示していません。')
