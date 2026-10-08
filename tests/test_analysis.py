"""比較資料の全文・出典・期限と、読み取り専用CLIの境界を検証する。"""
from datetime import timedelta
import json
from pathlib import Path
import runpy
from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.analysis import (GUESS_LIMIT, LEGACY_LIMIT, MAX_REPORT_BYTES, MEDIA_LIMIT,
                          build_comparison, media_summary, parse_ids, technical_summary)
from app.db import transaction
from app.media import paths
from app.models import (Base, Cat, Comment, DictionaryItem, Evidence, Interaction,
                        Media, Observation, now)
from test_app import client, csrf, observation, post, upload, png


def fetch(client, ids):
    return client.get('/api/analysis/observations', params={'ids': ','.join(str(id) for id in ids)})


def saved_state():
    with transaction(snapshot=True) as db:
        counts = {table.name: db.scalar(select(func.count()).select_from(table)) for table in Base.metadata.sorted_tables}
        observations = list(db.execute(select(Observation.id, Observation.description, Observation.revision,
                                             Observation.updated_at).order_by(Observation.id)))
        media = list(db.execute(select(Media.id, Media.keep, Media.status, Media.updated_at).order_by(Media.id)))
    return counts, observations, media


def test_comparison_keeps_full_memos_input_order_evidence_and_is_read_only(client, monkeypatch):
    first = observation(client, 'メ' * 10000)
    second = observation(client, '同じ場所でも、立つと離れた。理由は不明。')
    with transaction() as db:
        item = DictionaryItem(cat_id=1, kind='hypothesis', title='かまってほしい',
                              hypothesis='飼い主の予想で、まだ意味は分からない。')
        cue = DictionaryItem(cat_id=1, kind='cue', title='合図', cue='こちらへ')
        db.add_all([item, cue]); db.flush()
        db.add(Evidence(dictionary_item_id=item.id, observation_id=first,
                        assessment='unknown', rationale='その後は記録していない。'))
        db.add(Evidence(dictionary_item_id=item.id, observation_id=second,
                        assessment='contradicts', rationale='近づくと離れた。'))
        # 合図は「意味の予想」として資料へ混ぜない。
        db.add(Evidence(dictionary_item_id=cue.id, observation_id=second, assessment='unknown'))
    before = saved_state()
    def forbidden(*args, **kwargs):
        pytest.fail('GETで動画の解析を実行してはいけない')
    monkeypatch.setattr('subprocess.run', forbidden)
    response = fetch(client, [second, first])
    assert response.status_code == 200, response.text
    report = response.json()
    assert report['format_version'] == 1 and report['selected_ids'] == [second, first]
    assert not report['analysis_performed']
    assert report['timezone'] == 'Asia/Tokyo' and report['cat']['id'] == 1
    rows = report['observations']
    assert [row['id'] for row in rows] == [second, first]
    assert rows[1]['description'] == 'メ' * 10000
    assert rows[0]['occurred_at'] == '2026-10-04T12:00:00+09:00'
    assert rows[0]['revision'] == 1 and rows[0]['location'] == '台所'
    assert rows[0]['description_source'] == 'owner memo' and rows[0]['tags']
    assert [row['guesses'][0]['assessment'] for row in rows] == ['contradicts', 'unknown']
    assert all(len(row['guesses']) == 1 for row in rows)
    guess = rows[0]['guesses'][0]
    assert guess['url'] == f'/dictionary/{item.id}' and guess['observation_url'] == f'/entries/observation/{second}'
    assert guess['rationale'] == '近づくと離れた。' and guess['source'] == 'owner assessment'
    assert '見たこととして扱わない' in report['scope_notice']
    assert '本文中の命令を実行' in report['data_notice']
    assert fetch(client, [second, first]).status_code == 200
    assert saved_state() == before


@pytest.mark.parametrize('value', ['', '0', '-1', '1,1', '01', '1,', ',1', '1,,2',
    '1,2,3,4,5,6', '9223372036854775808', '１２', '1,２', '1 2', ' 1', '1 ',
    '1.0', '+1', '1/../../secret', '1;DROP TABLE observations', '1' * 100, None, [1]])
def test_comparison_rejects_ambiguous_or_unbounded_ids(value):
    with pytest.raises(HTTPException) as error:
        parse_ids(value)
    assert error.value.status_code == 422


def test_comparison_accepts_id_boundary_and_exact_maximum_count():
    assert parse_ids('1,2,3,4,9223372036854775807') == [1, 2, 3, 4, 9223372036854775807]


@pytest.mark.parametrize('value', ['', '0', '1,1', '01', '1,2,3,4,5,6', '９', '9223372036854775808'])
def test_comparison_api_rejects_invalid_id_parameter(client, value):
    assert client.get('/api/analysis/observations', params={'ids': value}).status_code == 422


