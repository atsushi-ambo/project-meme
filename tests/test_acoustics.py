import unittest

from app.acoustics import loud_spans


class LoudSpansTests(unittest.TestCase):
    def test_silence_and_single_short_noise_are_not_candidates(self):
        self.assertEqual(loud_spans([0.0] * 30), [])
        self.assertEqual(loud_spans([0.0] * 10 + [0.5] + [0.0] * 10), [])

    def test_sound_at_recording_end_is_kept_without_extending_its_time(self):
        levels = [0.0] * 10 + [0.4, 0.5]
        self.assertEqual(loud_spans(levels), [
            {'start': 1.0, 'end': 1.2, 'peak_rms': 0.5},
        ])

    def test_brief_pause_is_joined_but_long_pause_separates_candidates(self):
        levels = ([0.0] * 10 + [0.4] * 2 + [0.0] * 3 + [0.5] * 2
                  + [0.0] * 4 + [0.3] * 2 + [0.0] * 10)
        self.assertEqual(loud_spans(levels), [
            {'start': 1.0, 'end': 1.7, 'peak_rms': 0.5},
            {'start': 2.1, 'end': 2.3, 'peak_rms': 0.3},
        ])

    def test_limit_keeps_loudest_candidates_in_time_order(self):
        levels = ([0.0] * 10 + [0.6] * 2 + [0.0] * 10 + [0.2] * 2
                  + [0.0] * 10 + [0.8] * 2 + [0.0] * 10)
        self.assertEqual(loud_spans(levels, limit=2), [
            {'start': 1.0, 'end': 1.2, 'peak_rms': 0.6},
            {'start': 3.4, 'end': 3.6, 'peak_rms': 0.8},
        ])
