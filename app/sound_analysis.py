"""利用者が指定した区間をコンテナ内のFFmpegで測る。意味は推定しない。

測定値（音量・声の高さ・抑揚・長さ）と、飼い主が付ける分類（鳴き声の種類・
行動・その後・意味の予想）を分けて保存する。分類を自動で付けることはない。
"""
from decimal import Decimal, DecimalException
import json
import subprocess
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import delete, func, select, text
from . import acoustics, sound_events
from .analysis import technical_summary
from .media import paths
from .models import CallType, Cat, Media, SoundAnalysis, SoundBehavior, now
from .processes import run_bounded
from .schemas import SoundLabelsInput
from .services import parent, resolve_guess

LOUDNESS_SECONDS = 180


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


def decode(path, start_ms, length_ms):
    """指定位置から16kHzモノラルのPCMを読む。-tは上限なので、短い場合も返す。"""
    command = ['ffmpeg', '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe',
        '-format_whitelist', 'mov,mp3,wav', '-max_streams', '12', '-threads', '2',
        '-ss', str(start_ms / 1000), '-i', str(path), '-map', f'0:a:{audio_track(path)}',
        '-t', str(length_ms / 1000), '-vn', '-ac', '1', '-ar', str(acoustics.RATE),
        '-f', 's16le', '-acodec', 'pcm_s16le', 'pipe:1']
    return run_bounded(command, timeout=30, max_output=length_ms * 32 + 2)


def measure(path, start_ms, end_ms):
    """区間の平均音量・最大音量と、声の高さなどの特徴。"""
    try:
        raw = decode(path, start_ms, end_ms - start_ms)
        # -tは上限にすぎない。音声が途中で終わる区間を全体の測定として保存しない。
        expected = (end_ms - start_ms) * 16 * 2
        if len(raw) % 2 or len(raw) < expected - 50 * 16 * 2:
            raise ValueError()
        values = acoustics.samples_from_pcm(raw)
        rms = acoustics.rms(values)
        peak = max(abs(value) for value in values)
        return round(rms, 6), round(peak, 6), acoustics.features(values)
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        raise HTTPException(422, '音声を読み取れませんでした。動画と区間を確認してください。')


def usable_media(db, observation, media_id, lock=False):
    query = select(Media).where(Media.id == media_id)
    media = db.scalar(query.with_for_update() if lock else query)
    if (not media or media.observation_id != observation.id or media.status != 'ready'
            or (not media.keep and media.expires_at <= now())):
        raise HTTPException(404, 'この記録で利用できる添付ではありません。')
    return media


def original(media):
    path, _ = paths(media.storage_key)
    if path.is_symlink() or not path.is_file():
        raise HTTPException(404, '原本が見つかりません。')
    return path


def analysis_lock(db):
    if not db.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'), {'key': 741903}):
        raise HTTPException(409, '別の音声を分析中です。少し待ってから再試行してください。')


def labels(raw):
    try:
        return SoundLabelsInput.model_validate(
            {key: value for key, value in raw.items() if key not in {'start', 'end', 'media_id'}})
    except ValidationError:
        raise HTTPException(422, '鳴き声の種類・回数・行動・その後・メモ（各1000文字以内）を確認してください。')


def apply_labels(db, result, observation, data, *, assessment_explicit=True):
    if data.call_type_id is not None and db.get(CallType, data.call_type_id) is None:
        raise HTTPException(422, '鳴き声の種類を選び直してください。')
    item = resolve_guess(db, observation.cat_id, data.meaning) if data.meaning else None
    # 同じ予想の別表記では評価を保ち、予想が変わったら以前の評価を引き継がない。
    meaning_changed = result.dictionary_item_id != (item.id if item else None)
    result.call_type_id = data.call_type_id
    result.call_count = data.call_count
    result.owner_response = data.owner_response
    result.outcome = data.outcome
    result.dictionary_item_id = item.id if item else None
    result.assessment = data.assessment if item and (assessment_explicit or not meaning_changed) else 'unknown'
    result.observation, result.hypothesis, result.unknown = data.observation, data.hypothesis, data.unknown
    db.flush()
    db.execute(delete(SoundBehavior).where(SoundBehavior.sound_analysis_id == result.id))
    db.add_all(SoundBehavior(sound_analysis_id=result.id, behavior=key) for key in data.behaviors)


