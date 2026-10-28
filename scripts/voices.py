"""鳴き声の傾向（種類・行動・その後・意味の予想の集計）を読み取り専用JSONで表示する。"""
import argparse
import http.client
import json
import sys
from post import LocalClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    try:
        client = LocalClient(args.port)
        report = client.request('GET', '/api/analysis/voices')
        report['base_url'] = client.origin
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, http.client.HTTPException) as error:
        print(f'鳴き声の傾向を取得できませんでした: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
