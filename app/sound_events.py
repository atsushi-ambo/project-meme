"""YAMNet（GoogleのAudioSet 521種類の音の分類）で、猫の声らしい区間と人の声らしい区間を分けて探す。

ローカルで動き、音声を外部へ送らない。分類は一般的な猫の声の特徴によるもので、
メメの声か、何を伝えているかは判定しない。結果は区間選びの候補として扱う。
"""
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import threading
import zipfile

from .acoustics import RATE, loudness

WINDOW = 15600                 # 0.975秒（モデルの入力長）
HOP = WINDOW // 2              # 約0.49秒ずつずらす
CAT_KINDS = {'Meow': 'ニャー', 'Purr': 'ゴロゴロ', 'Hiss': 'シャー', 'Caterwaul': 'うなり・叫び'}
CAT_LABELS = ('Cat', *CAT_KINDS)
THRESHOLD = 0.3
# 人の声と重なると猫の声の強さが下がるため、弱い候補も別に示す（確認用）。
WEAK_THRESHOLD = 0.1
# 人の声（話し声・呼びかけ・叫び・ささやき）。笑い声や泣き声は含めない。
# 猫の叫びと混同しやすい Bellow・Whoop・Babbling は含めない。
HUMAN_LABELS = ('Speech', 'Child speech, kid speaking', 'Conversation', 'Narration, monologue',
                'Speech synthesizer', 'Shout', 'Yell', 'Children shouting', 'Whispering')
WEAK_LIMIT = 5
HUMAN_THRESHOLD = 0.3
NOTICE = ('YAMNetが一般的な猫の声の特徴から見つけた候補です。メメの声か、何を伝えているかは判定していません。'
          '近くの人の声やテレビの音も混ざることがあります。')

_lock = threading.Lock()
_model: dict = {}


def model_path():
    from .config import settings
    return settings.sound_model_path


def load(path=None):
    """モデルを一度だけ読み込む。使えなければNone。"""
    path = model_path() if path is None else path
    with _lock:
        if path in _model:
            return _model[path]
        loaded = None
        try:
            from ai_edge_litert.interpreter import Interpreter
            data = path.read_bytes()
            labels = zipfile.ZipFile(BytesIO(data)).read('yamnet_label_list.txt').decode().splitlines()
            interpreter = Interpreter(model_content=data, num_threads=2)
            interpreter.allocate_tensors()
            loaded = {'interpreter': interpreter, 'labels': labels,
                      'input': interpreter.get_input_details()[0]['index'],
                      'output': interpreter.get_output_details()[0]['index']}
        except (ImportError, OSError, ValueError, KeyError, zipfile.BadZipFile, RuntimeError):
            loaded = None
        _model[path] = loaded
        return loaded


def available():
    return load() is not None


def window_scores(values, path=None):
    """約0.49秒ごとの、猫の声の各種類と人の声の強さ（0〜1）。"""
    import numpy as np
    model = load(path)
    if model is None:
        return None
    labels = model['labels']
    index = {name: labels.index(name) for name in (*CAT_LABELS, *HUMAN_LABELS) if name in labels}
    human = [index[name] for name in HUMAN_LABELS if name in index]
    samples = np.asarray(values, dtype=np.float32)
    if len(samples) < WINDOW:
        samples = np.pad(samples, (0, WINDOW - len(samples)))
    rows = []
    with _lock:
        interpreter = model['interpreter']
        for start in range(0, len(samples) - WINDOW + 1, HOP):
            interpreter.set_tensor(model['input'], samples[start:start + WINDOW])
            interpreter.invoke()
            scores = interpreter.get_tensor(model['output'])[0]
            kinds = {name: round(float(scores[index[name]]), 3) for name in CAT_KINDS}
            rows.append({'start': round(start / RATE, 3),
                         'cat': round(max(float(scores[index[name]]) for name in CAT_LABELS), 3),
                         'kinds': kinds, 'speech': round(float(scores[index['Speech']]), 3),
                         'human': round(max(float(scores[i]) for i in human), 3)})
    return rows


