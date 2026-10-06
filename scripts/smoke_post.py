"""隔離CI環境専用。実HTTPでCodex投稿コマンドを最後まで通す。"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import json
import uuid
from PIL import Image
from post import LocalClient


def main():
    if os.environ.get('MEME_TEST_MODE') != '1' or os.environ.get('MEDIA_ROOT') != '/tmp/meme-test-media':
        raise SystemExit('テスト専用環境以外では実行できません。')
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        photo = root / 'test.png'
        Image.new('RGB', (8, 8), 'orange').save(photo)
        draft = root / 'draft.json'
        draft.write_text(json.dumps({'kind': 'observation', 'entry': {
            'submission_key': str(uuid.uuid4()), 'occurred_at': '2026-10-06T12:00:00+09:00',
            'description': '自動テストの架空記録。'}, 'media': [{'path': str(photo)}]}))
        command = [sys.executable, 'scripts/post.py', str(draft), '--submit']
        first = subprocess.run(command, check=True, capture_output=True, text=True).stdout
        second = subprocess.run(command, check=True, capture_output=True, text=True).stdout
        assert first == second, '再送で結果が変わりました。'
        url = first.splitlines()[0].split('http://localhost:8000', 1)[1]
        page = LocalClient(8000).request('GET', url, json_body=False)
        assert '自動テストの架空記録。' in page and 'test.png' in page
    print('Codex投稿コマンド: 実HTTPで本文・添付・再送・画面表示を確認しました。')


if __name__ == '__main__':
    main()
