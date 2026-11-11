"""ホストのAIワーカー向けジョブ：認証・貸出・二重取得・結果の表示と、記録を変えないことを検証する。"""
from datetime import timedelta
import pytest
from sqlalchemy import select
from app import ai_jobs
from app.config import settings
from app.db import transaction
from app.models import AiJob, Media, Observation, now
from test_app import client, csrf, observation, post, upload
from test_media_info import video_bytes, info_post

ORIGIN = {'Origin': 'http://localhost:8000'}


def worker(client, path, body=None, token=None):
    headers = {**ORIGIN, 'X-CSRF-Token': csrf(client),
               'X-Meme-Worker': ai_jobs.worker_token() if token is None else token}
    return client.post(path, json={} if body is None else body, headers=headers)


def video(client, video_bytes):
    oid = observation(client, '仕事部屋に入ってきて鳴いた。')
    mid, response = upload(client, oid, video_bytes, 'sample.mp4')
    assert response.status_code == 200, response.text
    return oid, mid


def test_button_queues_and_worker_saves_escaped_draft_without_changing_record(client, video_bytes):
    oid, mid = video(client, video_bytes)
    page = client.get(f'/entries/observation/{oid}').text
    assert 'AIで分析' in page
    with transaction() as db:
        before = db.get(Observation, oid).revision
    assert post(client, f'/observations/{oid}/ai/{mid}', {}).status_code == 303
    assert post(client, f'/observations/{oid}/ai/{mid}', {}).status_code == 303   # 二回押しても一件
    with transaction() as db:
        assert len(list(db.scalars(select(AiJob).where(AiJob.media_id == mid)))) == 1
    assert '分析待ち' in client.get(f'/entries/observation/{oid}').text

    assert worker(client, '/api/ai/jobs/claim', token='x' * 64).status_code == 403
    assert client.post('/api/ai/jobs/claim', json={}, headers={**ORIGIN, 'X-CSRF-Token': csrf(client)}).status_code == 403
    assert client.post('/api/ai/jobs/claim', json={}, headers={'Origin': 'http://evil.example',
        'X-CSRF-Token': csrf(client), 'X-Meme-Worker': ai_jobs.worker_token()}).status_code == 403
    job = worker(client, '/api/ai/jobs/claim').json()['job']
    assert job['observation_id'] == oid and job['media_id'] == mid and set(job) == {'id', 'lease', 'observation_id', 'media_id'}
    assert worker(client, '/api/ai/jobs/claim').json()['job'] is None

    result = {'lease': 'a' * 64, 'result': '<script>x</script>', 'tool': 'codex', 'images_sent': 3}
    assert worker(client, f"/api/ai/jobs/{job['id']}/result", result).status_code == 409
    result['lease'] = job['lease']
    assert worker(client, f"/api/ai/jobs/{job['id']}/result", {**result, 'result': 'x' * 20001}).status_code == 422
    assert worker(client, f"/api/ai/jobs/{job['id']}/result", result).json()['status'] == 'done'
    assert worker(client, f"/api/ai/jobs/{job['id']}/result", result).status_code == 409   # 再送は拒否
    page = client.get(f'/entries/observation/{oid}').text
    assert '&lt;script&gt;x&lt;/script&gt;' in page and '<script>x</script>' not in page
    assert 'codex · 画像3枚' in page and 'AIでもう一度分析' in page
    with transaction() as db:
        assert db.get(Observation, oid).revision == before


def test_failure_is_shown_and_ineligible_media_is_rejected(client, video_bytes):
    oid, mid = video(client, video_bytes)
    assert post(client, f'/observations/{oid}/ai/{mid}', {}).status_code == 303
    job = worker(client, '/api/ai/jobs/claim').json()['job']
    assert worker(client, f"/api/ai/jobs/{job['id']}/result", {'lease': job['lease'], 'error': '使えるAIがありません'}).status_code == 200
    assert '使えるAIがありません' in client.get(f'/entries/observation/{oid}').text
    other = observation(client, '別の記録')
    assert post(client, f'/observations/{other}/ai/{mid}', {}).status_code == 404
    with transaction() as db:
        db.get(Media, mid).technical_info = None
    assert post(client, f'/observations/{oid}/ai/{mid}', {}).status_code == 409


def test_stale_running_job_is_reclaimed_then_fails_after_attempts(client, video_bytes):
    oid, mid = video(client, video_bytes)
    post(client, f'/observations/{oid}/ai/{mid}', {})
    first = worker(client, '/api/ai/jobs/claim').json()['job']
    for attempt in range(ai_jobs.MAX_ATTEMPTS):
        with transaction() as db:
            db.get(AiJob, first['id']).claimed_at = now() - timedelta(minutes=ai_jobs.LEASE_MINUTES + 1)
        again = worker(client, '/api/ai/jobs/claim').json()['job']
        if attempt < ai_jobs.MAX_ATTEMPTS - 1:
            assert again['id'] == first['id'] and again['lease'] != first['lease']
            assert worker(client, f"/api/ai/jobs/{first['id']}/result",
                          {'lease': first['lease'], 'error': '古い'}).status_code == 409
            first = again
        else:
            assert again is None
    with transaction() as db:
        assert db.get(AiJob, first['id']).status == 'failed'


def test_auto_queue_on_upload_respects_setting_and_daily_limit(client, video_bytes, monkeypatch):
    oid = observation(client)
    mid, _ = upload(client, oid, video_bytes, 'off.mp4')
    with transaction() as db:
        assert db.scalar(select(AiJob).where(AiJob.media_id == mid)) is None
    monkeypatch.setattr(settings, 'ai_auto_analyze', True)
    monkeypatch.setattr(settings, 'ai_daily_limit', 1)
    first, _ = upload(client, oid, video_bytes, 'auto.mp4')
    second, _ = upload(client, oid, video_bytes, 'over.mp4')
    with transaction() as db:
        job = db.scalar(select(AiJob).where(AiJob.media_id == first))
        assert job.trigger == 'auto' and job.status == 'queued'
        assert db.scalar(select(AiJob).where(AiJob.media_id == second)) is None


def test_deleted_video_drops_drafts(client, video_bytes):
    from app.media import run_jobs
    oid, mid = video(client, video_bytes)
    post(client, f'/observations/{oid}/ai/{mid}', {})
    with transaction() as db:
        row = db.get(Media, mid)
        row.status, row.deletion_reason = 'delete_pending', 'expired'
    run_jobs()
    with transaction() as db:
        assert db.scalar(select(AiJob).where(AiJob.media_id == mid)) is None
