"""画面表示のGETルート。"""
import re
import uuid
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from ..analysis import build_comparison, technical_summary
from ..db import transaction
from ..exports import health_csv
from ..media import used_bytes
from ..models import Cat, Comment, DictionaryItem, Evidence, Health, Interaction, Media, Observation, Tag
from ..reviews import build_weekly_review
from ..schemas import RecordId
from ..services import LABELS, parent
from ..web import filtered, render

router = APIRouter()

@router.get('/')
def home(request: Request):
    with transaction() as db:
        return render(request, 'home.html', cat=db.get(Cat,1),
            observations=list(db.scalars(select(Observation).where(Observation.deletion_requested_at.is_(None)).order_by(Observation.occurred_at.desc()).limit(5))),
            health=list(db.scalars(select(Health).where(Health.deletion_requested_at.is_(None)).order_by(Health.occurred_at.desc()).limit(5))))

@router.get('/review/week')
def weekly_review(request: Request, end: str = ''):
    with transaction(snapshot=True) as db:
        return render(request, 'review.html', report=build_weekly_review(db, end))

@router.get('/compare')
def comparison(request: Request, ids: str = '', selected: list[RecordId] = Query(default=[]),
               q: str = '', start: str = '', end: str = '', page: int = 1):
    if ids and selected:
        raise HTTPException(422, '比較する記録の指定方法を一つにしてください。')
    if len(selected) > 5:
        raise HTTPException(422, '比較する記録は1〜5件を選んでください。')
    chosen = ids or ','.join(str(value) for value in selected)
    with transaction(snapshot=True) as db:
        if chosen:
            report = build_comparison(db, chosen)
            return render(request, 'compare.html', report=report,
                          ids=','.join(str(value) for value in report['selected_ids']))
        # サンプルと削除中を候補から除き、期間・キーワードで実記録を探せる。
        rows = filtered(db, Observation, q=q, start=start, end=end, page=page,
                        real_only=True)
        return render(request, 'compare.html', report=None, rows=rows,
                      q=q, start=start, end=end, page=page)

@router.get('/observations')
@router.get('/health')
def listing(request: Request, q: str='', start: str='', end: str='', tag: str='', page: int=1):
    kind = 'health' if request.url.path == '/health' else 'observation'
    with transaction() as db:
        return render(request, 'list.html', kind=kind, rows=filtered(db, Health if kind=='health' else Observation,q,start,end,tag,page),
            tags=list(db.scalars(select(Tag))), q=q,start=start,end=end,tag=tag,page=page)

@router.get('/entries/{kind}/new')
@router.get('/entries/{kind}/{id}/edit')
def entry_form(request: Request, kind: str, id: RecordId | None=None):
    if kind not in {'observation','health'}: raise HTTPException(404)
    with transaction() as db:
        row = parent(db,kind,id) if id else None
        from ..models import ObservationTag
        selected = list(db.scalars(select(ObservationTag.tag_id).where(ObservationTag.observation_id==id))) if row and kind=='observation' else []
        values = {c.name:getattr(row,c.name) for c in row.__table__.columns} if row else {}
        values.update(tags=selected, submission_key=str(uuid.uuid4()))
        if row and kind == 'observation': values['expected_revision'] = row.revision
        return render(request,'form.html',kind=kind,id=id,values=values,errors=[],tags=list(db.scalars(select(Tag))))

@router.get('/entries/{kind}/{id}')
def detail(request: Request, kind: str, id: RecordId):
    with transaction() as db:
        row=parent(db,kind,id)
        media=list(db.scalars(select(Media).where((Media.observation_id==id) if kind=='observation' else (Media.health_entry_id==id)).order_by(Media.id)))
        context={}
        if kind=='observation':
            context=dict(comments=list(db.scalars(select(Comment).where(Comment.observation_id==id).order_by(Comment.created_at))),
                interactions=list(db.scalars(select(Interaction).where(Interaction.observation_id==id).order_by(Interaction.id))),
                evidence=list(db.execute(select(Evidence,DictionaryItem).join(DictionaryItem).where(Evidence.observation_id==id))),
                items=list(db.scalars(select(DictionaryItem).order_by(DictionaryItem.title))))
        return render(request,'detail.html',kind=kind,row=row,media=media,
                      technical={m.id: technical_summary(m.technical_info) for m in media}, **context)

@router.get('/dictionary')
def dictionary(request: Request):
    with transaction() as db:
        return render(request,'dictionary.html',items=list(db.scalars(select(DictionaryItem).order_by(DictionaryItem.updated_at.desc()))), edit=None)

@router.get('/dictionary/{id}')
def dictionary_detail(request: Request, id:RecordId):
    with transaction() as db:
        item=db.get(DictionaryItem,id)
        if not item: raise HTTPException(404)
        evidence=list(db.execute(select(Evidence,Observation).join(Observation).where(Evidence.dictionary_item_id==id,Observation.deletion_requested_at.is_(None))))
        uses=list(db.execute(select(Interaction,Observation).join(Observation).where(Interaction.cue_item_id==id,Observation.deletion_requested_at.is_(None))))
        return render(request,'dictionary.html',items=[],edit=item,evidence=evidence,uses=uses)

@router.get('/storage')
def storage(request: Request, month: str = ''):
    if month and not re.fullmatch(r'[0-9]{4}-(0[1-9]|1[0-2])', month):
        raise HTTPException(422, '月はYYYY-MM形式で指定してください。')
    with transaction() as db:
        month_key = func.to_char(func.timezone('Asia/Tokyo',
            func.coalesce(Media.captured_at, Media.retention_started_at)), 'YYYY-MM')
        videos = (Media.status == 'ready', Media.mime_type.like('video/%'))
        months = db.execute(select(month_key.label('month'), func.count(Media.id).label('count'),
            func.coalesce(func.sum(Media.bytes), 0).label('bytes')).where(*videos)
            .group_by(month_key).order_by(month_key.desc())).all()
        query = select(Media).where(Media.status.not_in(['deleted']))
        if month:
            query = query.where(*videos, month_key == month)
        return render(request,'storage.html',cat=db.get(Cat,1),used=used_bytes(db),
            media=list(db.scalars(query.order_by(Media.expires_at.asc().nullsfirst()).limit(200))),
            video_months=months, selected_month=month,
            deleting=sum(db.scalar(select(func.count()).select_from(model).where(model.deletion_requested_at.is_not(None))) for model in (Observation,Health)))

@router.get('/health.csv')
def csv_download(start: str='',end:str=''):
    with transaction() as db:
        rows=filtered(db,Health,start=start,end=end,limit=100001)
        if len(rows)>100000: raise HTTPException(413,'期間を絞ってください。')
        return Response(health_csv(rows,LABELS),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="meme-health.csv"'})

@router.get('/health/print')
def print_health(request:Request,start:str='',end:str=''):
    with transaction() as db:
        rows=filtered(db,Health,start=start,end=end,limit=1001)
        if len(rows)>1000: raise HTTPException(413,'期間を絞ってください。')
        return render(request,'print.html',rows=rows)