def spans(rows, levels, threshold=THRESHOLD):
    """猫らしさが続く窓をまとめ、0.1秒ごとの音量で前後の静かな部分を詰める。"""
    found, current = [], []
    for row in rows + [None]:
        if row is not None and row['cat'] >= threshold:
            current.append(row)
            continue
        if current:
            start, end = current[0]['start'], current[-1]['start'] + WINDOW / RATE
            first, last = int(start * 10), min(int(end * 10) + 1, len(levels))
            inside = levels[first:last]
            if inside and max(inside) > 0:
                floor = max(inside) * 0.15
                loud = [i for i, level in enumerate(inside) if level >= floor]
                start, end = (first + loud[0]) / 10, (first + loud[-1] + 1) / 10
            peak = max(row['cat'] for row in current)
            totals = {name: sum(row['kinds'][name] for row in current) for name in CAT_KINDS}
            kind = max(totals, key=totals.get)
            found.append({
                'start': round(start, 1), 'end': round(end, 1),
                'kind': kind if totals[kind] / len(current) >= 0.1 else 'Cat',
                'kind_label': CAT_KINDS.get(kind, '猫の声') if totals[kind] / len(current) >= 0.1 else '猫の声',
                'strength': 'high' if peak >= 0.7 else 'mid', 'peak': round(peak, 2),
                'speech': max(row['speech'] for row in current) >= 0.3,
            })
            current = []
    return found


def trimmed(current, levels):
    """窓の並びを、0.1秒ごとの音量で前後の静かな部分を詰めた区間にする。"""
    start, end = current[0]['start'], current[-1]['start'] + WINDOW / RATE
    first, last = int(start * 10), min(int(end * 10) + 1, len(levels))
    inside = levels[first:last]
    if inside and max(inside) > 0:
        floor = max(inside) * 0.15
        loud = [i for i, level in enumerate(inside) if level >= floor]
        start, end = (first + loud[0]) / 10, (first + loud[-1] + 1) / 10
    return round(start, 1), round(end, 1)


def human_spans(rows, levels, threshold=HUMAN_THRESHOLD):
    """人の声らしさが続く区間。飼い主の呼びかけなどを猫の声と分けるための候補で、誰の声かは判定しない。"""
    found, current = [], []
    for row in rows + [None]:
        # 猫らしさの方が強い窓は、叫び声などの猫の声の可能性があるため人の声に数えない。
        if row is not None and row.get('human', 0) >= threshold and row.get('human', 0) > row['cat']:
            current.append(row)
            continue
        if current:
            start, end = trimmed(current, levels)
            peak = max(row['human'] for row in current)
            found.append({'start': start, 'end': end, 'peak': round(peak, 2),
                          'strength': 'high' if peak >= 0.7 else 'mid',
                          'cat_peak': round(max(row['cat'] for row in current), 2)})
            current = []
    return found


def weak_spans(rows, levels):
    """通常の候補にならない弱い猫らしさ（0.1〜0.3）。人の声と重なった鳴き声の確認用。"""
    # 通常の候補（0.3以上）の窓は除き、その前後の弱い部分も別に見えるようにする。
    masked = [row if row['cat'] < THRESHOLD else dict(row, cat=0.0) for row in rows]
    found = [dict(span, strength='low') for span in spans(masked, levels, WEAK_THRESHOLD)]
    strongest = sorted(found, key=lambda span: span['peak'], reverse=True)[:WEAK_LIMIT]
    return sorted(strongest, key=lambda span: span['start'])


def timeline(rows):
    """約0.49秒ごとの猫らしさと人の声らしさ（0〜1）。"""
    return [{'start': row['start'], 'cat': round(row['cat'], 2), 'human': round(row.get('human', 0), 2)}
            for row in rows]


def detect(values, *, path=None):
    """音声全体から猫の声らしい区間と人の声らしい区間。モデルがなければavailable=False。"""
    rows = window_scores(values, path)
    if rows is None:
        return {'available': False, 'spans': [], 'notice': '猫の声の判定モデルがありません。音量の候補を使ってください。'}
    levels = loudness(values)
    return {'available': True, 'spans': spans(rows, levels), 'notice': NOTICE,
            'model': 'YAMNet (AudioSet)', 'windows': len(rows),
            'weak_spans': weak_spans(rows, levels), 'human_spans': human_spans(rows, levels),
            'timeline': timeline(rows)}


def main():
    """分析用コンテナから使う。16kHzモノラルのWAVを読み、結果をJSONで出す。"""
    import wave
    from .acoustics import samples_from_pcm
    with wave.open(sys.argv[1], 'rb') as audio:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getframerate() != RATE:
            raise SystemExit('16kHzモノラルのWAVが必要です。')
        values = samples_from_pcm(audio.readframes(RATE * 600))
    # 単独コンテナではDBやセッションの設定を読み込まない。モデルの場所だけを使う。
    path = Path(os.environ.get('SOUND_MODEL_PATH', '/opt/meme/yamnet.tflite'))
    print(json.dumps(detect(values, path=path), ensure_ascii=False))


if __name__ == '__main__':
    main()