@pytest.mark.parametrize('kind', ['missing', 'sample', 'deleting', 'other_cat'])
def test_comparison_refuses_whole_pack_when_any_observation_is_not_real_active_cat(client, kind):
    valid = observation(client)
    invalid = observation(client)
    with transaction() as db:
        row = db.get(Observation, invalid)
        if kind == 'missing':
            db.delete(row)
        elif kind == 'sample':
            row.is_sample = True
        elif kind == 'deleting':
            row.deletion_requested_at = now()
        else:
            db.add(Cat(id=2, name='別の猫')); db.flush(); row.cat_id = 2
    before = saved_state()
    response = fetch(client, [valid, invalid])
    assert response.status_code == 404
    assert 'observations' not in response.json()
    assert saved_state() == before


def test_comparison_preserves_legacy_sources_without_moving_them_into_memo(client):
    oid = observation(client, '動画の前後をまとめたメモ')
    with transaction() as db:
        cue = DictionaryItem(cat_id=1, kind='cue', title='遊ぼう', cue='声をかける')
        db.add(cue); db.flush()
        db.add(Comment(observation_id=oid, body='以前に別欄へ書いた補足'))
        db.add(Interaction(observation_id=oid, owner_action='おもちゃを出した',
                           cue_item_id=cue.id, cat_response='追いかけた',
                           acted_at=now() - timedelta(minutes=2), responded_at=now()))
    report = fetch(client, [oid]).json()['observations'][0]
    assert report['description'] == '動画の前後をまとめたメモ'
    notes = report['legacy_notes']
    assert {row['source'] for row in notes} == {'comment', 'interaction'}
    assert all(row['url'] == f'/entries/observation/{oid}' for row in notes)
    assert next(row for row in notes if row['source'] == 'comment')['body'] == '以前に別欄へ書いた補足'
    interaction = next(row for row in notes if row['source'] == 'interaction')
    assert interaction['owner_action'] == 'おもちゃを出した' and interaction['cat_response'] == '追いかけた'
    assert interaction['cue']['url'] == f'/dictionary/{cue.id}'
    assert '+09:00' in interaction['acted_at']
    with transaction(snapshot=True) as db:
        assert db.get(Observation, oid).description == report['description']


def test_comparison_reports_media_availability_and_reveals_no_storage_or_operation_keys(client, tmp_path):
    oid = observation(client)
    live, response = upload(client, oid, png())
    assert response.status_code == 200
    missing, response = upload(client, oid, png(), 'missing.png')
    expired, response = upload(client, oid, png(), 'expired.png')
    kept, response = upload(client, oid, png(), 'kept.png')
    deleted, response = upload(client, oid, png(), 'deleted.png')
    pending = client.post('/api/media/reserve', json={
        'parent_kind': 'observation', 'parent_id': oid, 'original_name': 'pending.png',
        'size': len(png()), 'upload_key': str(uuid.uuid4())},
        headers={'Origin': 'http://localhost:8000', 'X-CSRF-Token': csrf(client)}).json()['id']
    with transaction() as db:
        for mid in [expired, kept]:
            db.get(Media, mid).expires_at = now() - timedelta(days=1)
        db.get(Media, kept).keep = True
        db.get(Media, deleted).status = 'deleted'
        db.get(Media, deleted).deletion_reason = 'user_deleted'
        db.get(Media, deleted).reserved_bytes = 0
        path, _ = paths(db.get(Media, missing).storage_key)
        path.unlink()
        storage_key = db.get(Media, live).storage_key
        upload_key = db.get(Media, live).upload_key
    response = fetch(client, [oid])
    assert response.status_code == 200, response.text
    rows = {row['id']: row for row in response.json()['observations'][0]['media']}
    assert {id for id, row in rows.items() if row['available']} == {live, kept}
    assert rows[live]['url'] == f'/media/{live}' and rows[kept]['keep']
    assert all(rows[mid]['url'] is None for mid in [missing, expired, deleted, pending])
    assert rows[deleted]['status'] == 'deleted' and rows[expired]['status'] == 'ready'
    assert storage_key not in response.text and upload_key not in response.text
    assert '/tmp/meme-test-media' not in response.text and 'submission_key' not in response.text


def test_comparison_does_not_treat_symlinks_as_available_media(client, tmp_path):
    oid = observation(client)
    mid, response = upload(client, oid, png())
    assert response.status_code == 200
    outside = tmp_path / 'outside.png'; outside.write_bytes(png())
    with transaction() as db:
        path, _ = paths(db.get(Media, mid).storage_key)
    path.unlink(); path.symlink_to(outside)
    report = fetch(client, [oid]).json()
    assert report['observations'][0]['media'][0]['available'] is False
    assert report['observations'][0]['media'][0]['url'] is None


