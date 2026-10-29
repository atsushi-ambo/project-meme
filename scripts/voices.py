"""鳴き声の傾向（種類・行動・その後・意味の予想の集計）を読み取り専用JSONで表示する。

--similar 区間ID で、その声とかたちが近い区間を表示する。
"""
import argparse
import http.client
import json
import sys
from analyze import observation_argument
from post import LocalClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--similar', type=observation_argument, metavar='SOUND_ID',
                        help='この声の区間とかたちが近い区間を表示する')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    try:
        client = LocalClient(args.port)
        path = f'/api/analysis/voices/{args.similar}/similar' if args.similar else '/api/analysis/voices'
        report = client.request('GET', path)
        report['base_url'] = client.origin
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, http.client.HTTPException) as error:
        print(f'鳴き声の傾向を取得できませんでした: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
