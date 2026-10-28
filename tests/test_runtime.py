# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
import io
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from pyrobolic import runtime
from pyrobolic.core import Process, common_arguments, engine_path
from pyrobolic.updater import UpdateControl


class RuntimeTests(unittest.TestCase):
    def test_youtube_detection(self):
        self.assertTrue(runtime.youtube_url("https://m.youtube.com/watch?v=x"))
        self.assertTrue(runtime.youtube_url("https://youtu.be/x"))
        self.assertFalse(runtime.youtube_url("https://youtube.com.evil.example/x"))

    def test_atomic_pair_and_failure_preserves_working_pair(self):
        def deno(target, control, progress):
            target.write_text("deno")
            target.chmod(0o755)
            return "deno 2.9.8"
        def engine(stage, progress, control):
            target = stage / "bin/yt-dlp"
            target.parent.mkdir()
            target.write_text("engine")
            target.chmod(0o755)
            return "2026.10.14"
        with tempfile.TemporaryDirectory() as directory, patch.object(runtime, "install_deno", side_effect=deno), \
                patch.object(runtime, "_update_engine", side_effect=engine), \
                patch.object(runtime, "_validate", return_value="yt_dlp_ejs-1.0"):
            root = Path(directory)
            runtime.update_support(root)
            first = runtime.installed_directory(root)
            self.assertEqual(engine_path(root), str(first / "yt-dlp"))
            self.assertIn(f"deno:{first / 'deno'}", common_arguments(engine_path(root)))
            with patch.object(runtime, "install_deno", side_effect=RuntimeError("offline")):
                with self.assertRaisesRegex(RuntimeError, "offline"):
                    runtime.update_support(root)
            self.assertEqual(runtime.installed_directory(root), first)
            self.assertEqual(len(list((root / "support").glob("release-*"))), 1)
            runtime.update_support(root)
            self.assertNotEqual(runtime.installed_directory(root), first)
            self.assertTrue((first / "deno").exists())
            current = runtime.installed_directory(root)
            control = UpdateControl()
            def cancel_validation(*args, **kwargs):
                control.cancel()
                return "yt_dlp_ejs-1.0"
            with patch.object(runtime, "_validate", side_effect=cancel_validation):
                with self.assertRaises(InterruptedError):
                    runtime.update_support(root, control=control)
            self.assertEqual(runtime.installed_directory(root), current)

    def test_cancellation_and_no_unnecessary_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            control = UpdateControl()
            control.cancel()
            with self.assertRaises(InterruptedError):
                runtime.update_support(root, control=control)
            self.assertTrue(control.done.is_set())
            self.assertFalse((root / "support").exists())
            with patch.object(runtime, "update_support") as install:
                runtime.ensure_support("https://example.org/video", Process(), root)
                install.assert_not_called()

    def test_deno_digest_checked_before_execution(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("deno", b"not executed")
        payload = archive.getvalue()
        release = {"tag_name": "v2.9.8", "assets": [{
            "name": "deno-x86_64-unknown-linux-gnu.zip", "digest": "sha256:" + "0" * 64,
            "browser_download_url": "https://github.com/denoland/deno/releases/download/v2.9.8/test.zip"}]}
        with tempfile.TemporaryDirectory() as directory, patch.object(runtime.platform, "machine", return_value="x86_64"), \
                patch.object(runtime.platform, "system", return_value="Linux"), \
                patch.object(runtime, "fetch", side_effect=[io.BytesIO(json.dumps(release).encode()), io.BytesIO(payload)]), \
                patch.object(runtime, "_validate") as execute:
            with self.assertRaisesRegex(RuntimeError, "checksum"):
                runtime.install_deno(Path(directory) / "deno", UpdateControl())
            execute.assert_not_called()

    def test_verified_archive_and_version_validation(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("deno", b"fixture executable")
        payload = archive.getvalue()
        release = {"tag_name": "v2.9.8", "assets": [{
            "name": "deno-x86_64-unknown-linux-gnu.zip", "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
            "browser_download_url": "https://github.com/denoland/deno/releases/download/v2.9.8/test.zip"}]}
        for version, succeeds in (("deno 2.9.8", True), ("deno 1.0.0", False), ("deno 2.9.7", False), ("unexpected executable", False)):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory, \
                    patch.object(runtime.platform, "machine", return_value="x86_64"), \
                    patch.object(runtime.platform, "system", return_value="Linux"), \
                    patch.object(runtime, "fetch", side_effect=[io.BytesIO(json.dumps(release).encode()), io.BytesIO(payload)]), \
                    patch.object(runtime, "_validate", return_value=version):
                target = Path(directory) / "deno"
                if succeeds:
                    self.assertEqual(runtime.install_deno(target, UpdateControl()), version)
                    self.assertEqual(target.read_bytes(), b"fixture executable")
                else:
                    with self.assertRaises(RuntimeError):
                        runtime.install_deno(target, UpdateControl())
