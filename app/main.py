"""HTTPの入口。フォームを検証し、保存サービスへ渡して画面を返す。"""
import asyncio
import logging
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
import anyio
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select, func, delete, text
from sqlalchemy.exc import IntegrityError, OperationalError
from starlette.background import BackgroundTask
from .config import settings
from .db import transaction
from .models import Cat, Tag, Observation, Health, Comment, Interaction, DictionaryItem, Evidence, Media, now
from .schemas import ObservationInput, HealthInput, DictionaryInput, InteractionInput, EvidenceInput, ReserveInput, JST
from .services import parent, save_entry, save_dictionary, save_interaction, save_evidence, LABELS, ASSESSMENTS, CONDITIONS
from .media import reserve, receive_upload, paths, request_delete, run_jobs, used_bytes
from .exports import health_csv, export_zip
from .security import SecurityMiddleware

log = logging.getLogger('meme')

def initial_data():
    settings.media_root.mkdir(parents=True, exist_ok=True)
    (settings.media_root / 'staging').mkdir(exist_ok=True)
    with transaction(exclusive=True) as db:
        if not db.get(Cat, 1): db.add(Cat(id=1, name='メメ', notes=''))
        if not db.scalar(select(Tag.id).limit(1)):
            db.add_all(Tag(name=name) for name in ['食事前','ドアの前','遊び','帰宅','触れている','その他'])

def safe_jobs():
    try:
        run_jobs()
    except HTTPException as exc:
        if exc.status_code != 409: log.error('cleanup_http_failure')
    except Exception:
        log.error('cleanup_failed')

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
app.mount('/static', StaticFiles(directory='app/static'), name='static')
templates = Jinja2Templates(directory='app/templates')
templates.env.filters['localtime'] = lambda v: v.astimezone(JST).strftime('%Y/%m/%d %H:%M') if v else '不明'
templates.env.filters['inputtime'] = lambda v: v.astimezone(JST).strftime('%Y-%m-%dT%H:%M') if v else ''
templates.env.filters['size'] = lambda v: f'{(v or 0)/1_000_000:.1f} MB'

def render(request, template, **context):
    return templates.TemplateResponse(request=request, name=template, context={
        'csrf':request.state.csrf, 'labels':LABELS, 'assessments':ASSESSMENTS,
        'conditions':CONDITIONS, 'now':now(), 'timedelta':timedelta, 'settings':settings, **context})

@app.exception_handler(Exception)
async def unexpected_error(request, exc):
    log.error('request_failed')
    return await http_error(request, HTTPException(500, '処理に失敗しました。保存状態を確認して再試行してください。'))

@app.exception_handler(HTTPException)
async def http_error(request, exc):
    if request.url.path.startswith('/api/'):
        return JSONResponse({'detail':str(exc.detail)}, status_code=exc.status_code)
    response = render(request, 'error.html', message=str(exc.detail))
    response.status_code = exc.status_code
    return response

@app.exception_handler(IntegrityError)
async def integrity_error(request, exc):
    return await http_error(request, HTTPException(409, '関連データが変更されています。画面を更新して再試行してください。'))

@app.exception_handler(OperationalError)
async def operation_error(request, exc):
    log.warning('database_operation_failed')
    return await http_error(request, HTTPException(409, '処理中です。少し待って再試行してください。'))

@app.get('/healthz')
def healthz():
    with transaction() as db: db.execute(text('SELECT 1'))
    return {'status':'ok'}

@app.get('/')
def home(request: Request):
    with transaction() as db:
        return render(request, 'home.html', cat=db.get(Cat,1),
            observations=list(db.scalars(select(Observation).where(Observation.deletion_requested_at.is_(None)).order_by(Observation.occurred_at.desc()).limit(5))),
            health=list(db.scalars(select(Health).where(Health.deletion_requested_at.is_(None)).order_by(Health.occurred_at.desc()).limit(5))))

