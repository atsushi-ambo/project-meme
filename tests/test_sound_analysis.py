"""Test sound measurements using synthetic media and the isolated PostgreSQL database."""
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from app.db import transaction
from app.models import SoundAnalysis
from test_app import client, csrf, observation, post
from test_media_info import video_bytes, saved_video, info_post


def test_sound_analysis_saves_and_displays_measurement(client, video_bytes):
    oid, mid = saved_video(client, video_bytes)
    assert info_post(client, mid).status_code == 200
    data = {'start': '0', 'end': '0.1', 'observation': '短い音', 'hypothesis': '遊びたいかも', 'unknown': '不明'}
    url = f'/observations/{oid}/sound/{mid}'
    assert post(client, url, data).status_code == 303
    page = client.get(f'/entries/observation/{oid}').text
    assert '短い音' in page and '遊びたいかも' in page and '平均' in page
    assert post(client, url, data).status_code == 409
    with transaction() as db:
        results = list(db.scalars(select(SoundAnalysis).where(SoundAnalysis.media_id == mid)))
        assert len(results) == 1 and results[0].rms == 0
        result_id = results[0].id
    assert post(client, f'/observations/{oid}/sound-result/{result_id}/delete', {}).status_code == 303
    assert '遊びたいかも' not in client.get(f'/entries/observation/{oid}').text


def test_sound_analysis_rejects_cross_record_and_invalid_input(client, video_bytes):
    oid, mid = saved_video(client, video_bytes)
    other = observation(client, '別の記録')
    assert info_post(client, mid).status_code == 200
    data = {'start': '0', 'end': '0.1'}
    assert post(client, f'/observations/{other}/sound/{mid}', data).status_code == 404
    assert client.post(f'/observations/{oid}/sound/{mid}', data=data,
                       headers={'Origin': 'http://localhost:8000'}).status_code == 403
    for start, end in [('nan', '1'), ('0', '11'), ('0.15', '0.1'), ('0', '0.3'), ('1e999999', '1'), ('1e999996', '1'), ('-1', '1')]:
        assert post(client, f'/observations/{oid}/sound/{mid}', {'start': start, 'end': end}).status_code == 422
    with transaction() as db:
        assert list(db.scalars(select(SoundAnalysis).where(SoundAnalysis.media_id == mid))) == []


def test_measure_reads_only_the_selected_interval(tmp_path):
    """前半1秒は無音、後半1秒は振幅1/8（FFmpegのsineの既定）の音。開始位置がずれると値が混ざる。"""
    import subprocess
    from app.sound_analysis import measure
    path = tmp_path / 'tone.wav'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=mono:d=1',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=1',
        '-filter_complex', '[0:a][1:a]concat=n=2:v=0:a=1', str(path)],
        check=True, capture_output=True, timeout=15)
    assert measure(path, 0, 900) == (0, 0)
    rms, peak = measure(path, 1100, 1900)
    assert 0.087 < rms < 0.090 and 0.123 < peak < 0.126
    with pytest.raises(HTTPException):
        measure(path, 1500, 2500)  # 音声は2秒で終わり、区間の半分しかない。


def test_sound_analysis_survives_deleted_original_and_appears_in_comparison(client, video_bytes):
    from app.models import Media
    oid, mid = saved_video(client, video_bytes)
    assert info_post(client, mid).status_code == 200
    assert post(client, f'/observations/{oid}/sound/{mid}',
                {'start': '0', 'end': '0.1', 'hypothesis': '呼んでいるかも'}).status_code == 303
    report = client.get(f'/api/analysis/observations?ids={oid}').json()
    notes = report['observations'][0]['sound_notes']
    assert notes == [{'media_id': mid, 'start_seconds': 0, 'end_seconds': 0.1, 'observation': '',
                      'hypothesis': '呼んでいるかも', 'unknown': ''}]
    with transaction() as db:
        db.get(Media, mid).status = 'deleted'
    page = client.get(f'/entries/observation/{oid}').text
    assert '呼んでいるかも' in page and '元の添付は現在利用できません' in page
    assert post(client, f'/observations/{oid}/sound/{mid}', {'start': '0', 'end': '0.05'}).status_code == 404


def test_measure_uses_the_default_audio_track_like_the_summary(tmp_path):
    """先頭は既定でない無音、2本目が既定の音。基本情報と同じ2本目を測る。"""
    import subprocess
    from app.sound_analysis import measure
    path = tmp_path / 'two-tracks.m4a'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=mono:d=1',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=1',
        '-map', '0:a', '-map', '1:a', '-c:a', 'aac', '-disposition:a:0', '0',
        '-disposition:a:1', 'default', str(path)], check=True, capture_output=True, timeout=15)
    rms, peak = measure(path, 100, 900)
    assert rms > 0.05 and peak > 0.1
