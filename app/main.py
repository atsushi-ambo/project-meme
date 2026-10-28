"""HTTPの入口。アプリ生成・共通処理・ルーター登録だけを担う。

各ルートは app/routers/ に分けた:
- pages: 画面表示(GET)
- entries: フォーム送信の保存・削除
- api: ローカルCLI向けJSON API
- media: 添付とプロフィール写真
- admin: 全データ出力・全削除
画面共通の描画・検索・フォーム検証は app/web.py、リクエスト単位の
構造化ログとリクエストIDは app/observability.py に置く。
"""
import asyncio
import logging
from contextlib import asynccontextmanager
import anyio
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from .config import settings
from .db import transaction
from .models import CallType, Cat, Tag
from .observability import RequestContextMiddleware
from .routers import admin, api, entries, media, pages
from .security import SecurityMiddleware
from .services import CALL_TYPE_PRESETS
from .web import render, safe_jobs

log = logging.getLogger('meme')

def initial_data():
    settings.media_root.mkdir(parents=True, exist_ok=True)
    (settings.media_root / 'staging').mkdir(exist_ok=True)
    with transaction(exclusive=True) as db:
        if not db.get(Cat, 1): db.add(Cat(id=1, name='メメ', notes=''))
        if not db.scalar(select(Tag.id).limit(1)):
            db.add_all(Tag(name=name) for name in ['食事前','ドアの前','遊び','帰宅','触れている','その他'])
        if not db.scalar(select(CallType.id).limit(1)):
            db.add_all(CallType(name=name, description=description) for name, description in CALL_TYPE_PRESETS)

@asynccontextmanager
async def lifespan(app):
    await anyio.to_thread.run_sync(initial_data)
    await anyio.to_thread.run_sync(safe_jobs)
    async def loop():
        while True:
            await asyncio.sleep(settings.job_interval)
            await anyio.to_thread.run_sync(safe_jobs)
    task = asyncio.create_task(loop())
    yield
    task.cancel()
    try: await task
    except asyncio.CancelledError: pass

app = FastAPI(title='プロジェクト・メメ', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(SecurityMiddleware)
app.add_middleware(RequestContextMiddleware)
app.mount('/static', StaticFiles(directory='app/static'), name='static')

@app.exception_handler(Exception)
async def unexpected_error(request, exc):
    log.error('request_failed request_id=%s', getattr(request.state, 'request_id', '-'))
    return await http_error(request, HTTPException(500, '処理に失敗しました。保存状態を確認して再試行してください。'))

@app.exception_handler(HTTPException)
async def http_error(request, exc):
    if request.url.path.startswith('/api/'):
        return JSONResponse({'detail': str(exc.detail)}, status_code=exc.status_code)
    response = render(request, 'error.html', message=str(exc.detail))
    response.status_code = exc.status_code
    return response

@app.exception_handler(IntegrityError)
async def integrity_error(request, exc):
    return await http_error(request, HTTPException(409, '関連データが変更されています。画面を更新して再試行してください。'))

@app.exception_handler(OperationalError)
async def operation_error(request, exc):
    log.warning('database_operation_failed request_id=%s', getattr(request.state, 'request_id', '-'))
    return await http_error(request, HTTPException(409, '処理中です。少し待って再試行してください。'))

@app.get('/healthz')
def healthz():
    with transaction() as db: db.execute(text('SELECT 1'))
    return {'status': 'ok'}

app.include_router(pages.router)
app.include_router(entries.router)
app.include_router(api.router)
app.include_router(media.router)
app.include_router(admin.router)
