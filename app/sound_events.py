"""YAMNet（GoogleのAudioSet 521種類の音の分類）で、猫の声らしい区間を探す。

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
    index = {name: labels.index(name) for name in (*CAT_LABELS, 'Speech')}
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
                         'kinds': kinds, 'speech': round(float(scores[index['Speech']]), 3)})
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


def detect(values, *, path=None):
    """音声全体から猫の声らしい区間。モデルがなければavailable=False。"""
    rows = window_scores(values, path)
    if rows is None:
        return {'available': False, 'spans': [], 'notice': '猫の声の判定モデルがありません。音量の候補を使ってください。'}
    return {'available': True, 'spans': spans(rows, loudness(values)), 'notice': NOTICE,
            'model': 'YAMNet (AudioSet)', 'windows': len(rows)}


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
