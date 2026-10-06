"""実DBで期間・対象・根拠と、取得上限の整合性を検証する。"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.config import settings
from app.db import transaction
from app.main import app
from app.models import Base, Cat, DictionaryItem, Evidence, Health, Observation, ObservationTag, Tag, now
from app.reviews import SOURCE_LIMIT, build_weekly_review, review_range


@pytest.fixture
def review_client():
    assert os.environ.get('MEME_TEST_MODE') == '1' and settings.media_root == Path('/tmp/meme-test-media'), \
        'Use python3 scripts/test.py; refusing to clear non-test data'
    settings.media_root.mkdir(parents=True, exist_ok=True)
    (settings.media_root / 'staging').mkdir(exist_ok=True)
    with transaction(exclusive=True) as db:
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(delete(table))
    for path in settings.media_root.rglob('*'):
        if path.is_file():
            path.unlink()
    with TestClient(app, base_url='http://localhost:8000') as client:
        client.get('/')
        yield client


def record(db, model=Observation, when=None, **values):
    base = dict(cat_id=1, occurred_at=when or datetime(2026, 10, 5, 0, tzinfo=timezone.utc),
        submission_key=str(uuid.uuid4()), submission_hash='a' * 64)
    base.update({'description': '鳴いた', 'location': '寝室'} if model is Observation else
                {'event_type': 'other', 'details': 'メモ'})
    row = model(**{**base, **values})
    db.add(row)
    db.flush()
    return row


def test_week_uses_jst_half_open_range_and_real_active_cat_only(review_client):
    _, _, begin, finish = review_range('2026-10-06')
    with transaction() as db:
        record(db, when=begin - timedelta(microseconds=1))
        first = record(db, when=begin, description='【サンプル】という言葉を含む実記録', location='')
        last = record(db, when=finish - timedelta(microseconds=1))
        record(db, when=finish)
        record(db, is_sample=True)
        record(db, deletion_requested_at=now())
        db.add(Cat(id=2, name='別の猫'))
        db.flush()
        record(db, cat_id=2)
        health = record(db, Health, when=begin)
        record(db, Health, is_sample=True)
        record(db, Health, deletion_requested_at=now())
        ids = [first.id, last.id]
    response = review_client.get('/api/reviews/week?end=2026-10-06')
    assert response.status_code == 200, response.text
    report = response.json()
    assert report['start'] == '2026-09-30' and report['end'] == '2026-10-06'
    assert report['counts'] == {'observations': 2, 'health': 1}
    assert [row['id'] for row in report['observations']] == ids
    assert report['observations'][0]['occurred_at'] == '2026-09-30T00:00:00+09:00'
    assert report['days'][0] == {'date': '2026-09-30', 'observations': 1, 'health': 1}
    assert report['days'][-1]['observations'] == 1
    assert report['health'][0]['url'] == f'/entries/health/{health.id}'
    assert {'name': '場所未入力', 'count': 1} in report['locations']
    assert '頻度ではありません' in report['scope_notice']


def test_week_hypotheses_include_both_assessments_and_source_links(review_client):
    with transaction() as db:
        item = DictionaryItem(cat_id=1, kind='hypothesis', title='かまってほしい', hypothesis='予想')
        cue = DictionaryItem(cat_id=1, kind='cue', title='遊ぼう', cue='呼びかけ')
        db.add_all([item, cue]); db.flush()
        expected = []
        for assessment in ['supports', 'contradicts', 'unknown']:
            observation = record(db)
            expected.append(observation.id)
            db.add(Evidence(dictionary_item_id=item.id, observation_id=observation.id,
                            assessment=assessment, rationale='飼い主の評価理由'))
        sample = record(db, is_sample=True)
        deleted = record(db, deletion_requested_at=now())
        for observation in [sample, deleted]:
            db.add(Evidence(dictionary_item_id=item.id, observation_id=observation.id, assessment='supports'))
        # 辞書の合図は予想の評価として集計しない。
        db.add(Evidence(dictionary_item_id=cue.id, observation_id=expected[0], assessment='unknown'))
        tag = db.scalar(select(Tag).order_by(Tag.id))
        db.add(ObservationTag(observation_id=expected[0], tag_id=tag.id))
        db.add(ObservationTag(observation_id=sample.id, tag_id=tag.id))
        record(db, Health, event_type='weight', weight_value='4.250', weight_unit='kg')
        record(db, Health, event_type='vomit', count=3)
        db.flush()
        report = build_weekly_review(db, '2026-10-06')
    assert len(report['hypotheses']) == 1
    hypothesis = report['hypotheses'][0]
    assert hypothesis['counts'] == {'supports': 1, 'contradicts': 1, 'unknown': 1}
    assert [source['observation_id'] for source in hypothesis['evidence']] == expected
    assert all(source['url'] == f"/entries/observation/{source['observation_id']}" for source in hypothesis['evidence'])
    assert hypothesis['url'] == f'/dictionary/{item.id}' and hypothesis['omitted'] == 0
    assert report['tags'] == [{'id': tag.id, 'name': tag.name, 'count': 1}]
    assert next(row for row in report['health'] if row['event_type'] == 'weight')['weight_value'] == '4.250'
    assert next(row for row in report['health_types'] if row['event_type'] == 'vomit')['count'] == 1
    assert json.loads(json.dumps(report, ensure_ascii=False)) == report


def test_empty_week_does_not_claim_normal_health_and_escapes_sources(review_client):
    empty = review_client.get('/api/reviews/week?end=2026-10-06').json()
    assert empty['counts'] == {'observations': 0, 'health': 0}
    assert len(empty['days']) == 7 and not empty['hypotheses']
    page = review_client.get('/review/week?end=2026-10-06')
    assert page.status_code == 200
    assert '未記録は正常やゼロを意味しません' in page.text
    assert '/review/week?end=2026-09-29' in page.text
    with transaction() as db:
        record(db, description='<script>alert(1)</script>')
    page = review_client.get('/review/week?end=2026-10-06')
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in page.text and '<script>alert(1)</script>' not in page.text


@pytest.mark.parametrize('end', ['20261006', '2026-2-03', '2026-02-30', '2026-10-06T00:00',
    '0001-01-01', '9999-12-31', 'no date', '２０２６-１０-０６'])
def test_week_rejects_invalid_dates(end):
    with pytest.raises(HTTPException) as error:
        review_range(end)
    assert error.value.status_code == 422


def test_week_default_end_is_jst_today(monkeypatch):
    monkeypatch.setattr('app.reviews.now', lambda: datetime(2026, 1, 1, 15, tzinfo=timezone.utc))
    start, end, begin, finish = review_range()
    assert str(end) == '2026-01-02' and str(start) == '2025-12-27'
    assert begin.isoformat() == '2025-12-26T15:00:00+00:00'
    assert finish.isoformat() == '2026-01-02T15:00:00+00:00'


def test_week_bounds_sources_without_losing_totals_or_full_record_text(review_client):
    with transaction() as db:
        hypothesis = DictionaryItem(cat_id=1, kind='hypothesis', title='かまってほしい', hypothesis='予想' * 500)
        db.add(hypothesis); db.flush()
        ids = []
        for i in range(SOURCE_LIMIT + 1):
            observation = record(db, description='メ' * 10000)
            ids.append(observation.id)
            record(db, Health, details='メ' * 10000)
            db.add(Evidence(dictionary_item_id=hypothesis.id, observation_id=observation.id,
                            assessment='unknown', rationale='理' * 3000))
        db.flush()
        report = build_weekly_review(db, '2026-10-06')
        assert len(db.get(Observation, ids[0]).description) == 10000
    assert report['counts'] == {'observations': SOURCE_LIMIT + 1, 'health': SOURCE_LIMIT + 1}
    assert len(report['observations']) == len(report['health']) == SOURCE_LIMIT
    assert report['omitted']['observations'] == report['omitted']['health'] == 1
    assert [row['id'] for row in report['observations']] == ids[:-1]
    assert report['observations'][0]['description_truncated']
    assert report['health'][0]['details_truncated']
    hypothesis = report['hypotheses'][0]
    assert hypothesis['counts']['unknown'] == SOURCE_LIMIT + 1
    assert len(hypothesis['evidence']) == SOURCE_LIMIT and hypothesis['omitted'] == 1
    assert hypothesis['content_truncated'] and hypothesis['evidence'][0]['rationale_truncated']
    page = review_client.get('/review/week?end=2026-10-06')
    assert '集計には全件を含めています' in page.text and '続きは元の記録' in page.text


def test_week_api_bounds_large_japanese_payload_for_codex_client(review_client):
    with transaction() as db:
        for i in range(SOURCE_LIMIT + 1):
            item = DictionaryItem(cat_id=1, kind='hypothesis', title='予' * 120, hypothesis='説' * 3000)
            db.add(item); db.flush()
            observation = record(db, description='メ' * 10000, location=str(i) + '寝' * 197)
            record(db, Health, details='メ' * 10000)
            db.add(Evidence(dictionary_item_id=item.id, observation_id=observation.id,
                            assessment='unknown', rationale='理' * 3000))
        db.flush()
    response = review_client.get('/api/reviews/week?end=2026-10-06')
    assert response.status_code == 200
    assert len(response.content) < 1_000_000
    assert response.json()['omitted']['hypotheses'] == 1
    assert response.json()['omitted']['locations'] == 1


@pytest.mark.parametrize('character', ['🐈', '\x01'])
def test_week_payload_budget_handles_unicode_and_json_escaping(review_client, character):
    with transaction() as db:
        db.get(Cat, 1).name = character * 80
        tags = []
        for i in range(SOURCE_LIMIT):
            tag = Tag(name=str(i) + character * 37)
            db.add(tag); db.flush(); tags.append(tag)
        expected = []
        for i in range(SOURCE_LIMIT):
            item = DictionaryItem(cat_id=1, kind='hypothesis', title=character * 120,
                                  hypothesis=character * 3000)
            db.add(item); db.flush()
            observation = record(db, description=character * 10000, location=str(i) + character * 197)
            expected.append(observation.id)
            db.add(ObservationTag(observation_id=observation.id, tag_id=tags[i].id))
            record(db, Health, details=character * 10000)
            db.add(Evidence(dictionary_item_id=item.id, observation_id=observation.id,
                            assessment='unknown', rationale=character * 3000))
        db.flush()
    response = review_client.get('/api/reviews/week?end=2026-10-06')
    assert response.status_code == 200, response.text[:1000]
    assert len(response.content) <= 900_000
    report = response.json()
    assert report['payload_truncated']
    assert report['counts'] == {'observations': SOURCE_LIMIT, 'health': SOURCE_LIMIT}
    assert [row['id'] for row in report['observations']] == expected
    assert sum(item['counts']['unknown'] for item in report['hypotheses']) == SOURCE_LIMIT
    assert len(report['hypotheses']) == len(report['locations']) == len(report['tags']) == SOURCE_LIMIT
    assert all(row['url'] == f"/entries/observation/{row['id']}" for row in report['observations'])
    with transaction() as db:
        assert db.get(Observation, expected[0]).description == character * 10000
