"""画面共通: テンプレート、検索、一覧の絞り込み、フォーム検証、定期ジョブ。"""
import logging
from datetime import datetime, timedelta
from fastapi import HTTPException
from fastapi.templating import Jinja2Templates
from pydantic import TypeAdapter
from sqlalchemy import or_, select
from .media import run_jobs
from .models import Comment, Observation, now
from .schemas import JST, RecordId, parse_time
from .security import token_matches
from .acoustics import CONTOURS
from .voice_match import sparkline
from .services import ASSESSMENTS, BEHAVIORS, CONDITIONS, GUESS_PRESETS, LABELS, LOCATIONS, OUTCOMES
from .config import settings
from .profile_photo import photo_path

log = logging.getLogger('meme')
id_validator = TypeAdapter(RecordId)
templates = Jinja2Templates(directory='app/templates')
templates.env.filters['localtime'] = lambda v: v.astimezone(JST).strftime('%Y/%m/%d %H:%M') if v else '不明'
templates.env.filters['inputtime'] = lambda v: v.astimezone(JST).strftime('%Y-%m-%dT%H:%M') if v else ''
templates.env.filters['isotime'] = lambda v: datetime.fromisoformat(v).astimezone(JST).strftime('%Y/%m/%d %H:%M') if v else '不明'
templates.env.filters['size'] = lambda v: f'{(v or 0)/1_000_000:.1f} MB'

def render(request, template, **context):
    return templates.TemplateResponse(request=request, name=template, context={
        'has_profile_photo': photo_path().is_file(), 'csrf': request.state.csrf, 'labels': LABELS, 'assessments': ASSESSMENTS,
        'conditions': CONDITIONS, 'locations': LOCATIONS, 'guess_presets': GUESS_PRESETS, 'behaviors': BEHAVIORS, 'outcomes': OUTCOMES, 'contours': CONTOURS, 'sparkline': sparkline, 'now': now(), 'timedelta': timedelta, 'settings': settings, **context})

def safe_jobs():
    try:
        run_jobs()
    except HTTPException as exc:
        if exc.status_code != 409: log.error('cleanup_http_failure')
    except Exception:
        log.error('cleanup_failed')

async def read_form(request):
    form = await request.form(max_files=0, max_fields=40, max_part_size=131072)
    supplied = str(form.get('csrf', ''))
    if not token_matches(supplied, request.state.csrf):
        raise HTTPException(403, '送信を確認できません。ページを開き直してください。')
    return {key: form.getlist(key) if key in {'tags', 'guesses', 'behaviors', 'sound_ids'} else value for key, value in form.items() if key != 'csrf'}

def filtered(db, model, q='', start='', end='', tag='', page=1, limit=30, real_only=False):
    query = select(model).where(model.deletion_requested_at.is_(None))
    if real_only and model is Observation:
        query = query.where(Observation.cat_id == 1, Observation.is_sample.is_(False))
    try:
        if start: query = query.where(model.occurred_at >= parse_time(datetime.fromisoformat(start).replace(tzinfo=JST)))
        if end: query = query.where(model.occurred_at < parse_time(datetime.fromisoformat(end).replace(tzinfo=JST)+timedelta(days=1)))
        if page < 1 or page > 100000: raise ValueError()
        if tag and model is Observation:
            from .models import ObservationTag
            query = query.where(model.id.in_(select(ObservationTag.observation_id).where(ObservationTag.tag_id == id_validator.validate_python(tag))))
    except (ValueError, OverflowError):
        raise HTTPException(422, '日付・ページ・タグを確認してください。')
    if len(q) > 200: raise HTTPException(422, '検索語が長すぎます。')
    if q:
        if model is Observation:
            query = query.where(or_(model.description.contains(q, autoescape=True),
                model.id.in_(select(Comment.observation_id).where(Comment.body.contains(q, autoescape=True)))))
        else:
            query = query.where(model.details.contains(q, autoescape=True))
    return list(db.scalars(query.order_by(model.occurred_at.desc(), model.id.desc()).offset((page-1)*limit).limit(limit)))
