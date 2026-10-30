"""AIのCLIへ渡す資料・コマンド・確認・保存を検証する。実際のCLIは呼ばない。"""
from pathlib import Path
import runpy
import subprocess

import pytest


@pytest.fixture
def module(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / 'scripts'))
    mod = runpy.run_path(str(root / 'scripts/ai_review.py'))
    base = tmp_path / 'analysis'
    base.mkdir()
    for name in ('bundle_path', 'run_review', 'build_command'):
        monkeypatch.setitem(mod[name].__globals__, 'BASE', base)
    monkeypatch.setattr('shutil.which', lambda tool: f'/usr/bin/{tool}')
    return mod, base


def bundle(base, name='observation-1-media-2-abc'):
    path = base / name
    path.mkdir()
    for file in ('report.json', 'spectrogram.png', 'audio.wav', 'frame-000.jpg', 'near-001.25s.jpg'):
        (path / file).write_text('{}' if file.endswith('.json') else 'x')
    return path


class Runner:
    def __init__(self, stdout='# 動画の観察下書き\n本文', returncode=0, help_text='--restricted --no-session-persistence'):
        self.calls, self.stdout, self.returncode, self.help_text = [], stdout, returncode, help_text

    def __call__(self, command, **kwargs):
        if command[:2] == ['claude', '--help']:
            return subprocess.CompletedProcess(command, 0, self.help_text, '')
        self.calls.append((command, kwargs))
        staged = Path(kwargs['cwd'])
        self.seen = sorted(p.name for p in staged.iterdir())
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, 'usage limit reached')


def test_bundle_must_be_a_private_analysis_folder(module, tmp_path):
    mod, base = module
    good = bundle(base)
    assert mod['bundle_path'](good) == good.resolve()
    outside = tmp_path / 'elsewhere'
    outside.mkdir()
    (outside / 'report.json').write_text('{}')
    link = base / 'link'
    link.symlink_to(outside)
    for path in (outside, link, base, base / 'missing'):
        with pytest.raises(ValueError):
            mod['bundle_path'](path)


def test_only_report_and_images_are_sent_after_confirmation(module):
    mod, base = module
    path = bundle(base)
    runner = Runner()
    target = mod['run_review'](path, 'claude', confirm=lambda _: 'y', runner=runner, out=lambda *_: None)
    [(command, kwargs)] = runner.calls
    assert runner.seen == ['frame-000.jpg', 'near-001.25s.jpg', 'report.json', 'spectrogram.png']
    assert 'audio.wav' not in ' '.join(command)
    assert command[:2] == ['claude', '-p'] and '--restricted' in command and '--no-session-persistence' in command
    assert command[command.index('--tools') + 1] == 'Read,Glob'
    assert 'Read(./**)' in command and command[command.index('--permission-mode') + 1] == 'dontAsk'
    assert 'あなたは音を聞けません' in command[2] and 'tail_up' in command[2]
    assert target.read_text().startswith('<!-- claude による下書き') and target.stat().st_mode & 0o777 == 0o600
    assert not (path / 'ai-input').exists()


def test_older_claude_without_restricted_mode_still_limits_reads(module):
    mod, base = module
    runner = Runner(help_text='')
    mod['run_review'](bundle(base), 'claude', confirm=lambda _: 'y', runner=runner, out=lambda *_: None)
    command = runner.calls[0][0]
    assert '--restricted' not in command and 'Read(./**)' in command


def test_codex_is_read_only_and_gets_images(module):
    mod, base = module
    runner = Runner()
    mod['run_review'](bundle(base), 'codex', confirm=lambda _: 'yes', runner=runner, out=lambda *_: None)
    command = runner.calls[0][0]
    assert command[:5] == ['codex', 'exec', '--sandbox', 'read-only', '--skip-git-repo-check']
    assert sum(1 for part in command if part == '-i') == 3 and not any(part.endswith('.wav') for part in command)


def test_declining_sends_nothing_and_failures_save_nothing(module):
    mod, base = module
    path = bundle(base)
    runner = Runner()
    assert mod['run_review'](path, 'claude', confirm=lambda _: 'n', runner=runner, out=lambda *_: None) is None
    assert runner.calls == []
    failing = Runner(stdout='', returncode=1)
    with pytest.raises(ValueError, match='使用量の上限'):
        mod['run_review'](path, 'codex', confirm=lambda _: 'y', runner=failing, out=lambda *_: None)
    assert not list(path.glob('ai-review-*')) and not (path / 'ai-input').exists()