def filtered(db, model, q='', start='', end='', tag='', page=1, limit=30):
    query = select(model).where(model.deletion_requested_at.is_(None))
    try:
        if start: query = query.where(model.occurred_at >= datetime.fromisoformat(start).replace(tzinfo=JST))
        if end: query = query.where(model.occurred_at < datetime.fromisoformat(end).replace(tzinfo=JST)+timedelta(days=1))
        if page < 1 or page > 100000: raise ValueError()
        if tag and model is Observation:
            from .models import ObservationTag
            query = query.where(model.id.in_(select(ObservationTag.observation_id).where(ObservationTag.tag_id == int(tag))))
    except ValueError:
        raise HTTPException(422, '日付・ページ・タグを確認してください。')
    if len(q) > 200: raise HTTPException(422, '検索語が長すぎます。')
    if q:
        column = model.description if model is Observation else model.details
        query = query.where(column.contains(q, autoescape=True))
    return list(db.scalars(query.order_by(model.occurred_at.desc(), model.id.desc()).offset((page-1)*limit).limit(limit)))

@app.get('/observations')
@app.get('/health')
def listing(request: Request, q: str='', start: str='', end: str='', tag: str='', page: int=1):
    kind = 'health' if request.url.path == '/health' else 'observation'
    with transaction() as db:
        return render(request, 'list.html', kind=kind, rows=filtered(db, Health if kind=='health' else Observation,q,start,end,tag,page),
            tags=list(db.scalars(select(Tag))), q=q,start=start,end=end,tag=tag,page=page)

@app.get('/entries/{kind}/new')
@app.get('/entries/{kind}/{id}/edit')
def entry_form(request: Request, kind: str, id: int | None=None):
    if kind not in {'observation','health'}: raise HTTPException(404)
    with transaction() as db:
        row = parent(db,kind,id) if id else None
        from .models import ObservationTag
        selected = list(db.scalars(select(ObservationTag.tag_id).where(ObservationTag.observation_id==id))) if row and kind=='observation' else []
        values = {c.name:getattr(row,c.name) for c in row.__table__.columns} if row else {}
        values.update(tags=selected, submission_key=str(uuid.uuid4()))
        return render(request,'form.html',kind=kind,id=id,values=values,errors=[],tags=list(db.scalars(select(Tag))))

@app.get('/entries/{kind}/{id}')
def detail(request: Request, kind: str, id: int):
    with transaction() as db:
        row=parent(db,kind,id)
        media=list(db.scalars(select(Media).where((Media.observation_id==id) if kind=='observation' else (Media.health_entry_id==id)).order_by(Media.id)))
        context={}
        if kind=='observation':
            context=dict(comments=list(db.scalars(select(Comment).where(Comment.observation_id==id).order_by(Comment.created_at))),
                interactions=list(db.scalars(select(Interaction).where(Interaction.observation_id==id).order_by(Interaction.id))),
                evidence=list(db.execute(select(Evidence,DictionaryItem).join(DictionaryItem).where(Evidence.observation_id==id))),
                items=list(db.scalars(select(DictionaryItem).order_by(DictionaryItem.title))))
        return render(request,'detail.html',kind=kind,row=row,media=media,**context)

@app.get('/dictionary')
def dictionary(request: Request):
    with transaction() as db:
        return render(request,'dictionary.html',items=list(db.scalars(select(DictionaryItem).order_by(DictionaryItem.updated_at.desc()))), edit=None)

@app.get('/dictionary/{id}')
def dictionary_detail(request: Request, id:int):
    with transaction() as db:
        item=db.get(DictionaryItem,id)
        if not item: raise HTTPException(404)
        evidence=list(db.execute(select(Evidence,Observation).join(Observation).where(Evidence.dictionary_item_id==id,Observation.deletion_requested_at.is_(None))))
        uses=list(db.execute(select(Interaction,Observation).join(Observation).where(Interaction.cue_item_id==id,Observation.deletion_requested_at.is_(None))))
        return render(request,'dictionary.html',items=[],edit=item,evidence=evidence,uses=uses)

