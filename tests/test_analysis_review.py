"""私用の閲覧資料で外部入力を実行せず、確認漏れを断定しない。"""
from pathlib import Path
import runpy

MODULE = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/analysis_review.py'))


def test_review_escapes_notes_and_uses_only_generated_local_filenames(tmp_path):
    for name in ['frame-000.jpg', 'near-001.00s.jpg', 'audio.wav']:
        (tmp_path / name).write_bytes(b'test')
    report = {'observation_id': 1, 'media_id': 2, 'original_name': '<img src=https://example.test/x>',
              'duration_seconds': 3, 'memo': '</pre><script>alert(1)</script>', 'audio_file': 'audio.wav',
              'frames': ['frame-000.jpg', '../secret.jpg', 'https://example.test/p.jpg'],
              'loud_candidates': [{'start': 1, 'end': 2, 'frames': ['near-001.00s.jpg', 'x" onerror="x.jpg']},
                                  {'start': float('nan'), 'end': 3}],
              'saved_segments': [], 'source_omitted': {'sound_notes': 1}}
    MODULE['write_review'](tmp_path, report)
    html = (tmp_path / 'review.html').read_text()
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert '<img src=https://' not in html and '&lt;img src=' in html
    assert '../secret.jpg' not in html and 'src="https://' not in html
    assert 'src="frame-000.jpg"' in html and 'src="near-001.00s.jpg"' in html
    assert '1秒の静止画' in html and '未確認' in html
    assert '関連情報の省略があります' in html
    assert 'audio.wav#t=1,2' in html and '候補 2' not in html
    assert '候補 1件は' in html and 'sound_notes：1件' in html
    assert '画像 2件は' in html
    assert 'form-action' in html and '<form' not in html
    assert '<script>' not in (tmp_path / 'review.md').read_text()


def test_review_without_audio_or_candidates_does_not_claim_no_calls(tmp_path):
    MODULE['write_review'](tmp_path, {'observation_id': 1, 'media_id': 2, 'original_name': 'test.mov',
        'duration_seconds': 1, 'memo': '', 'frames': [], 'audio_file': None, 'loud_candidates': [],
        'saved_segments': []})
    html = (tmp_path / 'review.html').read_text()
    assert '<audio' not in html
    assert '声や行動がなかったという意味ではありません' in html
    assert '鳴いていなかったという判断には使えません' in html


def test_review_reports_provenance_and_missing_media(tmp_path):
    MODULE['write_review'](tmp_path, {'observation_id': 1, 'media_id': 2, 'original_name': 'test.mov',
        'duration_seconds': 1, 'memo': '', 'frames': ['frame-000.jpg'], 'audio_file': 'audio.wav',
        'loud_candidates': [], 'saved_segments': [], 'prepared_at': '2026-10-30T00:00:00Z',
        'observation_occurred_at': '2026-10-29T08:00:00+09:00', 'observation_revision': 3})
    html = (tmp_path / 'review.html').read_text(encoding='utf-8')
    assert '元記録の版：3' in html and '2026-10-30T00:00:00Z' in html
    assert '2026-10-29T08:00:00+09:00' in html
    assert '画像 1件は' in html and '<audio' not in html


def test_review_keeps_model_candidates_distinct_from_loudness(tmp_path):
    (tmp_path / 'audio.wav').write_bytes(b'test')
    MODULE['write_review'](tmp_path, {'observation_id': 1, 'media_id': 2, 'original_name': 'test.mov',
        'duration_seconds': 2, 'memo': '', 'frames': [], 'audio_file': 'audio.wav',
        'loud_candidates': [], 'saved_segments': [], 'cat_detector': {'available': True},
        'cat_candidates': [{'start': 0.5, 'end': 1.5, 'kind_label': '<script>種類</script>', 'speech': True}]})
    html = (tmp_path / 'review.html').read_text(encoding='utf-8')
    assert 'モデル候補 1' in html and '&lt;script&gt;種類&lt;/script&gt;' in html
    assert '<script>' not in html and '人の声が混ざっている可能性' in html


def test_review_model_state_and_long_interval_are_disclosed(tmp_path):
    report = {'observation_id': 1, 'media_id': 2, 'original_name': 'test.mov',
        'duration_seconds': 12, 'memo': '', 'frames': [], 'audio_file': None,
        'loud_candidates': [], 'saved_segments': [], 'cat_detector': {'available': False},
        'cat_candidates': [{'start': 0, 'end': 13}]}
    MODULE['write_review'](tmp_path, report)
    html = (tmp_path / 'review.html').read_text(encoding='utf-8')
    assert 'モデル候補 1' not in html and 'この資料では実行されていません' in html
    report['cat_detector']['available'] = True
    MODULE['write_review'](tmp_path, report)
    html = (tmp_path / 'review.html').read_text(encoding='utf-8')
    assert 'モデル候補 1：0〜12秒' in html
    assert '表示は動画の終わりまで' in html and '10秒を超える' in html
