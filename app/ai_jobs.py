"""動画の分析をホストのAIワーカーへ頼むキュー。

アプリ自身は外部へ送信しない。ホストで飼い主のログインを使うワーカー（scripts/ai_worker.py）が
ジョブを取りに来て、既存の分析資料（report.jsonと静止画。音声・動画の原本は除く）をAIへ渡し、
下書きの文章だけを返す。下書きは表示するだけで、記録・声の区間・予想・評価は変更しない。
"""
from datetime import timedelta
import hashlib
import hmac
import secrets
from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from .config import settings
from .models import AiJob, Media, now

MAX_SECONDS = 60
MAX_ATTEMPTS = 3
LEASE_MINUTES = 90
KEEP_PER_MEDIA = 3
RESULT_LIMIT = 20000


def worker_token():
    """ワーカー用の合言葉。ホストの.envのSECRET_KEYから同じ値を導出し、別に保存しない。"""
    return hmac.new(settings.secret_key.encode(), b'meme-ai-worker-v1', hashlib.sha256).hexdigest()


def require_worker(supplied):
    if not isinstance(supplied, str) or not supplied.isascii() or not secrets.compare_digest(supplied, worker_token()):
        raise HTTPException(403, 'AIワーカーを確認できません。')


def eligible(media):
    """分析資料を作れる動画か。prepare_analysis.pyの条件と合わせる。"""
    info = media.technical_info or {}
    duration = info.get('duration_seconds')
    return (media.observation_id is not None and media.status == 'ready'
            and (media.keep or media.expires_at > now())
            and str(media.mime_type or '').startswith('video/')
            and isinstance(info.get('video'), dict)
            and type(duration) in (int, float) and 0 < duration <= MAX_SECONDS)


def enqueue(db, media, trigger='manual'):
    """待機・実行中のジョブがあればそれを返す。自動は1日の件数上限まで。"""
    if not eligible(media):
        if trigger == 'auto':
            return None
        raise HTTPException(409, 'AIで分析できるのは、保存済みで基本情報を取得した60秒以内の動画です。')
    active = db.scalar(select(AiJob).where(AiJob.media_id == media.id, AiJob.status.in_(['queued', 'running'])))
    if active:
        return active
    if trigger == 'auto':
        recent = db.scalar(select(func.count()).select_from(AiJob).where(
            AiJob.trigger == 'auto', AiJob.created_at > now() - timedelta(days=1)))
        if recent >= settings.ai_daily_limit:
            return None
    try:
        with db.begin_nested():
            job = AiJob(media_id=media.id, status='queued', trigger=trigger, attempts=0, created_at=now())
            db.add(job)
    except IntegrityError:
        return db.scalar(select(AiJob).where(AiJob.media_id == media.id, AiJob.status.in_(['queued', 'running'])))
    # 古い結果は添付ごとに直近だけ残す。自動の件数上限を数えるため、1日以内のものは消さない。
    old = select(AiJob.id).where(AiJob.media_id == media.id, AiJob.status.in_(['done', 'failed']),
                                 AiJob.created_at <= now() - timedelta(days=1)) \
        .order_by(AiJob.created_at.desc()).offset(KEEP_PER_MEDIA - 1)
    db.execute(delete(AiJob).where(AiJob.id.in_(old)))
    return job


def claim(db):
    """一件を原子的に取り出す。期限切れの実行中は回収し、回数の上限で失敗にする。"""
    at = now()
    for job in db.scalars(select(AiJob).where(AiJob.status == 'running',
            AiJob.claimed_at < at - timedelta(minutes=LEASE_MINUTES)).with_for_update(skip_locked=True)):
        job.lease = None
        if job.attempts >= MAX_ATTEMPTS:
            job.status, job.error, job.finished_at = 'failed', '時間内に終わりませんでした。', at
        else:
            job.status = 'queued'
    while True:
        job = db.scalar(select(AiJob).where(AiJob.status == 'queued').order_by(AiJob.created_at, AiJob.id)
                        .limit(1).with_for_update(skip_locked=True))
        if job is None:
            return None
        media = db.get(Media, job.media_id)
        if media is None or not eligible(media):
            job.status, job.error, job.finished_at = 'failed', '動画を利用できません。', at
            continue
        job.status, job.lease, job.claimed_at = 'running', secrets.token_hex(32), at
        job.attempts += 1
        return {'id': job.id, 'lease': job.lease, 'observation_id': media.observation_id, 'media_id': media.id}


def finish(db, id, data):
    """claimしたワーカーの結果だけを受け付ける。回収後に遅れて届いた結果は拒否する。"""
    job = db.get(AiJob, id, with_for_update=True)
    lease = data.get('lease')
    if (job is None or job.status != 'running' or not job.lease or not isinstance(lease, str)
            or not lease.isascii() or not secrets.compare_digest(lease, job.lease)):
        raise HTTPException(409, 'このジョブの結果は受け付けられません。')
    job.lease, job.finished_at = None, now()
    if data.get('result') is not None:
        result, tool, images = data.get('result'), data.get('tool'), data.get('images_sent')
        if (not isinstance(result, str) or not result.strip() or len(result) > RESULT_LIMIT
                or not isinstance(tool, str) or not 1 <= len(tool) <= 40
                or type(images) is not int or not 0 <= images <= 100):
            raise HTTPException(422, '結果・ツール名・画像枚数を確認してください。')
        job.status, job.result, job.tool, job.images_sent, job.error = 'done', result.replace('\x00', '').strip(), tool, images, None
    else:
        error = data.get('error')
        if not isinstance(error, str) or not error.strip():
            raise HTTPException(422, '失敗の理由を指定してください。')
        job.status, job.error = 'failed', error.replace('\x00', '').strip()[:500]
        tool = data.get('tool')
        job.tool = tool[:40] if isinstance(tool, str) and tool else None
    return job


def latest_by_media(db, media_ids):
    rows = {}
    if media_ids:
        for job in db.scalars(select(AiJob).where(AiJob.media_id.in_(media_ids)).order_by(AiJob.created_at, AiJob.id)):
            rows[job.media_id] = job
    return rows


def forget_unavailable(db):
    """削除・期限切れの動画から作った下書きを残さない。"""
    gone = select(Media.id).where(Media.status.in_(['delete_pending', 'deleted', 'failed']))
    db.execute(delete(AiJob).where(AiJob.media_id.in_(gone)))
