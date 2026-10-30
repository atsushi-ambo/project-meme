"""実記録を日付ごとに並べる、読み取り専用の一覧。"""
from datetime import date, datetime, time, timedelta, timezone
import re

from fastapi import HTTPException
from sqlalchemy import func, literal, select, union_all

from .models import Health, Media, Observation, SoundAnalysis, now
from .schemas import JST

PAGE_SIZE = 30
SOUND_LIMIT = 3
MIN_DAY = date(1, 1, 2)
MAX_DAY = date(9999, 12, 30)


def day_range(value):
    if not value:
        chosen = now().astimezone(JST).date()
    else:
        if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
            raise HTTPException(422, '日付はYYYY-MM-DD形式で指定してください。')
        try:
            chosen = date.fromisoformat(value)
        except ValueError:
            raise HTTPException(422, '日付を確認してください。') from None
    if not MIN_DAY <= chosen <= MAX_DAY:
        raise HTTPException(422, '日付を確認してください。')
    begin = datetime.combine(chosen, time.min, JST).astimezone(timezone.utc)
    return chosen, begin, begin + timedelta(days=1)


def day_timeline(db, day='', page=1):
    date, begin, finish = day_range(day)
    if page < 1 or page > 100000:
        raise HTTPException(422, 'ページを確認してください。')
    def records(model, kind):
        return select(model.id.label('id'), model.occurred_at.label('occurred_at'),
                      literal(kind).label('kind')).where(
            model.cat_id == 1, model.is_sample.is_(False),
            model.deletion_requested_at.is_(None),
            model.occurred_at >= begin, model.occurred_at < finish)
    combined = union_all(records(Observation, 'observation'), records(Health, 'health')).subquery()
    keys = list(db.execute(select(combined).order_by(combined.c.occurred_at,
        combined.c.kind, combined.c.id).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE + 1)))
    has_next = len(keys) > PAGE_SIZE
    keys = keys[:PAGE_SIZE]
    ids = {kind: [row.id for row in keys if row.kind == kind] for kind in ('observation', 'health')}
    observations = {r.id: r for r in db.scalars(select(Observation).where(Observation.id.in_(ids['observation'])))}
    health = {r.id: r for r in db.scalars(select(Health).where(Health.id.in_(ids['health'])))}
    sounds = {id: [] for id in ids['observation']}
    totals = {}
    if sounds:
        numbered = select(Media.observation_id.label('oid'), Media.original_name.label('name'),
            Media.status.label('status'), SoundAnalysis.id.label('id'),
            SoundAnalysis.start_ms.label('start_ms'), SoundAnalysis.end_ms.label('end_ms'),
            func.row_number().over(partition_by=Media.observation_id,
                order_by=(Media.id, SoundAnalysis.start_ms, SoundAnalysis.id)).label('n'),
            func.count().over(partition_by=Media.observation_id).label('total')
        ).join(Media, Media.id == SoundAnalysis.media_id).where(Media.observation_id.in_(sounds)).subquery()
        for item in db.execute(select(numbered).where(numbered.c.n <= SOUND_LIMIT)
                               .order_by(numbered.c.oid, numbered.c.n)):
            sounds[item.oid].append(item)
            totals[item.oid] = item.total
    rows = [{'kind': key.kind, 'entry': (observations if key.kind == 'observation' else health)[key.id],
             'sounds': sounds.get(key.id, []) if key.kind == 'observation' else [],
             'sound_total': totals.get(key.id, 0) if key.kind == 'observation' else 0} for key in keys]
    return {'day': date.isoformat(), 'previous': (date - timedelta(days=1)).isoformat() if date > MIN_DAY else None,
            'next': (date + timedelta(days=1)).isoformat() if date < MAX_DAY else None, 'page': page, 'has_next': has_next,
            'rows': rows}
