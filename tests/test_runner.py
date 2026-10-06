"""テスト実行が失敗・中断しても専用環境だけを片付けることを確認。"""
from pathlib import Path
import runpy
from types import SimpleNamespace
import pytest


@pytest.mark.parametrize('failure,expected', [(0, 0), (7, 7), ('interrupt', 130), ('health', 9), ('restore', 8)])
def test_runner_isolation_and_cleanup(monkeypatch, failure, expected):
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/test.py'))
    calls = []
    monkeypatch.setenv('COMPOSE_FILE', 'compose.yaml')
    def fake_run(command, **kwargs):
        calls.append(command)
        assert 'COMPOSE_FILE' not in kwargs['env']
        if 'run' in command:
            if failure == 'interrupt': raise KeyboardInterrupt()
            return SimpleNamespace(returncode=0 if failure in ('health', 'restore') else failure)
        if 'up' in command and failure == 'health':
            return SimpleNamespace(returncode=9)
        if 'exec' in command and 'db' in command and failure == 'restore':
            return SimpleNamespace(returncode=8)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(module['subprocess'], 'run', fake_run)
    assert module['main']() == expected
    run, *middle, cleanup, image = calls
    assert len(middle) == (3 if failure in (0, 'restore') else 1 if failure == 'health' else 0)
    if middle:
        assert '--wait' in middle[0] and middle[0][-1] == 'web'
    project = run[run.index('-p') + 1]
    assert project.startswith('project-meme-test-')
    assert run[run.index('--env-file') + 1] == module['os'].devnull
    assert Path(run[run.index('-f') + 1]).name == 'compose.test.yaml'
    assert '--build' in run and run[-1] == 'tests'
    assert cleanup[cleanup.index('-p') + 1] == project
    assert 'down' in cleanup and '--volumes' in cleanup
    assert image == ['docker', 'image', 'rm', project + ':test']
