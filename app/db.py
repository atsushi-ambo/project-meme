"""一操作をトランザクションでまとめる。ロックはバックアップ中の更新を防ぐ。"""
from contextlib import contextmanager
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from fastapi import HTTPException
from .config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True, pool_size=5, max_overflow=3, hide_parameters=True)
MAINTENANCE_LOCK = 741902

@contextmanager
def transaction(exclusive=False, snapshot=False):
    with Session(engine, expire_on_commit=False) as db, db.begin():
        if snapshot:
            # 集計と原文が同じ時点を参照し、読み取り経路からの更新も拒否する。
            db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
        fn = 'pg_try_advisory_xact_lock' if exclusive else 'pg_try_advisory_xact_lock_shared'
        if not db.scalar(text(f'SELECT {fn}(:key)'), {'key': MAINTENANCE_LOCK}):
            raise HTTPException(409, '保存・整理処理中です。少し待って再試行してください。')
        yield db
