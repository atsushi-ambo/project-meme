"""検証済みメディアの技術情報。GPS・任意タグ・鳴き声の意味は抽出しない。"""
from fractions import Fraction
import math
import re


def number(value, maximum):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, str) and len(value) > 40:
        return None
    try:
        result = float(value)
        return round(result, 3) if math.isfinite(result) and 0 < result <= maximum else None
    except (ValueError, OverflowError):
        return None


def integer(value, maximum):
    if type(value) is int:
        return value if 0 < value <= maximum else None
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 10:
        return integer(int(value), maximum)
    return None


def codec(value):
    return value if isinstance(value, str) and re.fullmatch(r'[a-z0-9_]{1,40}', value) else None


def frame_rate(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        return number(float(Fraction(value)), 1000)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None


def summarize_probe(info):
    if not isinstance(info, dict) or not isinstance(info.get('streams'), list):
        return None
    streams = info['streams']
    if len(streams) > 12 or any(not isinstance(row, dict) for row in streams):
        return None
    def choose(kind):
        selected = [row for row in streams if row.get('codec_type') == kind]
        return next((row for row in selected if isinstance(row.get('disposition'), dict)
            and row['disposition'].get('default') == 1), selected[0] if selected else None)
    video, audio = choose('video'), choose('audio')
    if video is None and audio is None:
        return None
    fmt = info.get('format') if isinstance(info.get('format'), dict) else {}
    return {'version': 1, 'duration_seconds': number(fmt.get('duration'), 604800),
        'video': {'codec': codec(video.get('codec_name')), 'width': integer(video.get('width'), 32768),
                  'height': integer(video.get('height'), 32768),
                  'fps': frame_rate(video.get('avg_frame_rate')) or frame_rate(video.get('r_frame_rate'))} if video else None,
        'audio': {'codec': codec(audio.get('codec_name')), 'channels': integer(audio.get('channels'), 64),
                  'sample_rate': integer(audio.get('sample_rate'), 768000)} if audio else None}


def finite_number(value, minimum, maximum):
    if type(value) not in (int, float):
        return None
    if not minimum <= value <= maximum or not math.isfinite(value):
        return None
    return value


def public_integer(value, minimum, maximum):
    return value if type(value) is int and minimum <= value <= maximum else None


def technical_summary(info):
    """保存済みJSONも検証し、公開する仕様の項目だけを選ぶ。"""
    if not isinstance(info, dict) or type(info.get('version')) is not int or info['version'] != 1:
        return None
    video = info.get('video')
    audio = info.get('audio')
    public_video = public_audio = None
    if isinstance(video, dict) and isinstance(video.get('codec'), str) and video['codec'] in {'hevc', 'h264'}:
        public_video = {
            'codec': video['codec'],
            'width': public_integer(video.get('width'), 1, 32768),
            'height': public_integer(video.get('height'), 1, 32768),
            'fps': finite_number(video.get('fps'), 0.001, 1000),
        }
    if isinstance(audio, dict) and isinstance(audio.get('codec'), str) and audio['codec'] in {
            'aac', 'mp3', 'alac', 'pcm_s16le', 'pcm_s24le', 'pcm_f32le'}:
        public_audio = {
            'codec': audio['codec'],
            'channels': public_integer(audio.get('channels'), 1, 64),
            'sample_rate': public_integer(audio.get('sample_rate'), 1, 768000),
        }
    if public_video is None and public_audio is None:
        return None
    return {
        'version': 1,
        'duration_seconds': finite_number(info.get('duration_seconds'), 0, 604800),
        'video': public_video, 'audio': public_audio,
        'inspection_scope': 'technical metadata only',
    }

