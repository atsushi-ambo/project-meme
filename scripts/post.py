"""Codexが整理したJSONをlocalhostへ投稿。既定は確認のみ、--submitで保存。"""
import argparse
import hashlib
from html.parser import HTMLParser
import http.client
from http.cookies import SimpleCookie
import json
from pathlib import Path
import sys
import uuid


class TokenParser(HTMLParser):
    token = None
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta' and attrs.get('name') == 'csrf-token':
            self.token = attrs.get('content')


class LocalClient:
    def __init__(self, port):
        if not 1 <= port <= 65535:
            raise ValueError('ポート番号が不正です。')
        self.port = port
        self.origin = f'http://localhost:{port}'
        self.cookie = ''
        self.token = ''
        page = self.request('GET', '/', json_body=False)
        parser = TokenParser()
        parser.feed(page)
        if not parser.token:
            raise ValueError('アプリのCSRFトークンを取得できません。')
        self.token = parser.token

    def request(self, method, path, body=None, *, size=None, json_body=True):
        # ホスト・リダイレクト・プロキシを外部へ切り替えられないローカル専用通信。
        headers = {'Host': f'localhost:{self.port}', 'Origin': self.origin,
                   'Cookie': self.cookie, 'X-CSRF-Token': self.token}
        if json_body and body is not None and size is None:
            body = json.dumps(body, ensure_ascii=False).encode()
            headers['Content-Type'] = 'application/json'
        if size is not None:
            headers.update({'Content-Length': str(size), 'Content-Type': 'application/octet-stream'})
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=300)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            cookies = SimpleCookie()
            cookies.load(response.getheader('Set-Cookie', ''))
            if 'meme_browser' in cookies:
                self.cookie = 'meme_browser=' + cookies['meme_browser'].value
            raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError('応答が大きすぎます。')
            text = raw.decode('utf-8')
            if not 200 <= response.status < 300:
                try: detail = json.loads(text).get('detail', '保存できませんでした。')
                except ValueError: detail = '保存できませんでした。'
                raise ValueError(f'HTTP {response.status}: {detail}')
            return json.loads(text) if json_body else text
        finally:
            connection.close()


def load_draft(path):
    if path.stat().st_size > 65536:
        raise ValueError('下書きは64KB以内にしてください。')
    draft = json.loads(path.read_text())
    if not isinstance(draft, dict) or set(draft) - {'kind', 'entry', 'media'}:
        raise ValueError('下書きの項目が不正です。')
    if draft.get('kind') not in ('observation', 'health') or not isinstance(draft.get('entry'), dict):
        raise ValueError('kindとentryを指定してください。')
    uuid.UUID(draft['entry']['submission_key'])
    media = draft.get('media', [])
    if not isinstance(media, list) or len(media) > 10:
        raise ValueError('添付は10件までです。')
    for item in media:
        if not isinstance(item, dict) or set(item) - {'path', 'captured_at'}:
            raise ValueError('添付の項目が不正です。')
        file = Path(item['path']).expanduser()
        if not file.is_absolute() or not file.is_file():
            raise ValueError('添付には存在するファイルの絶対パスを指定してください。')
        if file.suffix.lower() not in {'.mov', '.mp4', '.jpg', '.jpeg', '.png', '.m4a', '.mp3', '.wav'}:
            raise ValueError('対応形式のファイルを選んでください。MOVの拡張子は変更不要です。')
        if not 0 < file.stat().st_size <= 1_000_000_000:
            raise ValueError('添付は空でない1GB以内のファイルを指定してください。')
    return draft


def submit(draft, client):
    # 全ファイルを先に読み取り確認。サイズにかかわらずメモリ使用は1MB程度。
    files = []
    for item in draft.get('media', []):
        path = Path(item['path']).expanduser()
        digest = hashlib.sha256()
        with path.open('rb') as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b''):
                digest.update(chunk)
        files.append((item, path, path.stat(), digest.hexdigest()))
    entry = client.request('POST', '/api/entries/' + draft['kind'], draft['entry'])
    print('本文保存済み: ' + client.origin + entry['url'], flush=True)
    for item, path, stat, digest in files:
        key = uuid.uuid5(uuid.UUID(draft['entry']['submission_key']),
                         json.dumps([path.name, digest, item.get('captured_at')]))
        for attempt in range(20):
            reservation = client.request('POST', '/api/media/reserve', {
                'parent_kind': draft['kind'], 'parent_id': entry['id'],
                'original_name': path.name, 'size': stat.st_size,
                'upload_key': str(key), 'captured_at': item.get('captured_at')})
            if reservation['status'] == 'failed':
                key = uuid.uuid5(key, 'retry')
                continue
            if reservation['status'] == 'ready':
                break
            if reservation['status'] != 'pending':
                raise ValueError('削除済み・削除中の添付です。自動で再登録はしません。')
            with path.open('rb') as file:
                current = path.stat()
                if (current.st_size, current.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                    raise ValueError('送信前にファイルが変更されました。再実行してください。')
                client.request('PUT', '/api/media/' + str(reservation['id']), file, size=stat.st_size)
            break
        else:
            raise ValueError('添付の再試行上限です。アプリで保存状態を確認してください。')
        print('添付保存済み: ' + path.name, flush=True)
    return entry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('draft', type=Path)
    parser.add_argument('--submit', action='store_true')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    try:
        draft = load_draft(args.draft)
        if not args.submit:
            print(json.dumps(draft, ensure_ascii=False, indent=2))
            print('確認のみです。保存するには --submit を付けてください。')
            return 0
        submit(draft, LocalClient(args.port))
        return 0
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as error:
        print(f'投稿未完了: {error}\n本文だけ保存されている場合があります。同じ下書きで再実行すると重複を防げます。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
