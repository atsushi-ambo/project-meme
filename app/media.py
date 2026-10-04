"""ファイルとDBの二段階保存、期限削除。実ファイル名はランダムキーだけ。"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import timedelta
from pathlib import Path
import anyio
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import select, func, or_, delete
from sqlalchemy.orm import Session
from .config import settings
from .db import transaction, engine
from .models import Cat, Media, Observation, Health, DictionaryItem, now
from .services import parent

Image.MAX_IMAGE_PIXELS = 30_000_000

def paths(key):
    if not re.fullmatch('[0-9a-f]{32}', key):
        raise RuntimeError('invalid_storage_key')
    root = settings.media_root.resolve()
    return root / key, root / 'staging' / key

def used_bytes(db):
    return db.scalar(select(func.coalesce(func.sum(Media.reserved_bytes), 0)))

def reserve(db, data):
    db.scalar(select(Cat).where(Cat.id == 1).with_for_update())
    entry = parent(db, data.parent_kind, data.parent_id, True)
    existing = db.scalar(select(Media).where(Media.upload_key == str(data.upload_key)))
    if existing:
        target = existing.observation_id if data.parent_kind == 'observation' else existing.health_entry_id
        if target != entry.id or existing.original_name != data.original_name or existing.reserved_bytes != data.size:
            raise HTTPException(409, '同じ添付送信の内容が異なります。')
        return existing
    if data.size > settings.max_file_bytes or used_bytes(db) + data.size > settings.max_total_bytes:
        raise HTTPException(413, '容量上限です。添付を整理するか設定を変更してください。本文は保存されています。')
    if shutil.disk_usage(settings.media_root).free < data.size * 2 + 512_000_000:
        raise HTTPException(413, 'MacBookの空き容量が不足しています。')
    count = db.scalar(select(func.count()).select_from(Media).where(
        (Media.observation_id == entry.id) if data.parent_kind == 'observation' else (Media.health_entry_id == entry.id),
        Media.status.in_(['pending','ready','delete_pending'])))
    if count >= 10:
        raise HTTPException(413, '一つの記録の添付は10件までです。')
    row = Media(storage_key=uuid.uuid4().hex, original_name=data.original_name,
        reserved_bytes=data.size, upload_key=str(data.upload_key), captured_at=data.captured_at,
        captured_at_source='user' if data.captured_at else 'unknown',
        **{('observation_id' if data.parent_kind == 'observation' else 'health_entry_id'): entry.id})
    db.add(row)
    db.flush()
    return row

def inspect_file(path, name):
    suffix = Path(name).suffix.lower()
    if suffix in {'.jpg','.jpeg','.png'}:
        try:
            with Image.open(path) as image:
                if image.format not in {'JPEG','PNG'}:
                    raise ValueError('format')
                image.verify()
                return 'image/jpeg' if image.format == 'JPEG' else 'image/png'
        except Exception:
            raise HTTPException(415, '画像の内容を確認できません。JPEG/PNGを選んでください。')
    if suffix not in {'.mov','.mp4','.m4a','.mp3','.wav'}:
        raise HTTPException(415, '対応形式はMOV/MP4（HEVC・H.264）、JPEG/PNG、M4A/MP3/WAVです。')
    # 固定のローカルパスのみ。ネットワークプロトコルやプレイリストを許可しない。
    command = ['ffprobe','-v','error','-protocol_whitelist','file','-format_whitelist','mov,mp3,wav',
        '-show_entries','stream=codec_type,codec_name,codec_tag_string:stream_disposition=default:format=format_name,duration','-of','json', str(path)]
    try:
        result = subprocess.run(command, capture_output=True, timeout=15, check=True)
        info = json.loads(result.stdout)
        streams = info.get('streams', [])
        video = [s for s in streams if s.get('codec_type') == 'video']
        audio = [s for s in streams if s.get('codec_type') == 'audio']
        if len(streams) > 12 or not (video or audio):
            raise ValueError('streams')
        if any(s.get('codec_name') not in {'hevc','h264'} for s in video):
            raise ValueError('video_codec')
        fmt = info['format']['format_name'].split(',')
        # iPhoneの空間音声は通常のAACと別トラックに入る。古いffprobeでは
        # APACのcodec_nameが得られないため、非既定の補助トラックだけ許可する。
        aac_fallback = any(s.get('codec_name') == 'aac' and
                           s.get('disposition', {}).get('default') == 1 for s in audio)
        for stream in audio:
            if stream.get('codec_name') in {'aac','mp3','alac','pcm_s16le','pcm_s24le','pcm_f32le'}:
                continue
            if (video and 'mov' in fmt and aac_fallback
                    and stream.get('codec_tag_string') == 'apac'
                    and stream.get('codec_name') in {None, 'apac'}
                    and stream.get('disposition', {}).get('default') == 0):
                continue
            raise ValueError('audio_codec')
        if video and suffix in {'.mov','.mp4'} and 'mov' in fmt:
            return 'video/mp4' if suffix == '.mp4' else 'video/quicktime'
        if not video and audio:
            if suffix == '.m4a' and 'mov' in fmt: return 'audio/mp4'
            if suffix == '.mp3' and 'mp3' in fmt: return 'audio/mpeg'
            if suffix == '.wav' and 'wav' in fmt: return 'audio/wav'
        raise ValueError('container')
    except (ValueError, KeyError, subprocess.SubprocessError):
        raise HTTPException(415, '形式を確認できません。HEVC/H.264の動画または対応音声を選んでください。')

class Upload:
    """一件の受信中だけDBロックを保持。別スレッドから同時には使用しない。"""
    def __init__(self, id):
        self.id = id
        self.context = transaction()
        self.db = self.context.__enter__()
        self.file = None
        try:
            self.row = self.db.scalar(select(Media).where(Media.id == id).with_for_update(nowait=True))
            if not self.row or self.row.status != 'pending':
                raise HTTPException(409, '添付が処理中か保存済みです。画面を更新してください。')
            kind = 'observation' if self.row.observation_id else 'health'
            parent(self.db, kind, self.row.observation_id or self.row.health_entry_id)
            self.final, self.temp = paths(self.row.storage_key)
            self.temp.parent.mkdir(parents=True, exist_ok=True)
            self.file = self.temp.open('xb')
            self.size = 0
        except BaseException as exc:
            self.context.__exit__(type(exc), exc, exc.__traceback__)
            raise

    def write(self, chunk):
        self.size += len(chunk)
        if self.size > self.row.reserved_bytes or self.size > settings.max_file_bytes:
            raise HTTPException(413, '予約した容量を超えました。')
        self.file.write(chunk)

    def finish(self):
        if self.size != self.row.reserved_bytes:
            raise HTTPException(422, '受信サイズが一致しません。再試行してください。')
        self.file.flush()
        os.fsync(self.file.fileno())
        self.file.close()
        mime = inspect_file(self.temp, self.row.original_name)
        os.replace(self.temp, self.final)
        self.row.mime_type, self.row.bytes = mime, self.size
        self.row.status = 'ready'
        self.row.retention_started_at = now()
        self.row.expires_at = now() + timedelta(days=settings.retention_days)
        self.context.__exit__(None, None, None)

    def abort(self, exc):
        if self.file: self.file.close()
        self.context.__exit__(type(exc), exc, exc.__traceback__)
        # 失敗ファイルを片づけられなければ予約を残し、ジョブが再試行する。
        with transaction() as db:
            row = db.get(Media, self.id, with_for_update=True)
            if row and row.status == 'pending':
                row.status = 'delete_pending'
                row.deletion_reason = 'upload_failed'

async def receive_upload(request, id):
    upload = await anyio.to_thread.run_sync(Upload, id)
    try:
        async for chunk in request.stream():
            if chunk:
                await anyio.to_thread.run_sync(upload.write, chunk)
        await anyio.to_thread.run_sync(upload.finish)
    except BaseException as exc:
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(upload.abort, exc)
        raise

def request_delete(db, kind, id):
    row = parent(db, kind, id, True)
    row.deletion_requested_at = now()
    condition = Media.observation_id == id if kind == 'observation' else Media.health_entry_id == id
    for media in db.scalars(select(Media).where(condition).with_for_update()):
        if media.status != 'deleted':
            media.status, media.deletion_reason = 'delete_pending', 'parent_deleted'

def run_jobs(at=None):
    """排他保守ロックでアップロード・出力との競合を避ける。再実行可能。"""
    at = at or now()
    with transaction(exclusive=True) as db:
        rows = list(db.scalars(select(Media).with_for_update()))
        for row in rows:
            stale = row.status == 'pending' and row.created_at < at - timedelta(minutes=15)
            expired = row.status == 'ready' and not row.keep and row.expires_at <= at
            if stale or expired:
                row.status = 'delete_pending'
                row.deletion_reason = 'expired' if expired else 'upload_failed'
            if row.status != 'delete_pending' or (row.next_retry_at and row.next_retry_at > at):
                continue
            try:
                for path in paths(row.storage_key):
                    path.unlink(missing_ok=True)
                row.status = 'failed' if row.deletion_reason == 'upload_failed' else 'deleted'
                row.deleted_at, row.reserved_bytes = at, 0
                row.last_error_code, row.next_retry_at = None, None
            except OSError:
                row.retry_count += 1
                row.last_error_code = 'file_delete_failed'
                row.next_retry_at = at + timedelta(seconds=min(3600, 30 * 2 ** min(row.retry_count, 7)))
        db.flush()
        for model, field in ((Observation, Media.observation_id), (Health, Media.health_entry_id)):
            for entry in db.scalars(select(model).where(model.deletion_requested_at.is_not(None))):
                children = list(db.scalars(select(Media).where(field == entry.id)))
                if all(m.status in {'failed','deleted'} and m.reserved_bytes == 0 for m in children):
                    db.execute(delete(Media).where(field == entry.id))
                    db.delete(entry)
        known = {r.storage_key for r in rows}
        for folder in (settings.media_root, settings.media_root / 'staging'):
            for path in folder.iterdir():
                if path.is_file() and re.fullmatch('[0-9a-f]{32}', path.name) and path.name not in known:
                    if path.stat().st_mtime < at.timestamp() - 900:
                        path.unlink()

        marker = settings.media_root / 'deleting-all'
        if marker.exists():
            # 停止が削除要求のcommit前だった場合も全削除を再開する。
            remaining = False
            for model in (Observation, Health):
                for entry in db.scalars(select(model)):
                    remaining = True
                    entry.deletion_requested_at = at
            for row in db.scalars(select(Media)):
                if row.status not in {'deleted','failed'} or row.reserved_bytes:
                    remaining = True
                    row.status, row.deletion_reason = 'delete_pending', 'all_deleted'
            if not remaining:
                db.execute(delete(Media))
                db.execute(delete(DictionaryItem))
                cat = db.get(Cat, 1)
                cat.name, cat.notes, cat.birthday = 'メメ', '', None
                db.flush()
                # DB commit後にマーカーを消す。失敗時は次の周期に再実行。
                db.commit()
                marker.unlink(missing_ok=True)