@app.get('/storage')
def storage(request: Request):
    with transaction() as db:
        return render(request,'storage.html',cat=db.get(Cat,1),used=used_bytes(db),
            media=list(db.scalars(select(Media).where(Media.status.not_in(['deleted'])).order_by(Media.expires_at.asc().nullsfirst()).limit(200))),
            deleting=sum(db.scalar(select(func.count()).select_from(model).where(model.deletion_requested_at.is_not(None))) for model in (Observation,Health)))

@app.get('/health.csv')
def csv_download(start: str='',end:str=''):
    with transaction() as db:
        rows=filtered(db,Health,start=start,end=end,limit=100001)
        if len(rows)>100000: raise HTTPException(413,'期間を絞ってください。')
        return Response(health_csv(rows,LABELS),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="meme-health.csv"'})

@app.get('/health/print')
def print_health(request:Request,start:str='',end:str=''):
    with transaction() as db:
        rows=filtered(db,Health,start=start,end=end,limit=1001)
        if len(rows)>1000: raise HTTPException(413,'期間を絞ってください。')
        return render(request,'print.html',rows=rows)

async def read_form(request):
    form = await request.form(max_files=0,max_fields=40,max_part_size=65536)
    supplied = str(form.get('csrf',''))
    if not secrets.compare_digest(supplied, request.state.csrf):
        raise HTTPException(403,'送信を確認できません。ページを開き直してください。')
    return {key:form.getlist(key) if key=='tags' else value for key,value in form.items() if key!='csrf'}

@app.post('/entries/{kind}/save')
@app.post('/entries/{kind}/{id}/save')
async def entry_save(request: Request, kind:str,id:int|None=None):
    if kind not in {'observation','health'}: raise HTTPException(404)
    raw=await read_form(request)
    def process():
        try: data=(ObservationInput if kind=='observation' else HealthInput).model_validate(raw)
        except ValidationError as error:
            with transaction() as db:
                response=render(request,'form.html',kind=kind,id=id,values=raw,tags=list(db.scalars(select(Tag))),errors=[f"{'.'.join(map(str,e['loc']))}: {e['msg']}" for e in error.errors()])
                response.status_code=422
                return response
        with transaction() as db:
            row=save_entry(db,kind,data,id)
            return RedirectResponse(f'/entries/{kind}/{row.id}',303)
    return await anyio.to_thread.run_sync(process)

@app.post('/entries/{kind}/{id}/delete')
async def entry_delete(request:Request,kind:str,id:int):
    raw=await read_form(request)
    if raw.get('confirm')!='削除': raise HTTPException(422,'「削除」と入力してください。')
    def process():
        with transaction() as db: request_delete(db,kind,id)
        safe_jobs()
        return RedirectResponse('/storage',303)
    return await anyio.to_thread.run_sync(process)

@app.post('/observations/{id}/{action}')
async def observation_action(request:Request,id:int,action:str):
    raw=await read_form(request)
    def process():
        with transaction() as db:
            parent(db,'observation',id,True)
            try:
                if action=='comment':
                    body=str(raw.get('body','')).strip()
                    if not 1<=len(body)<=5000: raise HTTPException(422,'追記は1〜5000文字で入力してください。')
                    db.add(Comment(observation_id=id,body=body))
                elif action=='interaction':
                    edit_id=raw.pop('id','')
                    save_interaction(db,id,InteractionInput.model_validate(raw),int(edit_id) if edit_id else None)
                elif action=='evidence': save_evidence(db,id,EvidenceInput.model_validate(raw))
                elif action=='unlink':
                    db.execute(delete(Evidence).where(Evidence.observation_id==id,Evidence.dictionary_item_id==int(raw['dictionary_item_id'])))
                else: raise HTTPException(404)
            except (ValidationError,ValueError,KeyError): raise HTTPException(422,'入力内容と日時を確認してください。戻ると入力を修正できます。')
        return RedirectResponse(f'/entries/observation/{id}',303)
    return await anyio.to_thread.run_sync(process)

@app.post('/dictionary/save')
@app.post('/dictionary/{id}/save')
async def dictionary_save(request:Request,id:int|None=None):
    raw=await read_form(request)
    try: data=DictionaryInput.model_validate(raw)
    except ValidationError: raise HTTPException(422,'辞書の種類・タイトル・内容を確認してください。')
    def process():
        with transaction() as db: item=save_dictionary(db,data,id)
        return RedirectResponse(f'/dictionary/{item.id}',303)
    return await anyio.to_thread.run_sync(process)

@app.post('/dictionary/{id}/delete')
async def dictionary_delete(request:Request,id:int):
    raw=await read_form(request)
    if raw.get('confirm')!='削除': raise HTTPException(422,'「削除」と入力してください。')
    def process():
        with transaction() as db:
            row=db.get(DictionaryItem,id,with_for_update=True)
            if row: db.delete(row)
        return RedirectResponse('/dictionary',303)
    return await anyio.to_thread.run_sync(process)

@app.post('/profile')
async def profile(request:Request):
    raw=await read_form(request)
    name=str(raw.get('name','')).strip(); notes=str(raw.get('notes','')).strip()
    if not 1<=len(name)<=80 or len(notes)>3000: raise HTTPException(422,'名前またはメモの長さを確認してください。')
    try:
        birthday = datetime.fromisoformat(raw['birthday']).date() if raw.get('birthday') else None
        if birthday and birthday > now().astimezone(JST).date(): raise ValueError()
    except ValueError: raise HTTPException(422,'誕生日を確認してください。')
    def process():
        with transaction() as db:
            cat=db.get(Cat,1,with_for_update=True); cat.name=name; cat.notes=notes; cat.birthday=birthday
        return RedirectResponse('/storage',303)
    return await anyio.to_thread.run_sync(process)

@app.post('/api/media/reserve')
async def reserve_media(request:Request):
    try: data=ReserveInput.model_validate(await request.json())
    except (ValidationError,ValueError): raise HTTPException(422,'添付情報を確認してください。')
    def process():
        with transaction() as db:
            row=reserve(db,data)
            return {'id':row.id,'status':row.status}
    return await anyio.to_thread.run_sync(process)

@app.put('/api/media/{id}')
async def upload_media(request:Request,id:int):
    await receive_upload(request,id)
    return {'status':'ready'}

@app.get('/media/{id}')
def media_file(id:int):
    with transaction() as db:
        row=db.get(Media,id)
        if not row or row.status!='ready' or (not row.keep and row.expires_at<=now()): raise HTTPException(404)
        parent(db,'observation' if row.observation_id else 'health',row.observation_id or row.health_entry_id)
        path,_=paths(row.storage_key)
        if not path.is_file(): raise HTTPException(404,'添付が見つかりません。')
        return FileResponse(path,media_type=row.mime_type)

@app.post('/media/{id}/{action}')
async def media_action(request:Request,id:int,action:str):
    await read_form(request)
    def process():
        with transaction() as db:
            row=db.get(Media,id,with_for_update=True)
            if not row or row.status!='ready': raise HTTPException(409,'添付は処理中か削除済みです。')
            parent(db,'observation' if row.observation_id else 'health',row.observation_id or row.health_entry_id)
            if action=='keep':
                if not row.keep and row.expires_at<=now(): raise HTTPException(409,'保存期限を過ぎています。')
                row.keep=not row.keep
            elif action=='delete': row.status='delete_pending'; row.deletion_reason='user_deleted'
            else: raise HTTPException(404)
        return RedirectResponse('/storage',303)
    return await anyio.to_thread.run_sync(process)

@app.post('/export')
async def export(request:Request):
    raw=await read_form(request)
    if raw.get('confirm')!='出力': raise HTTPException(422,'「出力」と入力してください。')
    path=await anyio.to_thread.run_sync(export_zip)
    return FileResponse(path,filename='project-meme-export.zip',background=BackgroundTask(Path(path).unlink,missing_ok=True))

@app.post('/delete-all')
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
