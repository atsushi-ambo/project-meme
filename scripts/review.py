"""直近7日間の実記録を読み取り、Codexでの振り返りに使うJSONを表示する。"""
import argparse
import http.client
import json
import sys
from urllib.parse import urlencode
from post import LocalClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--end', default='', help='最終日 YYYY-MM-DD（省略時は今日、日本時間）')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    try:
        client = LocalClient(args.port)
        report = client.request('GET', '/api/reviews/week?' + urlencode({'end': args.end}))
        report['base_url'] = client.origin
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError, http.client.HTTPException) as error:
        print(f'振り返りを取得できませんでした: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