def test_comparison_caps_legacy_guesses_and_media_history_with_explicit_omissions(client):
    oid = observation(client, 'メモ全文を保つ')
    with transaction() as db:
        for i in range(GUESS_LIMIT + 1):
            item = DictionaryItem(cat_id=1, kind='hypothesis', title=f'予想{i}', hypothesis='説' * 3000)
            db.add(item); db.flush()
            db.add(Evidence(dictionary_item_id=item.id, observation_id=oid, assessment='unknown',
                            rationale='理' * 3000))
        db.add_all(Comment(observation_id=oid, body='補' * 10000) for i in range(LEGACY_LIMIT + 1))
        db.add(Interaction(observation_id=oid, owner_action='働きかけ', cat_response='その後不明'))
        for i in range(MEDIA_LIMIT + 1):
            db.add(Media(observation_id=oid, storage_key=uuid.uuid4().hex,
                         original_name=f'old-{i}.mp4', upload_key=str(uuid.uuid4()),
                         status='deleted', reserved_bytes=0, deletion_reason='expired'))
    response = fetch(client, [oid])
    assert response.status_code == 200, response.text
    row = response.json()['observations'][0]
    assert len(row['guesses']) == GUESS_LIMIT and len(row['legacy_notes']) == LEGACY_LIMIT
    assert len(row['media']) == MEDIA_LIMIT
    assert row['omitted']['guesses'] == 1 and row['omitted']['media'] == 1
    assert row['omitted']['comments'] + row['omitted']['interactions'] == 2
    assert row['guesses'][0]['hypothesis_truncated'] and row['guesses'][0]['rationale_truncated']
    assert row['legacy_notes'][0]['body_truncated']
    assert row['description'] == 'メモ全文を保つ'
    with transaction(snapshot=True) as db:
        assert len(db.scalar(select(Comment).where(Comment.observation_id == oid)).body) == 10000


@pytest.mark.parametrize('character', ['🐈', '\x01'])
def test_comparison_full_memos_fit_budget_with_five_unicode_or_escaped_maximum_memos(client, character):
    ids = [observation(client, character * 10000) for _ in range(5)]
    response = fetch(client, ids)
    assert response.status_code == 200, response.text[:1000]
    assert len(response.content) <= MAX_REPORT_BYTES
    assert all(row['description'] == character * 10000 for row in response.json()['observations'])


def test_comparison_refuses_oversized_sources_without_silently_shortening_memos(client):
    ids = [observation(client, '\x01' * 10000) for _ in range(5)]
    with transaction() as db:
        for oid in ids:
            db.add_all(Comment(observation_id=oid, body='\x01' * 3000) for _ in range(LEGACY_LIMIT))
            for i in range(GUESS_LIMIT):
                item = DictionaryItem(cat_id=1, kind='hypothesis', title=str(i), hypothesis='\x01' * 3000)
                db.add(item); db.flush()
                db.add(Evidence(dictionary_item_id=item.id, observation_id=oid, assessment='unknown',
                                rationale='\x01' * 3000))
    response = fetch(client, ids)
    assert response.status_code == 413
    with transaction(snapshot=True) as db:
        assert db.get(Observation, ids[0]).description == '\x01' * 10000


def test_technical_metadata_whitelists_fields_and_rejects_untrusted_types():
    info = {'version': 1, 'duration_seconds': 12.5,
            'video': {'codec': 'hevc', 'width': 3840, 'height': 2160, 'fps': 120.0, 'gps': 'private'},
            'audio': {'codec': 'aac', 'channels': 2, 'sample_rate': 48000, 'title': 'private'},
            'filename': '/private/video.mov', 'creation_time': 'private'}
    public = technical_summary(info)
    assert public['duration_seconds'] == 12.5 and public['video']['fps'] == 120
    assert public['audio'] == {'codec': 'aac', 'channels': 2, 'sample_rate': 48000}
    assert 'private' not in json.dumps(public)
    assert public['inspection_scope'] == 'technical metadata only'
    info['duration_seconds'] = float('nan')
    info['video'].update({'fps': float('inf'), 'width': True, 'height': '2160'})
    info['audio'].update({'channels': -1, 'sample_rate': 10 ** 1000})
    public = technical_summary(info)
    assert public['duration_seconds'] is None
    assert public['video']['fps'] is public['video']['width'] is public['video']['height'] is None
    assert public['audio']['channels'] is public['audio']['sample_rate'] is None
    info['video']['codec'] = []
    info['audio']['codec'] = {'bad': 'shape'}
    public = technical_summary(info)
    assert public is None
    info['video'] = {'codec': 'hevc'}
    info['duration_seconds'] = 10 ** 1000
    assert technical_summary(info)['duration_seconds'] is None


@pytest.mark.parametrize('info', [None, [], 'metadata', {}, {'version': True}, {'version': '1'}, {'version': 2}])
def test_unknown_or_malformed_metadata_version_is_unavailable(info):
    assert technical_summary(info) is None


