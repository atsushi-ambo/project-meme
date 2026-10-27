"""利用者が指定した区間をコンテナ内のFFmpegで測る。意味は推定しない。"""
from array import array
from decimal import Decimal, DecimalException
import json
import math
import subprocess
import sys
from fastapi import HTTPException
from sqlalchemy import func, select, text
from .analysis import technical_summary
from .media import paths
from .models import Media, SoundAnalysis, now
from .processes import run_bounded
from .services import parent


def interval(raw):
    try:
        strings = [str(raw[name]) for name in ('start', 'end')]
        if any(len(value) > 20 for value in strings):
            raise ValueError()
        values = [Decimal(value) for value in strings]
        if any(not value.is_finite() or value.as_tuple().exponent < -3 for value in values):
            raise ValueError()
        # 巨大な指数を整数へ展開しないよう、Decimalのまま範囲を確かめる。
        if any(not 0 <= value <= 604800 for value in values):
            raise ValueError()
        start, end = [int(value * 1000) for value in values]
        if not start < end or end - start > 10000:
            raise ValueError()
        return start, end
    except (KeyError, ValueError, DecimalException, OverflowError):
        raise HTTPException(422, '開始・終了を秒で入力してください。区間は10秒以内です。')


def audio_track(path):
    """基本情報（summarize_probe）と同じく既定の音声、なければ最初の音声を選ぶ。"""
    command = ['ffprobe', '-v', 'error', '-protocol_whitelist', 'file', '-format_whitelist', 'mov,mp3,wav',
        '-max_streams', '12', '-select_streams', 'a', '-show_entries',
        'stream=codec_name:stream_disposition=default', '-of', 'json', str(path)]
    streams = json.loads(run_bounded(command)).get('streams')
    if not isinstance(streams, list) or not streams or not all(isinstance(row, dict) for row in streams):
        raise ValueError()
    index = next((i for i, row in enumerate(streams) if isinstance(row.get('disposition'), dict)
                  and row['disposition'].get('default') == 1), 0)
    if streams[index].get('codec_name') not in {'aac', 'mp3', 'alac', 'pcm_s16le', 'pcm_s24le', 'pcm_f32le'}:
        raise ValueError()
    return index


def measure(path, start_ms, end_ms):
    try:
        command = ['ffmpeg', '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe',
            '-format_whitelist', 'mov,mp3,wav', '-max_streams', '12', '-threads', '2',
            '-ss', str(start_ms / 1000), '-i', str(path), '-map', f'0:a:{audio_track(path)}',
            '-t', str((end_ms - start_ms) / 1000), '-vn', '-ac', '1', '-ar', '16000',
            '-f', 's16le', '-acodec', 'pcm_s16le', 'pipe:1']
        raw = run_bounded(command, timeout=20, max_output=320002)
        # -tは上限にすぎない。音声が途中で終わる区間を全体の測定として保存しない。
        expected = (end_ms - start_ms) * 16 * 2
        if len(raw) % 2 or len(raw) < expected - 50 * 16 * 2:
            raise ValueError()
        samples = array('h')
        samples.frombytes(raw)
        if sys.byteorder != 'little': samples.byteswap()
        rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768
        peak = max(abs(s) for s in samples) / 32768
        return round(rms, 6), round(peak, 6)
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        raise HTTPException(422, '音声を読み取れませんでした。動画と区間を確認してください。')


def create(db, observation_id, media_id, raw):
    start, end = interval(raw)
    notes = []
    for name in ('observation', 'hypothesis', 'unknown'):
        value = str(raw.get(name, '')).strip()
        if len(value) > 1000 or '\x00' in value:
            raise HTTPException(422, 'メモは各1000文字以内で入力してください。')
        notes.append(value)
    row = parent(db, 'observation', observation_id, True)
    if row.is_sample:
        raise HTTPException(422, 'サンプルは分析できません。')
    media = db.scalar(select(Media).where(Media.id == media_id).with_for_update())
    if not media or media.observation_id != row.id or media.status != 'ready' or (not media.keep and media.expires_at <= now()):
        raise HTTPException(404, 'この記録で利用できる添付ではありません。')
    info = technical_summary(media.technical_info)
    if not info or not info['audio'] or not info['duration_seconds'] or end > round(info['duration_seconds'] * 1000):
        raise HTTPException(422, '音声の基本情報または指定範囲を確認してください。')
    if db.scalar(select(func.count()).select_from(SoundAnalysis).where(SoundAnalysis.media_id == media.id)) >= 50:
        raise HTTPException(422, 'この添付の分析は50件までです。')
    if db.scalar(select(SoundAnalysis.id).where(SoundAnalysis.media_id == media.id,
            SoundAnalysis.start_ms == start, SoundAnalysis.end_ms == end)):
        raise HTTPException(409, 'この区間はすでに保存されています。')
    if not db.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'), {'key': 741903}):
        raise HTTPException(409, '別の音声を分析中です。少し待ってから再試行してください。')
    path, _ = paths(media.storage_key)
    if path.is_symlink() or not path.is_file():
        raise HTTPException(404, '原本が見つかりません。')
    rms, peak = measure(path, start, end)
    result = SoundAnalysis(media_id=media.id, start_ms=start, end_ms=end,
        rms=rms, peak=peak, observation=notes[0], hypothesis=notes[1], unknown=notes[2])
    db.add(result)
    return result
