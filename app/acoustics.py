"""16kHzモノラルの音から、声の高さ・抑揚・長さ・音量の変化を測る。

標準ライブラリだけで動き、アプリとローカルCLIの両方から使う。
測るのは音の物理的な特徴だけで、猫の声かどうか、意味・感情は判定しない。
人の声や物音が一番強い周期音なら、その高さを測ることもある。
"""
from array import array
from itertools import accumulate
import math
from operator import mul
import statistics
import sys

RATE = 16000
FRAME = 640          # 40ms。猫の声の高さ（数百Hz）を数周期含む長さ。
HOP = 320            # 20ms
MIN_HZ = 200         # 低いうなりの一部と人の声の上側を含む範囲。
MAX_HZ = 1600
VOICED = 0.5         # 正規化した自己相関がこれ未満のフレームは高さ不明とする。
SILENCE = 0.003      # これより小さいRMSは無音扱い。
STEP = RATE // 10    # 音量の推移は0.1秒ごと。
CONTOURS = {'rising': '上がる', 'falling': '下がる', 'arch': '上がって下がる',
            'valley': '下がって上がる', 'flat': 'ほぼ平ら', 'unknown': '判定できない'}


def samples_from_pcm(raw):
    """リトルエンディアンのs16le PCMを-1〜1のfloat列にする。"""
    values = array('h')
    values.frombytes(raw[:len(raw) - len(raw) % 2])
    if sys.byteorder != 'little':
        values.byteswap()
    return [value / 32768 for value in values]


def rms(values):
    return math.sqrt(sum(map(mul, values, values)) / len(values)) if values else 0.0


def frame_pitch(frame, lags):
    """1フレームの基本周波数（Hz）と周期性の強さ。周期が弱ければHzはNone。"""
    squares = list(accumulate(map(mul, frame, frame), initial=0.0))
    total = squares[-1]
    best_lag, best_score, scores = None, 0.0, {}
    for lag in lags:
        energy = (squares[len(frame) - lag]) * (total - squares[lag])
        if energy <= 0:
            continue
        score = sum(map(mul, frame, frame[lag:])) / math.sqrt(energy)
        scores[lag] = score
        if score > best_score:
            best_lag, best_score = lag, score
    if best_lag is None or best_score < VOICED:
        return None, best_score
    # 周期の倍数でも相関は高くなるため、最大値に近い最短の周期を選ぶ（オクターブ誤り対策）。
    for lag in lags:
        if scores.get(lag, 0) >= 0.9 * best_score and scores.get(lag, 0) >= scores.get(lag - 1, 0) \
                and scores.get(lag, 0) >= scores.get(lag + 1, 0):
            best_lag = lag
            break
    left, center, right = scores.get(best_lag - 1), scores[best_lag], scores.get(best_lag + 1)
    shift = 0.0
    if left is not None and right is not None:
        denominator = left - 2 * center + right
        if denominator < 0:
            shift = max(-0.5, min(0.5, 0.5 * (left - right) / denominator))
    return RATE / (best_lag + shift), best_score


def pitch_track(values):
    """20msごとの (秒, Hz or None, RMS)。小さすぎる音は高さを測らない。"""
    lags = range(RATE // MAX_HZ, RATE // MIN_HZ + 1)
    frames = []
    for start in range(0, max(len(values) - FRAME, 0) + 1, HOP):
        frame = values[start:start + FRAME]
        if len(frame) < FRAME:
            break
        level = rms(frame)
        frames.append([start / RATE, None, level])
    if not frames:
        return []
    loudest = max(level for _, _, level in frames)
    # 一番大きい部分より約14dB以上小さいフレームは背景音とみなして高さを測らない。
    floor = max(SILENCE, loudest * 0.2)
    for row in frames:
        if row[2] >= floor:
            start = round(row[0] * RATE)
            mean = sum(values[start:start + FRAME]) / FRAME
            frame = [value - mean for value in values[start:start + FRAME]]
            row[1], _ = frame_pitch(frame, lags)
    return [tuple(row) for row in frames]


def contour(pitches):
    """有声フレームを3等分し、半音（約6%）以上の差で大まかな抑揚を表す。"""
    if len(pitches) < 6:
        return 'unknown'
    third = len(pitches) // 3
    first = statistics.median(pitches[:third])
    middle = statistics.median(pitches[third:len(pitches) - third])
    last = statistics.median(pitches[len(pitches) - third:])
    up, down = 1.06, 1 / 1.06
    if middle > first * up and middle > last * up:
        return 'arch'
    if middle < first * down and middle < last * down:
        return 'valley'
    if last > first * up:
        return 'rising'
    if last < first * down:
        return 'falling'
    return 'flat'


def smooth(pitches):
    """3点の中央値で、単発の外れ値（オクターブ誤りなど）を抑える。"""
    if len(pitches) < 3:
        return list(pitches)
    return [pitches[0]] + [statistics.median(pitches[i - 1:i + 2]) for i in range(1, len(pitches) - 1)] + [pitches[-1]]


def features(values):
    """区間全体の特徴。値はJSONにそのまま保存できる型だけにする。"""
    track = pitch_track(values)
    if not track:
        return {'version': 1, 'voiced_ms': 0, 'sound_ms': 0, 'pitch_hz': None, 'contour': 'unknown'}
    loudest = max(level for _, _, level in track)
    floor = max(SILENCE, loudest * 0.2)
    sound_frames = sum(level >= floor for _, _, level in track)
    pitches = smooth([hz for _, hz, _ in track if hz is not None])
    pitch = None
    if pitches:
        ordered = sorted(pitches)
        low = ordered[int(0.1 * (len(ordered) - 1))]
        high = ordered[int(0.9 * (len(ordered) - 1))]
        pitch = {'median': round(statistics.median(pitches)), 'low': round(low), 'high': round(high),
                 'start': round(statistics.median(pitches[:3])), 'end': round(statistics.median(pitches[-3:]))}
    return {'version': 1, 'voiced_ms': len(pitches) * HOP * 1000 // RATE,
            'sound_ms': sound_frames * HOP * 1000 // RATE,
            'pitch_hz': pitch, 'contour': contour(pitches)}


def loudness(values):
    """0.1秒ごとのRMS（0〜1）。すべての音を含む。"""
    return [round(rms(values[i:i + STEP]), 5) for i in range(0, len(values) - STEP // 2, STEP)]


def loud_spans(levels, *, minimum=0.2, gap=3, limit=20):
    """周囲より大きい音が続く0.1秒単位の範囲。鳴き声の検出ではなく、聞き直す候補。"""
    if not levels:
        return []
    background = statistics.median(levels)
    threshold = max(SILENCE * 3, background * 3, max(levels) * 0.25)
    spans, start, quiet = [], None, 0
    for index, level in enumerate(levels + [0.0] * (gap + 1)):
        if level >= threshold:
            if start is None:
                start = index
            quiet = 0
        elif start is not None:
            quiet += 1
            if quiet > gap:
                end = index - quiet + 1
                if (end - start) / 10 >= minimum:
                    peak = max(levels[start:end])
                    spans.append({'start': start / 10, 'end': min(end, len(levels)) / 10, 'peak_rms': peak})
                start, quiet = None, 0
    spans.sort(key=lambda span: -span['peak_rms'])
    return sorted(spans[:limit], key=lambda span: span['start'])
