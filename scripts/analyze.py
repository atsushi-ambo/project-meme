"""選んだ観察の比較資料をローカルAPIから取得する。保存・外部送信はしない。"""
import argparse
import http.client
import json
import sys
from urllib.parse import urlencode

from post import LocalClient

MAX_ID = 9223372036854775807


def observation_argument(value):
    if not value.isascii() or not value.isdecimal() or len(value) > 19 or value.startswith('0'):
        raise argparse.ArgumentTypeError('観察IDは半角の正整数で指定してください。')
    id = int(value)
    if not 1 <= id <= MAX_ID:
        raise argparse.ArgumentTypeError('観察IDの数値の範囲を確認してください。')
    return id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ids', nargs='+', required=True, type=observation_argument,
                        metavar='ID', help='比較する観察ID（重複なし、1〜5件）')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    if len(args.ids) > 5 or len(set(args.ids)) != len(args.ids):
        parser.error('比較する観察IDは重複なしで1〜5件指定してください。')
    try:
        client = LocalClient(args.port)
        query = urlencode({'ids': ','.join(str(id) for id in args.ids)})
        report = client.request('GET', '/api/analysis/observations?' + query)
        report['base_url'] = client.origin
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, http.client.HTTPException) as error:
        print(f'比較資料を取得できませんでした: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
