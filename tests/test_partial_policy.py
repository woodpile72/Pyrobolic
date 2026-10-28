# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
from dataclasses import replace
from functools import partial
from http.server import ThreadingHTTPServer
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest

from pyrobolic.core import Job, Queue, Settings, Store, download_arguments, staging_directory
from test_core import await_idle
from test_integration import QuietHandler


class PartialPolicyTests(unittest.TestCase):
    def test_overwrite_scope_validation_and_persistence(self):
        job = Job("https://example.org/fixture", "Fixture", "/tmp", filename="Fixture", use_part_files=False)
        with self.assertRaises(ValueError):
            download_arguments(job, "yt-dlp")
        job.output_stem = "Fixture"
        args = download_arguments(job, "yt-dlp")
        self.assertIn("--no-part", args)
        self.assertIn("--no-continue", args)
        self.assertIn("--force-overwrites", args)
        self.assertIn("/.pyrobolic/", args[args.index("--paths") + 1])
        self.assertIn("--part", download_arguments(replace(job, use_part_files=True), "yt-dlp"))
        with self.assertRaises(ValueError):
            Settings(use_part_files="false")
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            store.paused = True
            settings = Settings(use_part_files=False)
            job = replace(job, output=directory)
            Queue(store).add([job], settings)
            restored = Store(store.directory)
            self.assertFalse(restored.settings.use_part_files)
            restored.settings.use_part_files = True
            Queue(restored).retry(job.id)
            self.assertFalse(restored.jobs[0].use_part_files)


@unittest.skipUnless(os.environ.get("PYROBOLIC_INTEGRATION") == "1", "Set PYROBOLIC_INTEGRATION=1")
class PartialPolicyMediaTests(unittest.TestCase):
    def test_no_part_cancel_restart_does_not_publish_partial_or_overwrite_existing_media(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "tone.wav"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440",
                            "-t", "4", str(source)], check=True)
            content = source.read_bytes()
            requests = []
            class Handler(QuietHandler):
                def do_GET(self):
                    requests.append(self.headers.get("Range"))
                    super().do_GET()
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=directory))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            output = root / "output"
            output.mkdir()
            existing = output / "Fixture.wav"
            existing.write_bytes(b"existing media")
            queue = Queue(Store(root / "state"))
            job = Job(f"http://127.0.0.1:{server.server_port}/tone.wav", "Fixture", str(output),
                      filename="Fixture", include_id=False, metadata=False, use_part_files=False, rate_limit_kib=128)
            try:
                queue.add([job])
                deadline = time.monotonic() + 15
                while job.progress < 0.05 and job.id in queue.running and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertGreater(job.progress, 0.05, job.detail)
                queue.cancel(job.id)
                await_idle(queue)
                self.assertEqual(job.status, "Canceled")
                stage = staging_directory(job)
                self.assertFalse(list(stage.glob("*.part")))
                partial_file = stage / "media.wav"
                self.assertTrue(partial_file.is_file())
                self.assertGreater(partial_file.stat().st_size, 0)
                self.assertLess(partial_file.stat().st_size, len(content))
                self.assertEqual(list(output.glob("*.wav")), [existing])
                before = len(requests)
                restored = Store(queue.store.directory)
                queue = Queue(restored)
                job = restored.jobs[0]
                queue.retry(job.id)
                await_idle(queue, 30)
                self.assertEqual(job.status, "Completed", job.log)
                self.assertGreater(len(requests), before)
                self.assertTrue(all(value is None for value in requests[before:]), requests[before:])
                self.assertEqual(hashlib.sha256(Path(job.files[0]).read_bytes()).digest(), hashlib.sha256(content).digest())
                self.assertEqual(existing.read_bytes(), b"existing media")
                self.assertNotEqual(Path(job.files[0]), existing)
            finally:
                queue.shutdown()
                await_idle(queue)
                server.shutdown()
                server.server_close()
