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


def local_classifier(source_path, output):
    """コンテナの代わりに同じモジュールを直接使う。"""
    from app import sound_events
    from app.acoustics import samples_from_pcm
    import wave
    with wave.open(str(output / 'audio.wav'), 'rb') as audio:
        return sound_events.detect(samples_from_pcm(audio.readframes(audio.getnframes())))


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
    monkeypatch.setitem(prepare.__globals__, 'run_classifier', local_classifier)
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
    assert result_data['format_version'] == 2 and len(result_data['audio_rms_by_100ms']) == 20
    assert result_data['saved_segments'] == []
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in result.iterdir())
    assert not (result / 'source.mov').exists()
    assert list(base.iterdir()) == [result]


def test_loud_candidates_get_features_and_nearby_frames(module, tmp_path, monkeypatch):
    """1秒の無音、600Hz 0.6秒、1.4秒の無音。音の前後だけ細かい静止画が残る。"""
    source = tmp_path / 'call.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=s=128x96:r=8:d=3',
        '-f', 'lavfi', '-i', 'anullsrc=r=16000:cl=mono:d=1',
        '-f', 'lavfi', '-i', 'sine=frequency=600:sample_rate=16000:duration=0.6',
        '-f', 'lavfi', '-i', 'anullsrc=r=16000:cl=mono:d=1.4',
        '-filter_complex', '[1:a][2:a][3:a]concat=n=3:v=0:a=1[a]', '-map', '0:v', '-map', '[a]',
        '-c:v', 'libx264', '-threads:v', '1', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-t', '3', str(source)],
        check=True, timeout=30)
    data = report()
    data['observations'][0]['media'][0]['technical_info']['duration_seconds'] = 3.0
    data['observations'][0]['sound_notes'] = [{'media_id': 2, 'start_seconds': 1.0, 'end_seconds': 1.6,
                                               'call_type': {'id': 1, 'name': '短い「ニャッ」'}},
                                              {'media_id': 9, 'start_seconds': 0, 'end_seconds': 1}]
    class Client:
        def request(self, method, path):
            return data
    def fake_download(client, media, dest):
        shutil.copyfile(source, dest)
    def ffmpeg(source_path, output, args):
        args = [arg.replace('/output/', str(output) + '/') for arg in args]
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(source_path)] + args, check=True, timeout=30)
    prepare = module['prepare']
    monkeypatch.setitem(prepare.__globals__, 'download', fake_download)
    monkeypatch.setitem(prepare.__globals__, 'run_ffmpeg', ffmpeg)
    monkeypatch.setitem(prepare.__globals__, 'run_classifier', local_classifier)
    result = prepare(Client(), 1, 2, tmp_path / 'private')
    result_data = json.loads((result / 'report.json').read_text())
    [candidate] = result_data['loud_candidates']
    assert 0.9 <= candidate['start'] <= 1.1 and 1.5 <= candidate['end'] <= 1.7
    assert 590 <= candidate['features']['pitch_hz']['median'] <= 610
    # 候補の前後1秒（約0〜2.6秒）だけを0.25秒間隔で残し、離れた2.75秒以降は消す。
    assert candidate['frames'][0] == 'near-000.00s.jpg' and candidate['frames'][-1] == 'near-002.50s.jpg'
    assert len(candidate['frames']) == 11
    assert sorted(p.name for p in result.glob('near-*.jpg')) == candidate['frames']
    assert [s['media_id'] for s in result_data['saved_segments']] == [2]
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in result.iterdir())
