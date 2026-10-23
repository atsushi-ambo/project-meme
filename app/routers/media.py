"""添付メディアとプロフィール写真のルート。"""
import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from pydantic import ValidationError
from ..db import transaction
from ..media import inspect_saved, paths, receive_upload, reserve, set_keep
from ..analysis import technical_summary
from ..models import Media, now
from ..profile_photo import MAX_PHOTO_BYTES, icon_bytes, photo_path, save_photo
from ..schemas import Input, MediaTimeInput, RecordId, ReserveInput
from ..services import parent
from ..web import read_form

router = APIRouter()

@router.post('/api/media/reserve')
async def reserve_media(request:Request):
    try: data=ReserveInput.model_validate(await request.json())
    except (ValidationError,ValueError): raise HTTPException(422,'添付情報を確認してください。')
    def process():
        with transaction() as db:
            row=reserve(db,data)
            return {'id':row.id,'status':row.status}
    return await anyio.to_thread.run_sync(process)

@router.put('/api/media/{id}')
async def upload_media(request:Request,id:RecordId):
    await receive_upload(request,id)
    return {'status':'ready'}

@router.get('/media/{id}')
def media_file(id:RecordId):
    with transaction() as db:
        row=db.get(Media,id)
        if not row or row.status!='ready' or (not row.keep and row.expires_at<=now()): raise HTTPException(404)
        parent(db,'observation' if row.observation_id else 'health',row.observation_id or row.health_entry_id)
        path,_=paths(row.storage_key)
        if not path.is_file() or path.is_symlink(): raise HTTPException(404,'添付が見つかりません。')
        return FileResponse(path,media_type=row.mime_type)

@router.post('/media/{id}/captured-at')
async def media_capture_time(request: Request, id: RecordId):
    raw = await read_form(request)
    try:
        data = MediaTimeInput.model_validate(raw)
    except ValidationError:
        raise HTTPException(422, '撮影日時を正しく入力してください。')
    def process():
        with transaction() as db:
            row = db.get(Media, id, with_for_update=True)
            if not row or row.status != 'ready' or (not row.keep and row.expires_at <= now()):
                raise HTTPException(409, 'この添付の撮影日時は変更できません。')
            kind = 'observation' if row.observation_id else 'health'
            parent_id = row.observation_id or row.health_entry_id
            parent(db, kind, parent_id)
            row.captured_at = data.captured_at
            row.captured_at_source = 'user' if data.captured_at else 'unknown'
        return RedirectResponse(f'/entries/{kind}/{parent_id}', 303)
    return await anyio.to_thread.run_sync(process)

@router.post('/api/media/{id}/info')
async def media_info_api(request: Request, id: RecordId):
    try:
        Input.model_validate(await request.json())
    except (ValidationError, ValueError):
        raise HTTPException(422, '空のJSONオブジェクトを指定してください。')
    def process():
        with transaction() as db:
            row = inspect_saved(db, id)
            return {'id': row.id, 'technical_info': technical_summary(row.technical_info)}
    return await anyio.to_thread.run_sync(process)

@router.post('/media/{id}/info')
async def media_info_form(request: Request, id: RecordId):
    raw = await read_form(request)
    if raw:
        raise HTTPException(422, '送信項目を確認してください。')
    def process():
        with transaction() as db:
            row = inspect_saved(db, id)
            kind = 'observation' if row.observation_id else 'health'
            entry_id = row.observation_id or row.health_entry_id
        return RedirectResponse(f'/entries/{kind}/{entry_id}', 303)
    return await anyio.to_thread.run_sync(process)

@router.post('/media/{id}/{action}')
async def media_action(request:Request,id:RecordId,action:str):
    await read_form(request)
    def process():
        with transaction() as db:
            if action == 'keep':
                set_keep(db, id)
            elif action == 'delete':
                row = db.get(Media, id, with_for_update=True)
                if not row or row.status != 'ready':
                    raise HTTPException(409, '添付は処理中か削除済みです。')
                parent(db, 'observation' if row.observation_id else 'health', row.observation_id or row.health_entry_id)
                row.status = 'delete_pending'
                row.deletion_reason = 'user_deleted'
            else:
                raise HTTPException(404)
        return RedirectResponse('/storage',303)
    return await anyio.to_thread.run_sync(process)

@router.get('/profile-photo')
def profile_photo():
    if not photo_path().is_file(): raise HTTPException(404)
    return FileResponse(photo_path(), media_type='image/jpeg')

@router.get('/profile-icon')
def profile_icon():
    with transaction():
        if not photo_path().is_file(): raise HTTPException(404)
        return Response(icon_bytes(), media_type='image/png')

@router.put('/api/profile-photo')
async def upload_profile_photo(request: Request):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_PHOTO_BYTES:
            raise HTTPException(413, '写真は10MB以内で選んでください。')
    def process():
        with transaction(exclusive=True):
            save_photo(data)
    await anyio.to_thread.run_sync(process)
    return {'status': 'ready'}

@router.post('/profile-photo/delete')
async def delete_profile_photo(request: Request):
    await read_form(request)
    def process():
        with transaction(exclusive=True):
            photo_path().unlink(missing_ok=True)
    await anyio.to_thread.run_sync(process)
    return RedirectResponse('/storage', 303)

