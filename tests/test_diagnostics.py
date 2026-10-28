# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from pyrobolic.core import Process
from pyrobolic.diagnostics import TOOLS, check_dependencies


class DiagnosticsTests(unittest.TestCase):
    def test_paths_arguments_and_version_only(self):
        processes = [Mock() for _ in TOOLS]
        for process in processes:
            process.cancelled.is_set.return_value = False
            process.communicate.return_value = "version 1.2\nExtra build details\n"
        with patch("pyrobolic.diagnostics.engine_path", return_value="/managed/yt-dlp"), \
                patch("pyrobolic.diagnostics.shutil.which", side_effect=lambda name: "/system/" + name):
            results = check_dependencies(Path("/unused"), processes)
        self.assertEqual([item.name for item in results], list(TOOLS))
        self.assertTrue(all(item.status == "Available" and item.version == "version 1.2" for item in results))
        processes[0].communicate.assert_called_once_with(["/managed/yt-dlp", "--ignore-config", "--version"], timeout=5)
        processes[1].communicate.assert_called_once_with(["/system/ffmpeg", "-version"], timeout=5)
        processes[2].communicate.assert_called_once_with(["/system/ffprobe", "-version"], timeout=5)
        processes[3].communicate.assert_called_once_with(["/system/deno", "--version"], timeout=5)

    def test_missing_tools_do_not_start_processes(self):
        processes = [Process() for _ in TOOLS]
        with patch("pyrobolic.diagnostics.engine_path", side_effect=RuntimeError("missing")), \
                patch("pyrobolic.diagnostics.shutil.which", return_value=None):
            results = check_dependencies(Path("/unused"), processes)
        self.assertTrue(all(item.status == "Not Found" and item.detail for item in results))
        self.assertTrue(all(process.process is None for process in processes))

    def test_failure_categories_do_not_echo_raw_errors(self):
        processes = [Mock() for _ in TOOLS]
        for process, error in zip(processes, (PermissionError("secret"), subprocess.TimeoutExpired("secret", 5),
                                              subprocess.CalledProcessError(1, "secret", output="secret"), None)):
            process.cancelled.is_set.return_value = False
            process.communicate.side_effect = error
            process.communicate.return_value = ""
        with patch("pyrobolic.diagnostics.engine_path", return_value="/fixture"), \
                patch("pyrobolic.diagnostics.shutil.which", return_value="/fixture"):
            results = check_dependencies(Path("/unused"), processes)
        self.assertEqual([item.status for item in results], ["Could Not Run", "Timed Out", "Could Not Run", "No Version Reported"])
        self.assertNotIn("secret", str(results))

    def test_real_timeout_and_cancellation_reap_probes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "probe"
            path.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n", encoding="utf-8")
            path.chmod(0o700)
            with patch("pyrobolic.diagnostics.engine_path", return_value=str(path)), \
                    patch("pyrobolic.diagnostics.shutil.which", return_value=None):
                processes = [Process() for _ in TOOLS]
                results = check_dependencies(Path(directory), processes, timeout=0.1)
                self.assertEqual(results[0].status, "Timed Out")
                self.assertTrue(processes[0].done.is_set())
                self.assertIsNotNone(processes[0].process.poll())
                canceled = [Process() for _ in TOOLS]
                errors = []
                def run():
                    try:
                        check_dependencies(Path(directory), canceled)
                    except InterruptedError:
                        errors.append("canceled")
                thread = threading.Thread(target=run)
                thread.start()
                try:
                    deadline = time.monotonic() + 3
                    while canceled[0].process is None and time.monotonic() < deadline:
                        time.sleep(0.01)
                    for process in canceled:
                        process.cancel()
                    thread.join(5)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(errors, ["canceled"])
                    self.assertTrue(canceled[0].done.is_set())
                finally:
                    for process in canceled:
                        process.cancel()
                        process.close()
                    thread.join(5)
