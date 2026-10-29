"""全データ出力と全削除。"""
import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from ..config import settings
from ..db import transaction
from ..exports import ExportResponse, export_zip
from ..models import Health, Media, Observation, now
from ..web import read_form, safe_jobs

router = APIRouter()

@router.post('/export')
async def export(request:Request):
    raw=await read_form(request)
    if raw.get('confirm')!='出力': raise HTTPException(422,'「出力」と入力してください。')
    path=await anyio.to_thread.run_sync(export_zip)
    return ExportResponse(path,filename='project-meme-export.zip')

@router.post('/delete-all')
async def delete_all(request:Request):
    raw=await read_form(request)
    if raw.get('confirm')!='メメの全記録を削除': raise HTTPException(422,'確認文を正確に入力してください。')
    def process():
        with transaction(exclusive=True) as db:
            # 設定マーカーをファイルへ永続化。再起動時も新規書き込みを止める。
            (settings.media_root/'deleting-all').write_text('pending')
            for model in (Observation,Health):
                for row in db.scalars(select(model)): row.deletion_requested_at=now()
            for row in db.scalars(select(Media)):
                if row.status!='deleted': row.status='delete_pending'; row.deletion_reason='all_deleted'
        safe_jobs()
        return RedirectResponse('/storage',303)
    return await anyio.to_thread.run_sync(process)