def create(db, observation_id, media_id, raw):
    start, end = interval(raw)
    data = labels(raw)
    # 予想の候補から辞書項目を作る処理（add_guesses）と同じ順でロックする。
    db.scalar(select(Cat).where(Cat.id == 1).with_for_update())
    row = parent(db, 'observation', observation_id, True)
    if row.is_sample:
        raise HTTPException(422, 'サンプルは分析できません。')
    media = usable_media(db, row, media_id, lock=True)
    info = technical_summary(media.technical_info)
    if not info or not info['audio'] or not info['duration_seconds'] or end > round(info['duration_seconds'] * 1000):
        raise HTTPException(422, '音声の基本情報または指定範囲を確認してください。')
    if db.scalar(select(func.count()).select_from(SoundAnalysis).where(SoundAnalysis.media_id == media.id)) >= 50:
        raise HTTPException(422, 'この添付の分析は50件までです。')
    if db.scalar(select(SoundAnalysis.id).where(SoundAnalysis.media_id == media.id,
            SoundAnalysis.start_ms == start, SoundAnalysis.end_ms == end)):
        raise HTTPException(409, 'この区間はすでに保存されています。')
    analysis_lock(db)
    rms, peak, features = measure(original(media), start, end)
    result = SoundAnalysis(media_id=media.id, start_ms=start, end_ms=end, rms=rms, peak=peak, features=features)
    db.add(result)
    db.flush()
    apply_labels(db, result, row, data)
    return result


def owned_result(db, observation_id, result_id):
    db.scalar(select(Cat).where(Cat.id == 1).with_for_update())
    row = parent(db, 'observation', observation_id, True)
    result = db.scalar(select(SoundAnalysis).join(Media).where(
        SoundAnalysis.id == result_id, Media.observation_id == row.id).with_for_update())
    if not result:
        raise HTTPException(404, '声の区間が見つかりません。')
    return row, result


def update_labels(db, observation_id, result_id, raw, *, merge=False):
    """あとで分かったその後の様子や評価を書き足す。測定値は変えない。

    merge=Trueでは指定した項目だけを変える（ローカルCLI用）。画面のフォームは全項目を送る。
    """
    row, result = owned_result(db, observation_id, result_id)
    raw = dict(raw)
    expected = raw.pop('expected_revision', None)
    # フォームだけは文字列。JSONのbool・小数・文字列は版番号として受け付けない。
    if not merge and isinstance(expected, str) and len(expected) <= 19 and expected.isascii() and expected.isdecimal():
        expected = int(expected)
    if type(expected) is not int or not 1 <= expected <= 9223372036854775807 or expected != result.revision:
        raise HTTPException(409, '声の区間の版番号が未指定、または別の画面で変更されています。画面を再読み込みし、最新の分類と入力内容を確認してください。')
    data = labels(raw)
    if merge:
        from .voices import label_values
        current = label_values(db, result)
        current.pop('revision')
        data = labels({**current, **raw})
    apply_labels(db, result, row, data, assessment_explicit='assessment' in raw)
    result.revision += 1
    result.updated_at = now()
    return result


def remeasure(db, observation_id, result_id):
    """以前の分析に声の高さなどを追加する。原本が残っている場合だけ。"""
    row, result = owned_result(db, observation_id, result_id)
    media = usable_media(db, row, result.media_id)
    analysis_lock(db)
    result.rms, result.peak, result.features = measure(original(media), result.start_ms, result.end_ms)
    result.updated_at = now()
    return result


def media_loudness(db, observation_id, media_id):
    """区間を選ぶための0.1秒ごとの音量と、大きい音の候補。保存しない。"""
    row = parent(db, 'observation', observation_id)
    media = usable_media(db, row, media_id)
    info = technical_summary(media.technical_info)
    if not info or not info['audio'] or not info['duration_seconds']:
        raise HTTPException(422, '先に動画の基本情報を取得してください。')
    analysis_lock(db)
    seconds = min(info['duration_seconds'], LOUDNESS_SECONDS)
    try:
        values = acoustics.samples_from_pcm(decode(original(media), 0, round(seconds * 1000)))
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        raise HTTPException(422, '音声を読み取れませんでした。')
    levels = acoustics.loudness(values)
    cats = sound_events.detect(values)
    return {'media_id': media.id, 'step_seconds': 0.1, 'levels': levels,
            'candidates': acoustics.loud_spans(levels),
            'cat_candidates': cats['spans'], 'cat_available': cats['available'], 'cat_notice': cats['notice'],
            'truncated': info['duration_seconds'] > LOUDNESS_SECONDS,
            'notice': '周りより大きい音の区間です。鳴き声かどうかは判定していません。'}