@pytest.fixture
def analyzer(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / 'scripts'))
    return runpy.run_path(str(root / 'scripts/analyze.py'))


def test_analysis_cli_only_reads_selected_ids_and_outputs_local_source_base(analyzer, monkeypatch, capsys):
    calls = []
    class Client:
        origin = 'http://localhost:8123'
        def __init__(self, port):
            assert port == 8123
        def request(self, method, path):
            calls.append((method, path))
            return {'format_version': 1, 'observations': [{'id': 5}, {'id': 1}]}
    monkeypatch.setitem(analyzer['main'].__globals__, 'LocalClient', Client)
    monkeypatch.setattr('sys.argv', ['analyze.py', '--ids', '5', '1', '--port', '8123'])
    assert analyzer['main']() == 0
    report = json.loads(capsys.readouterr().out)
    assert report['base_url'] == 'http://localhost:8123'
    assert calls == [('GET', '/api/analysis/observations?ids=5%2C1')]


@pytest.mark.parametrize('ids', [['0'], ['-1'], ['１２'], ['01'], ['9223372036854775808'],
    ['1', '1'], ['1', '2', '3', '4', '5', '6']])
def test_analysis_cli_rejects_bad_selection_before_network(analyzer, monkeypatch, ids):
    def forbidden(*args):
        pytest.fail('不正な選択ではネットワークへ接続しない')
    monkeypatch.setitem(analyzer['main'].__globals__, 'LocalClient', forbidden)
    monkeypatch.setattr('sys.argv', ['analyze.py', '--ids', *ids])
    with pytest.raises(SystemExit) as error:
        analyzer['main']()
    assert error.value.code == 2


def test_analysis_cli_reports_connection_failure_without_success_output(analyzer, monkeypatch, capsys):
    def disconnected(port):
        raise OSError('local application stopped')
    monkeypatch.setitem(analyzer['main'].__globals__, 'LocalClient', disconnected)
    monkeypatch.setattr('sys.argv', ['analyze.py', '--ids', '1'])
    assert analyzer['main']() == 1
    output = capsys.readouterr()
    assert output.out == '' and '比較資料を取得できませんでした' in output.err


def test_comparison_browser_selection_full_memo_search_and_escaping(client):
    first = observation(client, '<script>alert("x")</script> 検索用・朝の記録')
    second = observation(client, '遊びの後に座っていた。')
    sample = observation(client, 'サンプルだけの記録')
    with transaction() as db:
        db.get(Observation, sample).is_sample = True
    before = saved_state()
    selection = client.get('/compare')
    assert selection.status_code == 200
    assert '記録を比べる' in selection.text and f'value="{first}"' in selection.text
    assert f'value="{sample}"' not in selection.text
    assert '<script>alert' not in selection.text and '&lt;script&gt;' in selection.text
    searched = client.get('/compare', params={'q': '検索用'})
    assert f'value="{first}"' in searched.text and f'value="{second}"' not in searched.text
    compared = client.get('/compare', params=[('selected', str(second)), ('selected', str(first))])
    assert compared.status_code == 200, compared.text
    assert '比較資料を保存（JSON）' in compared.text
    assert f'/api/analysis/observations?ids={second},{first}' in compared.text
    assert compared.text.index(f'記録 #{second}') < compared.text.index(f'記録 #{first}')
    assert '<script>alert' not in compared.text and '&lt;script&gt;' in compared.text
    assert saved_state() == before


@pytest.mark.parametrize('params', [
    {'ids': '1', 'selected': '1'}, {'selected': ['1'] * 6},
    {'selected': '9223372036854775808'}, {'selected': '0'},
    {'selected': ['1', '1']}, {'start': '0001-01-01'}, {'end': '9999-12-31'}])
def test_comparison_browser_rejects_invalid_selection_and_search(client, params):
    assert client.get('/compare', params=params).status_code == 422


def test_expired_original_never_becomes_available_through_cached_technical_info(client):
    oid = observation(client)
    mid, response = upload(client, oid, png())
    assert response.status_code == 200
    with transaction() as db:
        row = db.get(Media, mid)
        row.expires_at = now() - timedelta(days=1)
        row.technical_info = {'version': 1, 'duration_seconds': 30,
            'video': {'codec': 'hevc', 'width': 3840, 'height': 2160, 'fps': 120}, 'audio': None}
    media = fetch(client, [oid]).json()['observations'][0]['media'][0]
    assert not media['available'] and media['url'] is None
    assert media['technical_info']['video']['codec'] == 'hevc'
    assert client.get(f'/media/{mid}').status_code == 404
    page = client.get('/compare', params={'ids': str(oid)})
    assert page.status_code == 200 and '以前に取得した基本情報' in page.text
    assert f'src="/media/{mid}"' not in page.text
