# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pyrobolic.updater import UpdateControl, install_verified, update_engine


class UpdateCancelTests(unittest.TestCase):
    def test_cancel_during_transfer_preserves_engine_and_removes_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "yt-dlp"
            target.write_bytes(b"working engine")
            content = b"replacement" * 30000
            control = UpdateControl()
            with self.assertRaises(InterruptedError):
                install_verified(io.BytesIO(content), target, hashlib.sha256(content).hexdigest(),
                                 progress=lambda _: control.cancel(), validate=False, control=control)
            self.assertEqual(target.read_bytes(), b"working engine")
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_cancel_before_network_and_completion_signal(self):
        control = UpdateControl()
        control.cancel()
        with patch("pyrobolic.updater.fetch") as fetch, self.assertRaises(InterruptedError):
            update_engine(control=control)
        fetch.assert_not_called()
        self.assertTrue(control.done.is_set())

    def test_late_cancel_cannot_misreport_successful_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "yt-dlp"
            control = UpdateControl()
            content = b"fixture engine"
            install_verified(io.BytesIO(content), target, hashlib.sha256(content).hexdigest(),
                             validate=False, control=control)
            control.cancel()
            self.assertTrue(control.committed)
            self.assertFalse(control.canceled.is_set())
