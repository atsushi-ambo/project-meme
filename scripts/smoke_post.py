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
        video = root / 'test.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
            'color=c=green:s=128x96:r=30', '-t', '0.2', '-c:v', 'libx264',
            '-threads:v', '1', '-pix_fmt', 'yuv420p', str(video)],
            check=True, capture_output=True, timeout=15)
        draft = root / 'draft.json'
        draft.write_text(json.dumps({'kind': 'observation', 'entry': {
            'submission_key': str(uuid.uuid4()), 'occurred_at': '2030-01-01T12:00:00+09:00',
            'description': '自動テストの架空記録。'}, 'guesses': ['preset:attention'], 'media': [{'path': str(photo), 'keep': True}, {'path': str(video)}]}))
        command = [sys.executable, 'scripts/post.py', str(draft), '--submit']
        first = subprocess.run(command, check=True, capture_output=True, text=True).stdout
        second = subprocess.run(command, check=True, capture_output=True, text=True).stdout
        assert first == second, '再送で結果が変わりました。'
        url = first.splitlines()[0].split('http://localhost:8000', 1)[1]
        page = LocalClient(8000).request('GET', url, json_body=False)
        assert '自動テストの架空記録。' in page and 'test.png' in page
        assert 'かまってほしい' in page, '飼い主の予想が画面に表示されません。'
        oid = int(url.rsplit('/', 1)[1])
        snapshot = json.loads(subprocess.run([sys.executable, 'scripts/append.py', '--show', str(oid)],
            check=True, capture_output=True, text=True).stdout)
        assert snapshot['revision'] == 1 and snapshot['media'][0]['keep'] is True
        append = root / 'append.json'
        append.write_text(json.dumps({'observation_id': oid, 'operation_key': str(uuid.uuid4()),
            'expected_revision': snapshot['revision'], 'text': 'その後の架空の様子。'}))
        append_command = [sys.executable, 'scripts/append.py', str(append), '--submit']
        assert '追記保存済み: ' in subprocess.run(append_command, check=True, capture_output=True, text=True).stdout
        assert '同じ追記を確認' in subprocess.run(append_command, check=True, capture_output=True, text=True).stdout
        after = LocalClient(8000).request('GET', f'/api/observations/{oid}')
        assert after['description'] == '自動テストの架空記録。\n\nその後の架空の様子。'
        assert after['revision'] == 2 and after['media'][0]['keep'] is True
        # 元の投稿を再送しても追記と添付の保管設定を失わない。
        subprocess.run(command, check=True, capture_output=True, text=True)
        assert LocalClient(8000).request('GET', f'/api/observations/{oid}') == after
        report = json.loads(subprocess.run([sys.executable, 'scripts/review.py', '--end', '2030-01-01'],
            check=True, capture_output=True, text=True).stdout)
        assert report['base_url'] == 'http://localhost:8000'
        assert report['counts']['observations'] == 1
        assert any(row['url'] == url for row in report['observations'])
        assert any(item['title'] == 'かまってほしい' and item['counts']['unknown'] >= 1
                   for item in report['hypotheses'])
        comparison = json.loads(subprocess.run([sys.executable, 'scripts/analyze.py', '--ids', str(oid)],
            check=True, capture_output=True, text=True).stdout)
        assert comparison['base_url'] == 'http://localhost:8000'
        row = comparison['observations'][0]
        assert row['description'] == after['description'] and row['revision'] == 2
        assert row['guesses'][0]['assessment'] == 'unknown'
        video_info = next(media['technical_info'] for media in row['media'] if media['original_name'] == 'test.mp4')
        assert video_info['video']['codec'] == 'h264' and video_info['video']['width'] == 128
        assert video_info['audio'] is None
        assert '記録を比べる' in LocalClient(8000).request('GET', f'/compare?ids={oid}', json_body=False)
    print('Codex投稿コマンド: 実HTTPで本文・予想・添付・保管指定・追記・再送・動画情報・比較CLI・画面表示を確認しました。')


if __name__ == '__main__':
    main()
