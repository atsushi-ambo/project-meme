"""解析プロセスと画像の上限を、小さな合成データで検証する。"""
import json
import subprocess
import sys
import time

import pytest
from fastapi import HTTPException
from PIL import Image

from app import processes
from app.media import inspect_file
from app.media_metadata import video_capture_time


def python_command(code):
    return [sys.executable, '-c', code]


def tracked_processes(monkeypatch):
    children = []
    original = subprocess.Popen

    def spawn(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(processes.subprocess, 'Popen', spawn)
    return children


def test_process_exact_output_boundary_and_closed_stdin():
    command = python_command("import sys; assert sys.stdin.buffer.read() == b''; sys.stdout.buffer.write(b'x' * 1024)")
    assert processes.run_bounded(command, max_output=1024) == b'x' * 1024
    assert processes.run_bounded(python_command('pass'), max_output=0) == b''


@pytest.mark.parametrize('size', [1025, 2_000_000])
def test_process_output_overflow_kills_and_reaps_child(monkeypatch, size):
    children = tracked_processes(monkeypatch)
    command = python_command(f"import sys, time; sys.stdout.buffer.write(b'x' * {size}); sys.stdout.flush(); time.sleep(30)")
    with pytest.raises(processes.OutputLimitExceeded):
        processes.run_bounded(command, max_output=1024, timeout=3)
    assert children and all(child.poll() is not None for child in children)
    assert all(child.stdout.closed for child in children)


@pytest.mark.parametrize('code', [
    'import time; time.sleep(30)',
    "import sys, time; sys.stdout.write('x'); sys.stdout.flush(); time.sleep(30)",
    "import sys, time; sys.stdout.close(); time.sleep(30)",
])
def test_process_timeout_including_after_output_eof_reaps_child(monkeypatch, code):
    children = tracked_processes(monkeypatch)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        processes.run_bounded(python_command(code), timeout=0.25)
    assert time.monotonic() - started < 3
    assert children and all(child.poll() is not None for child in children)


def test_large_stderr_is_discarded_without_deadlock():
    command = python_command("import sys; sys.stderr.buffer.write(b'e' * 2_000_000); sys.stdout.write('ok')")
    assert processes.run_bounded(command, max_output=2, timeout=3) == b'ok'


def test_nonzero_exit_does_not_include_private_output(monkeypatch):
    children = tracked_processes(monkeypatch)
    command = python_command("import sys; sys.stderr.write('private-error'); sys.stdout.write('private-output'); sys.exit(7)")
    with pytest.raises(subprocess.CalledProcessError) as failure:
        processes.run_bounded(command)
    assert failure.value.returncode == 7
    assert failure.value.output is None and failure.value.stderr is None
    assert children[0].poll() == 7


def test_invalid_process_limits_and_missing_binary():
    for limits in ({'timeout': 0}, {'max_output': -1}):
        with pytest.raises(ValueError):
            processes.run_bounded(python_command('pass'), **limits)
    with pytest.raises(FileNotFoundError):
        processes.run_bounded(['/nonexistent/project-meme-test-command'])


@pytest.mark.parametrize('format,suffix', [('PNG', '.png'), ('JPEG', '.jpg')])
def test_image_pixel_boundary_is_enforced(tmp_path, monkeypatch, format, suffix):
    monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 100)
    at_limit = tmp_path / ('boundary' + suffix)
    Image.new('RGB', (10, 10)).save(at_limit, format=format)
    expected = 'image/png' if format == 'PNG' else 'image/jpeg'
    assert inspect_file(at_limit, at_limit.name) == expected
    too_large = tmp_path / ('too-large' + suffix)
    Image.new('RGB', (11, 10)).save(too_large, format=format)
    with pytest.raises(HTTPException) as error:
        inspect_file(too_large, too_large.name)
    assert error.value.status_code == 415


@pytest.mark.parametrize('failure', [
    processes.OutputLimitExceeded('process_output_limit'),
    subprocess.TimeoutExpired('ffprobe', 15),
    subprocess.CalledProcessError(1, 'ffprobe'),
])
def test_probe_failures_reject_media_but_optional_metadata_stays_unknown(tmp_path, monkeypatch, failure):
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr('app.media.run_bounded', fail)
    monkeypatch.setattr('app.media_metadata.run_bounded', fail)
    with pytest.raises(HTTPException) as error:
        inspect_file(tmp_path / 'video', 'sample.mov')
    assert error.value.status_code == 415
    assert video_capture_time(tmp_path / 'video', 'sample.mov') is None


def test_both_probe_commands_limit_streams_and_networks(tmp_path, monkeypatch):
    commands = []
    def result(command):
        commands.append(command)
        return json.dumps({'streams': [{'codec_type': 'video', 'codec_name': 'h264'}],
                           'format': {'format_name': 'mov', 'tags': {}}}).encode()
    monkeypatch.setattr('app.media.run_bounded', result)
    monkeypatch.setattr('app.media_metadata.run_bounded', result)
    assert inspect_file(tmp_path / 'video', 'sample.mov') == 'video/quicktime'
    assert video_capture_time(tmp_path / 'video', 'sample.mov') is None
    for command in commands:
        assert command[command.index('-max_streams') + 1] == '12'
        assert command[command.index('-protocol_whitelist') + 1] == 'file'
        assert command.index('-max_streams') < len(command) - 1
        assert command[-1] == str(tmp_path / 'video')


@pytest.mark.parametrize('stream_count,accepted', [(12, True), (13, False)])
def test_real_ffprobe_stream_count_boundary(tmp_path, stream_count, accepted):
    file = tmp_path / 'audio.m4a'
    command = ['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'anullsrc=r=8000:cl=mono']
    for _ in range(stream_count):
        command.extend(['-map', '0:a'])
    command.extend(['-t', '0.05', '-c:a', 'aac', str(file)])
    subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=15, check=True)
    if accepted:
        assert inspect_file(file, file.name) == 'audio/mp4'
    else:
        with pytest.raises(HTTPException) as error:
            inspect_file(file, file.name)
        assert error.value.status_code == 415
