"""標準ライブラリだけでローカル設定を作成。既存の秘密値は上書きしない。"""
from pathlib import Path
import secrets
root = Path(__file__).resolve().parents[1]
env = root / '.env'
if env.exists():
    print('.env は既に存在します。変更しません。')
else:
    media = Path.home() / 'Movies/project-meme'
    for part in ('inbox', 'media', 'media/staging'):
        (media / part).mkdir(parents=True, exist_ok=True)
    values = {
        'DB_PASSWORD': secrets.token_hex(32), 'APP_DB_PASSWORD': secrets.token_hex(32),
        'SECRET_KEY': secrets.token_hex(32), 'MEDIA_DIR': str(media / 'media'),
        'MAX_FILE_BYTES': '1000000000', 'MAX_TOTAL_BYTES': '30000000000',
        'RETENTION_DAYS': '60', 'PORT': '8000',
    }
    with env.open('x') as f:
        env.chmod(0o600)
        f.write(''.join(f'{k}={v}\n' for k,v in values.items()))
    print('ローカル設定と保存先を用意しました。秘密値は表示していません。')
