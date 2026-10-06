"""既存の観察メモへの追記。既定は確認のみ、--submitで保存、--show IDで現在の記録を表示。"""
import argparse
import http.client
import json
from pathlib import Path
import sys
import uuid
from post import LocalClient


MAX_ID = 9223372036854775807
DRAFT_KEYS = {'observation_id', 'operation_key', 'expected_revision', 'text'}


def positive_integer(value, label):
    # bool は int のサブクラスなので isinstance だけでは受け付けてしまう。
    if type(value) is not int or not 1 <= value <= MAX_ID:
        raise ValueError(label + 'は正の整数で指定してください。')
    return value


def observation_argument(value):
    if not value.isascii() or not value.isdecimal() or len(value) > 19:
        raise argparse.ArgumentTypeError('観察IDは正の整数で指定してください。')
    try:
        return positive_integer(int(value), '観察ID')
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def load_draft(path):
    # 一度の読み取りにも上限を設け、確認中にファイルが増えても制限する。
    with path.open('rb') as file:
        raw = file.read(65537)
    if len(raw) > 65536:
        raise ValueError('下書きは64KB以内にしてください。')
    draft = json.loads(raw.decode('utf-8'))
    if not isinstance(draft, dict) or set(draft) != DRAFT_KEYS:
        raise ValueError('下書きには observation_id、operation_key、expected_revision、text だけを指定してください。')
    positive_integer(draft['observation_id'], '観察ID')
    positive_integer(draft['expected_revision'], '確認した記録の版番号')
    if not isinstance(draft['operation_key'], str):
        raise ValueError('operation_key は再送時も同じ UUID 文字列を指定してください。')
    try:
        uuid.UUID(draft['operation_key'])
    except ValueError as error:
        raise ValueError('operation_key は再送時も同じ UUID 文字列を指定してください。') from error
    if not isinstance(draft['text'], str):
        raise ValueError('追記は空でない1万文字以内の文章で指定してください。')
    draft['text'] = draft['text'].strip()
    if not 1 <= len(draft['text']) <= 10000:
        raise ValueError('追記は空でない1万文字以内の文章で指定してください。')
    return draft


def submit(draft, client):
    # 最新の revision に自動で差し替えない。確認後の編集はサーバーが拒否する。
    body = {key: draft[key] for key in ('operation_key', 'expected_revision', 'text')}
    entry = client.request('POST', '/api/observations/' + str(draft['observation_id']) + '/append', body)
    status = '追記保存済み（同じ追記を確認）: ' if entry['replayed'] else '追記保存済み: '
    print(status + client.origin + entry['url'], flush=True)
    return entry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('draft', nargs='?', type=Path, help='確認済みの版番号と追記を記載したJSON下書き')
    parser.add_argument('--show', type=observation_argument, metavar='ID', help='現在の本文と版番号を読み取る（保存しません）')
    parser.add_argument('--submit', action='store_true', help='下書きの追記を保存する')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    if args.show is not None and (args.draft is not None or args.submit):
        parser.error('--show は下書きや --submit と同時に指定できません。')
    if args.show is None and args.draft is None:
        parser.error('下書き、または --show ID を指定してください。')
    try:
        if args.show is not None:
            client = LocalClient(args.port)
            entry = client.request('GET', '/api/observations/' + str(args.show))
            entry['base_url'] = client.origin
            print(json.dumps(entry, ensure_ascii=False, indent=2))
            return 0
        draft = load_draft(args.draft)
        if not args.submit:
            print(json.dumps(draft, ensure_ascii=False, indent=2))
            print('確認のみです。既存のメモの末尾に追記するには --submit を付けてください。')
            return 0
        submit(draft, LocalClient(args.port))
        return 0
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as error:
        if args.show is not None:
            print(f'記録を取得できませんでした: {error}', file=sys.stderr)
        else:
            print(f'追記未完了: {error}\n通信が途切れた場合は同じ下書きで再実行すると重複を防げます。版番号の競合時は現在の記録を確認し、追記内容を見直してください。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
