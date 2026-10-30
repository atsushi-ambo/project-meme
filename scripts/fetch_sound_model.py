"""YAMNet（Google、Apache-2.0）の音分類モデルを取得し、SHA-256を照合して保存する。

Dockerビルドと品質CIの両方で使う。照合に失敗したファイルは保存しない。
"""
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import urllib.request

URL = 'https://storage.googleapis.com/mediapipe-models/audio_classifier/yamnet/float32/1/yamnet.tflite'
SHA256 = '4d8b4a53282dc83ef04e3e7dbc4fbc98082e34e44ed798e16c3a0cdd4c584faf'
MAX_BYTES = 10_000_000


def fetch(destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(URL, timeout=60) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES or hashlib.sha256(data).hexdigest() != SHA256:
        raise SystemExit('YAMNetモデルのSHA-256が一致しません。保存しませんでした。')
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as file:
        file.write(data)
    os.chmod(file.name, 0o644)
    os.replace(file.name, destination)
    print(f'YAMNetモデルを保存しました: {destination}')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('使い方: python3 scripts/fetch_sound_model.py 保存先.tflite')
    fetch(sys.argv[1])
