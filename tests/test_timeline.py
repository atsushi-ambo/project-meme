"""一日の一覧の範囲、並びと元記録への導線を実DBで検証する。"""
from datetime import datetime, timedelta, timezone
import uuid

from app.db import transaction
from app.models import Cat, Health, Media, Observation, SoundAnalysis, now
from test_reviews import record, review_client  # noqa: F401


def test_timeline_jst_order_and_filters(review_client):
    begin = datetime(2026, 10, 5, 15, tzinfo=timezone.utc)
    with transaction() as db:
        record(db, when=begin - timedelta(microseconds=1), description='前日')
        first = record(db, Health, when=begin, event_type='food', details='食事',
                       food_amount='全部', time_is_approximate=True)
        obs = record(db, when=begin + timedelta(hours=1), description='<script>観察</script>')
        record(db, when=begin + timedelta(days=1), description='翌日')
        record(db, is_sample=True, when=begin, description='サンプル')
        record(db, deletion_requested_at=now(), when=begin, description='削除中')
        db.add(Cat(id=2, name='別の猫')); db.flush()
        record(db, cat_id=2, when=begin, description='別の猫')
        media = Media(observation_id=obs.id, storage_key=uuid.uuid4().hex, original_name='録音.wav',
                      reserved_bytes=100, upload_key=str(uuid.uuid4()), status='pending')
        db.add(media); db.flush()
        for n in range(4):
            db.add(SoundAnalysis(media_id=media.id, start_ms=n*1000, end_ms=n*1000+500, rms=0, peak=0))
        db.flush()
        first_id, obs_id = first.id, obs.id
    page = review_client.get('/timeline?day=2026-10-06')
    assert page.status_code == 200
    text = page.text
    assert text.index(f'/entries/health/{first_id}') < text.index(f'/entries/observation/{obs_id}')
    assert '食べた量：全部' in text and 'およそ' in text
    assert '&lt;script&gt;観察&lt;/script&gt;' in text and '<script>観察</script>' not in text
    assert all(s not in text for s in ['前日', '翌日', 'サンプル', '削除中', '別の猫'])
    assert '録音.wav' in text and '添付内の秒数' in text and 'ほか 1 区間' in text
    assert text.count(f'/entries/observation/{obs_id}#sound-') == 3
    assert '原本は利用できません' in text


def test_timeline_pagination_and_empty_day(review_client):
    when = datetime(2026, 10, 5, 15, tzinfo=timezone.utc)
    with transaction() as db:
        ids = [record(db, when=when, description=f'同時刻 {i}').id for i in range(31)]
    first = review_client.get('/timeline?day=2026-10-06').text
    second = review_client.get('/timeline?day=2026-10-06&page=2').text
    assert all(f'/entries/observation/{id}"' in first for id in ids[:30])
    assert f'/entries/observation/{ids[-1]}"' not in first
    assert f'/entries/observation/{ids[-1]}"' in second
    assert '次のページ' in first and '前のページ' in second
    empty = review_client.get('/timeline?day=2026-10-07')
    assert empty.status_code == 200 and '未記録は、出来事がなかったことを意味しません' in empty.text
    for query in ['day=2026-02-30', 'day=0001-01-01', 'page=0', 'page=abc']:
        assert review_client.get('/timeline?' + query).status_code == 422


def test_timeline_precision_and_truncated_notes(review_client):
    when = datetime(2026, 10, 5, 15, tzinfo=timezone.utc)
    with transaction() as db:
        record(db, Health, when=when, time_is_approximate=False, details='長' * 301)
        record(db, Health, when=when, time_is_approximate=None, details='')
    page = review_client.get('/timeline?day=2026-10-06')
    assert page.status_code == 200
    assert '入力した時刻' in page.text and '日時精度不明' in page.text
    assert '続きは元の記録で確認できます。' in page.text
    assert '長' * 301 not in page.text
    assert review_client.get('/timeline?day=0001-01-02').status_code == 200
    assert review_client.get('/timeline?day=9999-12-30').status_code == 200
