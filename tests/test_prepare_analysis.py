"""ローカル抽出の境界と、人工動画による処理を検証する。"""
import json
from pathlib import Path
import runpy
import shutil
import subprocess

import pytest


@pytest.fixture
def module(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / 'scripts'))
    return runpy.run_path(str(root / 'scripts/prepare_analysis.py'))


def report():
    return {'observations': [{'id': 1, 'description': '飼い主のメモ', 'media': [{
        'id': 2, 'available': True, 'status': 'ready', 'mime_type': 'video/mp4',
        'url': '/media/2', 'bytes': 50, 'original_name': 'sample.mp4',
        'technical_info': {'duration_seconds': 2.0, 'video': {'codec': 'h264'},
            'audio': {'codec': 'aac'}}}]}]}


@pytest.mark.parametrize('change', ['other_id', 'unavailable', 'bad_url', 'too_long', 'missing_info'])
def test_invalid_or_unavailable_source_is_rejected(module, change):
    data = report()
    media = data['observations'][0]['media'][0]
    if change == 'other_id': media['id'] = 3
    if change == 'unavailable': media['available'] = False
    if change == 'bad_url': media['url'] = 'https://example.com/media/2'
    if change == 'too_long': media['technical_info']['duration_seconds'] = 61
    if change == 'missing_info': media['technical_info'] = None
    with pytest.raises(ValueError):
        module['selected_media'](data, 1, 2)


def test_synthetic_video_to_private_analysis_bundle(module, tmp_path, monkeypatch):
    source = tmp_path / 'synthetic.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=s=128x96:r=5',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=16000', '-t', '2',
        '-c:v', 'libx264', '-threads:v', '1', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(source)],
        check=True, timeout=15)
    calls = []
    class Client:
        def request(self, method, path):
            calls.append((method, path))
            return report()
    def fake_download(client, media, dest):
        shutil.copyfile(source, dest)
    def ffmpeg(source_path, output, args):
        args = [arg.replace('/output/', str(output) + '/') for arg in args]
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe',
            '-format_whitelist', 'mov', '-i', str(source_path)] + args, check=True, timeout=30)
    prepare = module['prepare']
    monkeypatch.setitem(prepare.__globals__, 'download', fake_download)
    monkeypatch.setitem(prepare.__globals__, 'run_ffmpeg', ffmpeg)
    base = tmp_path / 'private'
    result = prepare(Client(), 1, 2, base)
    result_data = json.loads((result / 'report.json').read_text())
    assert calls == [('GET', '/api/analysis/observations?ids=1')]
    assert len(result_data['frames']) == 2
    assert (result / 'audio.wav').is_file()
    assert (result / 'spectrogram.png').is_file()
    assert len(result_data['audio_rms_by_second']) >= 2
    assert result_data['audio_rms_by_second'][0]['rms'] > 0
    assert '猫の声や意図の判定ではありません' in result_data['notice']
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in result.iterdir())
    assert not (result / 'source.mov').exists()
    assert list(base.iterdir()) == [result]
