"""飼い主が確認した声の区間と分類を保存する。既定は確認のみ、--submitで保存。

新しい区間: {"observation_id", "media_id", "start", "end", ...分類}
既存の区間の書き足し: {"observation_id", "sound_id", "expected_revision", ...変更する分類だけ}
編集前の確認: --show 声の区間ID --observation 観察ID
分類: call_type_id, call_count, behaviors, meaning, assessment, owner_response, outcome,
observation, hypothesis, unknown。値は飼い主が話した内容だけを入れる。
"""
import argparse
import http.client
import json
from pathlib import Path
import sys
from post import LocalClient

MAX_ID = 9223372036854775807
LABEL_KEYS = {'call_type_id', 'call_count', 'behaviors', 'meaning', 'assessment', 'owner_response',
              'outcome', 'observation', 'hypothesis', 'unknown'}


def record_id(value, label):
    if type(value) is not int or not 1 <= value <= MAX_ID:
        raise ValueError(label + 'は正の整数で指定してください。')
    return value


def id_argument(value):
    if not value.isascii() or not value.isdecimal() or len(value) > 19:
        raise argparse.ArgumentTypeError('IDは正の整数で指定してください。')
    try:
        return record_id(int(value), 'ID')
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def load_draft(path):
    with path.open('rb') as file:
        raw = file.read(65537)
    if len(raw) > 65536:
        raise ValueError('下書きは64KB以内にしてください。')
    draft = json.loads(raw.decode('utf-8'))
    if not isinstance(draft, dict):
        raise ValueError('下書きはJSONオブジェクトで指定してください。')
    record_id(draft.get('observation_id'), '観察ID')
    if 'sound_id' in draft:
        allowed = {'observation_id', 'sound_id', 'expected_revision'} | LABEL_KEYS
        record_id(draft['sound_id'], '声の区間ID')
        record_id(draft.get('expected_revision'), '確認した声の区間の版番号')
        if not LABEL_KEYS.intersection(draft):
            raise ValueError('書き足す分類を一つ以上指定してください。')
    else:
        allowed = {'observation_id', 'media_id', 'start', 'end'} | LABEL_KEYS
        record_id(draft.get('media_id'), '添付ID')
        if not all(isinstance(draft.get(key), (str, int, float)) and not isinstance(draft.get(key), bool)
                   for key in ('start', 'end')):
            raise ValueError('開始・終了を秒で指定してください。')
    if set(draft) - allowed:
        raise ValueError('下書きの項目が不正です: ' + ', '.join(sorted(set(draft) - allowed)))
    return draft


def submit(draft, client):
    oid = draft['observation_id']
    if 'sound_id' in draft:
        # 下書きで確認した版番号を維持する。競合時も読み直して自動再送しない。
        body = {key: value for key, value in draft.items() if key in LABEL_KEYS | {'expected_revision'}}
        result = client.request('POST', f'/api/observations/{oid}/sounds/{draft["sound_id"]}/labels', body)
        print('書き足し保存済み: ' + client.origin + result['observation_url'], flush=True)
    else:
        body = {key: str(value) if key in ('start', 'end') else value
                for key, value in draft.items() if key != 'observation_id'}
        result = client.request('POST', f'/api/observations/{oid}/sounds', body)
        print('声の区間を保存済み: ' + client.origin + result['observation_url'], flush=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('draft', nargs='?', type=Path, help='区間と分類を記載したJSON下書き')
    parser.add_argument('--show', type=id_argument, metavar='ID', help='声の区間の分類と版番号を読み取る（保存しません）')
    parser.add_argument('--observation', type=id_argument, metavar='ID', help='--showで読む区間が属する観察ID')
    parser.add_argument('--submit', action='store_true', help='下書きを保存する')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    if args.show is not None:
        if args.observation is None or args.draft is not None or args.submit:
            parser.error('--show は --observation と指定し、下書きや --submit と同時に指定できません。')
    elif args.draft is None or args.observation is not None:
        parser.error('下書き、または --show ID --observation ID を指定してください。')
    try:
        if args.show is not None:
            client = LocalClient(args.port)
            entry = client.request('GET', f'/api/observations/{args.observation}/sounds/{args.show}')
            entry['base_url'] = client.origin
            print(json.dumps(entry, ensure_ascii=False, indent=2))
            return 0
        draft = load_draft(args.draft)
        if not args.submit:
            print(json.dumps(draft, ensure_ascii=False, indent=2))
            print('確認のみです。保存するには --submit を付けてください。')
            return 0
        submit(draft, LocalClient(args.port))
        return 0
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as error:
        if args.show is not None:
            print(f'声の区間を取得できませんでした: {error}', file=sys.stderr)
            return 1
        print(f'声の区間を保存できませんでした: {error}\n'
              '「すでに保存されています」(409)は同じ区間が保存済みという意味です。'
              '記録の画面で確認し、書き足す場合はsound_idとexpected_revisionを指定してください。'
              '更新の409は版番号の競合または未指定です。現在の分類を読み直し、入力内容を見直してください。'
              '最新の版番号へ自動で差し替えて強制再送しないでください。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
