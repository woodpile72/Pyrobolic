# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
import json
import unittest
from unittest.mock import patch

from pyrobolic.core import ProgressDisplay, progress_update


class ProgressDisplayTests(unittest.TestCase):
    def setUp(self):
        self.display = ProgressDisplay()

    def update(self, now, **values):
        data = dict(status="downloading", filename="fixture", downloaded_bytes=25, total_bytes=100)
        data.update(values)
        with patch("pyrobolic.core.time.monotonic", return_value=now):
            return progress_update("PYRO_PROGRESS:" + json.dumps(data), self.display)

    def test_missing_estimates_do_not_collapse_label(self):
        first = self.update(0, speed=1048576, eta=90)
        self.assertEqual(self.update(1, speed=None, eta=None)["detail"], first["detail"])
        self.assertEqual(self.update(6)["detail"], "25%  ·  -- MiB/s  ·  --:-- remaining")

    def test_text_is_throttled_but_bar_is_current(self):
        first = self.update(0, speed=1048576, eta=90)
        next_update = self.update(.3, downloaded_bytes=40, speed=2097152, eta=60)
        self.assertEqual(next_update["detail"], first["detail"])
        self.assertEqual(next_update["progress"], .4)
        self.assertEqual(self.update(1, downloaded_bytes=40)["detail"], "40%  ·  2.0 MiB/s  ·  1:00 remaining")

    def test_stream_and_phase_changes_clear_estimates(self):
        self.update(0, speed=1048576, eta=90)
        self.assertIn("-- MiB/s", self.update(.1, filename="audio")["detail"])
        self.update(1, speed=1048576, eta=90)
        self.assertEqual(self.update(2, status="finished")["detail"], "Preparing media")
        self.assertIn("-- MiB/s", self.update(3)["detail"])
        self.update(4, speed=1048576, eta=90)
        self.assertEqual(progress_update("PYRO_PROCESS:processing", self.display)["status"], "Processing")
        self.assertFalse(self.display.estimates)

    def test_zero_speed_and_invalid_estimates(self):
        self.assertIn("0.0 MiB/s", self.update(0, speed=0, eta=0)["detail"])
        self.assertIn("-- MiB/s", self.update(6, speed=-1, eta="unknown")["detail"])
