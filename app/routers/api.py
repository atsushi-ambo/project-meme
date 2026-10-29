"""ローカルCLI(Codex等)向けJSON API。画面と同じ検証・保存処理を使う。"""
import anyio
from fastapi import APIRouter, HTTPException, Path as RoutePath, Request
from pydantic import ValidationError
from sqlalchemy import select
from ..analysis import build_comparison
from ..db import transaction
from ..media import set_keep
from ..models import Media, SoundAnalysis
from ..reviews import build_weekly_review
from ..schemas import (AppendInput, GuessesInput, HealthInput, MediaKeepInput,
                       ObservationInput, RecordId, SoundCreateInput)
from ..services import add_guesses, append_observation, parent, save_entry
from ..sound_analysis import create as create_sound, media_loudness, update_labels
from ..voices import build_voice_report, sound_summary

router = APIRouter()

@router.get('/api/reviews/week')
def weekly_review_api(end: str = ''):
    with transaction(snapshot=True) as db:
        return build_weekly_review(db, end)

@router.get('/api/analysis/voices')
def voices_api():
    with transaction(snapshot=True) as db:
        return build_voice_report(db)

@router.get('/api/analysis/observations')
def comparison_api(ids: str = ''):
    with transaction(snapshot=True) as db:
        return build_comparison(db, ids)

@router.post('/api/entries/{kind}')
async def create_entry_api(request: Request, kind: str):
    """Codex用の入口も画面と同じ検証・トランザクション・再送制御を使う。"""
    if kind not in {'observation', 'health'}:
        raise HTTPException(404, '記録の種類が不正です。')
    try:
        data = (ObservationInput if kind == 'observation' else HealthInput).model_validate(await request.json())
    except (ValidationError, ValueError):
        raise HTTPException(422, '本文・日時・項目を確認してください。')
    def process():
        with transaction() as db:
            row = save_entry(db, kind, data)
            return {'id': row.id, 'url': f'/entries/{kind}/{row.id}'}
    return await anyio.to_thread.run_sync(process)

@router.get('/api/observations/{id}')
def observation_api(id: RecordId = RoutePath(ge=1, le=9223372036854775807)):
    # 一時点の本文と版番号。内部の保存キー・ローカルパスは返さない。
    with transaction(snapshot=True) as db:
        row = parent(db, 'observation', id)
        media = list(db.scalars(select(Media).where(Media.observation_id == id,
            Media.status == 'ready').order_by(Media.id).limit(10)))
        return {'id': row.id, 'url': f'/entries/observation/{row.id}',
                'description': row.description, 'revision': row.revision,
                'occurred_at': row.occurred_at, 'location': row.location,
                'media': [{'id': item.id, 'original_name': item.original_name, 'keep': item.keep,
                           'expires_at': item.expires_at} for item in media]}

@router.post('/api/observations/{id}/append')
async def append_api(request: Request, id: RecordId = RoutePath(ge=1, le=9223372036854775807)):
    try:
        data = AppendInput.model_validate(await request.json())
    except (ValidationError, ValueError):
        raise HTTPException(422, '追記の本文・送信キー・確認した版番号を確認してください。')
    def process():
        with transaction() as db:
            row, replayed = append_observation(db, id, data)
            return {'id': row.id, 'url': f'/entries/observation/{row.id}',
                    'revision': row.revision, 'replayed': replayed}
    return await anyio.to_thread.run_sync(process)

@router.post('/api/media/{id}/keep')
async def keep_api(request: Request, id: RecordId = RoutePath(ge=1, le=9223372036854775807)):
    try:
        data = MediaKeepInput.model_validate(await request.json())
    except (ValidationError, ValueError):
        raise HTTPException(422, 'keepにはtrueまたはfalseを指定してください。')
    def process():
        with transaction() as db:
            row = set_keep(db, id, data.keep)
            return {'id': row.id, 'keep': row.keep}
    return await anyio.to_thread.run_sync(process)

@router.post('/api/observations/{id}/guesses')
async def guesses_api(request: Request, id: RecordId):
    try:
        data = GuessesInput.model_validate(await request.json())
    except (ValidationError, ValueError):
        raise HTTPException(422, '追加する予想を確認してください。')
    def process():
        with transaction() as db:
            add_guesses(db, id, data.guesses)
        return {'status': 'saved', 'url': f'/entries/observation/{id}'}
    return await anyio.to_thread.run_sync(process)


async def json_object(request):
    try:
        raw = await request.json()
    except ValueError:
        raise HTTPException(422, 'JSONを確認してください。')
    if not isinstance(raw, dict):
        raise HTTPException(422, 'JSONオブジェクトで指定してください。')
    return raw

@router.post('/api/observations/{id}/media/{media_id}/loudness')
async def loudness_api(request: Request, id: RecordId, media_id: RecordId):
    """区間選びの補助。POSTだけで解析し、結果は保存しない。"""
    await json_object(request)
    def process():
        with transaction() as db:
            return media_loudness(db, id, media_id)
    return await anyio.to_thread.run_sync(process)

@router.post('/api/observations/{id}/sounds')
async def sound_create_api(request: Request, id: RecordId):
    """飼い主が確認した区間と分類をローカルCLIから保存する。同じ区間の再送は409。"""
    raw = await json_object(request)
    try:
        data = SoundCreateInput.model_validate(raw)
    except ValidationError:
        raise HTTPException(422, '添付ID・開始・終了・分類の項目を確認してください。')
    def process():
        with transaction() as db:
            result = create_sound(db, id, data.media_id, data.model_dump())
            db.flush()
            return sound_summary(db, result)
    return await anyio.to_thread.run_sync(process)

@router.get('/api/observations/{id}/sounds/{result_id}')
def sound_api(id: RecordId, result_id: RecordId):
    # 分類と版番号を同じ時点で読み、別の記録の区間は返さない。
    with transaction(snapshot=True) as db:
        parent(db, 'observation', id)
        result = db.scalar(select(SoundAnalysis).join(Media).where(
            SoundAnalysis.id == result_id, Media.observation_id == id))
        if not result:
            raise HTTPException(404, '声の区間が見つかりません。')
        return sound_summary(db, result)


@router.post('/api/observations/{id}/sounds/{result_id}/labels')
async def sound_labels_api(request: Request, id: RecordId, result_id: RecordId):
    raw = await json_object(request)
    def process():
        with transaction() as db:
            result = update_labels(db, id, result_id, raw, merge=True)
            db.flush()
            return sound_summary(db, result)
    return await anyio.to_thread.run_sync(process)
