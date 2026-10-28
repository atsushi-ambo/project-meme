"""Synthetic-only checks; run with python -m unittest discover -s experiments/pysr."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

import pysr_experiment as experiment


class DemoTest(unittest.TestCase):
    def test_meals_reset_elapsed_time_and_seed_is_repeatable(self):
        frame = experiment.build_demo_frame(n_days=3, seed=42)
        self.assertTrue(frame.equals(experiment.build_demo_frame(n_days=3, seed=42)))
        self.assertEqual(len(frame), 72)
        self.assertTrue((frame.loc[frame.food_this_hour == 1, 'hours_since_food'] == 0).all())
        self.assertTrue(np.isfinite(frame.to_numpy()).all())

    def test_real_input_arguments_rejected_before_read(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'private.json'
            marker.write_text('must not be read')
            for option in ('--records', '--csv'):
                with self.subTest(option=option):
                    result = subprocess.run([sys.executable, str(Path(experiment.__file__)), option, str(marker)],
                                            capture_output=True, text=True, cwd=directory, timeout=10)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('unrecognized arguments', result.stderr)
                    self.assertNotIn('must not be read', result.stderr)
            self.assertEqual(list(Path(directory).iterdir()), [marker])

    def test_main_uses_a_temporary_output_directory(self):
        captured = []
        def fake_run(frame, iterations, seed, output_directory):
            self.assertTrue(os.path.isdir(output_directory))
            captured.append(output_directory)
        with patch.object(sys, 'argv', ['pysr_experiment.py', '--demo']), patch.object(experiment, 'run_pysr', fake_run):
            experiment.main()
        self.assertEqual(len(captured), 1)
        self.assertFalse(os.path.exists(captured[0]))


if __name__ == '__main__':
    unittest.main()
