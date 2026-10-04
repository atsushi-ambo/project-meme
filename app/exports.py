import csv
import io
import json
import tempfile
import shutil
import zipfile
from datetime import datetime
from decimal import Decimal
from sqlalchemy import select
from fastapi import HTTPException
from .db import transaction
from .models import Base, Media
from .media import paths
from .profile_photo import photo_path

# Excel等がセルの内容を式として実行しないようにする。
def csv_cell(value):
    value = '' if value is None else str(value)
    if value.lstrip().startswith(('=','+','-','@')) or value.startswith(('\t','\r','\n')):
        return "'" + value
    return value

def health_csv(rows, labels):
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(['日時（日本時間）','種類','回数','体重','単位','食事からの分数','食欲','元気さ','メモ'])
    from .schemas import JST
    for row in rows:
        writer.writerow([csv_cell(v) for v in [row.occurred_at.astimezone(JST).isoformat(), labels[row.event_type], row.count, row.weight_value, row.weight_unit, row.meal_interval_minutes, row.appetite, row.energy, row.details]])
    return '\ufeff' + output.getvalue()

def json_default(value):
    if isinstance(value, datetime): return value.isoformat()
    if isinstance(value, Decimal): return str(value)
    if hasattr(value, 'isoformat'): return value.isoformat()
    raise TypeError()

def export_zip(destination=None):
    # 保守ロックを取得できない場合は待たずに再試行を案内する。
    with transaction(exclusive=True) as db:
        size = sum(row.reserved_bytes for row in db.scalars(select(Media).where(Media.status == 'ready')))
        if shutil.disk_usage(destination or tempfile.gettempdir()).free < size + 512_000_000:
            raise HTTPException(413, '出力用の空き容量が不足しています。')
        handle = tempfile.NamedTemporaryFile(suffix='.zip', delete=False, dir=destination)
        path = handle.name
        handle.close()
        try:
            with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_STORED) as archive:
                data = {'format_version':1, 'tables':{}}
                for table in Base.metadata.sorted_tables:
                    data['tables'][table.name] = [dict(r) for r in db.execute(select(table)).mappings()]
                if photo_path().is_file():
                    archive.write(photo_path(), 'media/profile.jpg')
                archive.writestr('records.json', json.dumps(data, ensure_ascii=False, default=json_default))
                for row in db.scalars(select(Media).where(Media.status == 'ready')):
                    file, _ = paths(row.storage_key)
                    if not file.is_file():
                        raise HTTPException(409, '添付が見つからないため出力を中止しました。')
                    archive.write(file, 'media/' + row.storage_key)
            return path
        except BaseException:
            from pathlib import Path
            Path(path).unlink(missing_ok=True)
            raise
