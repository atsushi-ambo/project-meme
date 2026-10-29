import csv
import fcntl
import io
import json
import os
import tempfile
import shutil
import zipfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from sqlalchemy import select
from fastapi import HTTPException
from fastapi.responses import FileResponse
from .config import settings
from .db import transaction
from .models import Base, Media
from .media import paths
from .profile_photo import photo_path


class ExportResponse(FileResponse):
    async def __call__(self, scope, receive, send):
        path = Path(self.path)
        try:
            # 遅いダウンロードも残存ファイルの清掃対象にしない。
            with path.open('rb') as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                await super().__call__(scope, receive, send)
        finally:
            # 切断・送信失敗・キャンセルでも個人データを残さない。
            path.unlink(missing_ok=True)


def cleanup_exports(at, *, all_ages=False):
    """生成処理と競合しないよう、呼び出し側の排他保守ロック内で清掃する。"""
    for path in (settings.media_root / '.exports').glob('export-*.zip'):
        if path.is_symlink():
            continue
        try:
            with path.open('rb') as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                if all_ages or os.fstat(handle.fileno()).st_mtime < at.timestamp() - 86400:
                    path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass  # 応答の終了が先に削除した。
    # 転送中のZIPが残る間は全削除を完了させない。
    return not any((settings.media_root / '.exports').glob('export-*.zip'))


# Excel等がセルの内容を式として実行しないようにする。
def csv_cell(value):
    value = '' if value is None else str(value)
    if value.lstrip().startswith(('=','+','-','@')) or value.startswith(('\t','\r','\n')):
        return "'" + value
    return value

def health_csv(rows, labels):
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(['日時（日本時間）','種類','回数','体重','単位','食事からの分数','食欲','元気さ','メモ','日時の精度','食べた量','便の様子'])
    from .schemas import JST
    for row in rows:
        precision = 'およそ' if row.time_is_approximate is True else '入力した時刻' if row.time_is_approximate is False else '不明'
        writer.writerow([csv_cell(v) for v in [row.occurred_at.astimezone(JST).isoformat(), labels[row.event_type], row.count, row.weight_value, row.weight_unit, row.meal_interval_minutes, row.appetite, row.energy, row.details, precision, row.food_amount, row.stool_condition]])
    return '\ufeff' + output.getvalue()

def json_default(value):
    if isinstance(value, datetime): return value.isoformat()
    if isinstance(value, Decimal): return str(value)
    if hasattr(value, 'isoformat'): return value.isoformat()
    raise TypeError()

def export_zip(destination=None):
    path = None
    try:
        # 保守ロックを取得できない場合は待たずに再試行を案内する。
        with transaction(exclusive=True) as db:
            destination = Path(destination) if destination is not None else settings.media_root / '.exports'
            destination.mkdir(mode=0o700, parents=True, exist_ok=True)
            destination.chmod(0o700)
            size = sum(row.reserved_bytes for row in db.scalars(select(Media).where(Media.status == 'ready')))
            if shutil.disk_usage(destination).free < size + 512_000_000:
                raise HTTPException(413, '出力用の空き容量が不足しています。')
            handle = tempfile.NamedTemporaryFile(prefix='export-', suffix='.zip', delete=False, dir=destination)
            path = handle.name
            handle.close()
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
            # 大きな出力の作成時間ではなく、完成時刻から残存期間を数える。
            os.utime(path, None)
        return path
    except BaseException:
        # DBトランザクションの終了に失敗した場合も、完成済みのZIPを残さない。
        if path is not None:
            Path(path).unlink(missing_ok=True)
        raise
