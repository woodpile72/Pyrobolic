# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest

from pyrobolic.core import Job, Queue, Store, staging_directory
from test_core import await_idle


@unittest.skipUnless(os.environ.get("PYROBOLIC_INTEGRATION") == "1", "Set PYROBOLIC_INTEGRATION=1")
class TransferMediaTests(unittest.TestCase):
    def test_real_pause_cancel_range_resume_and_verified_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "tone.wav"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440",
                            "-t", "4", str(source)], check=True)
            content = source.read_bytes()
            ranges = []
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass

                def do_HEAD(self):
                    self.reply(False)

                def do_GET(self):
                    self.reply(True)

                def reply(self, body):
                    start = int(self.headers.get("Range", "bytes=0-").split("=")[1].split("-")[0])
                    if self.headers.get("Range"):
                        ranges.append(start)
                    self.send_response(206 if start else 200)
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("Content-Length", str(len(content) - start))
                    self.send_header("Accept-Ranges", "bytes")
                    if start:
                        self.send_header("Content-Range", f"bytes {start}-{len(content)-1}/{len(content)}")
                    self.end_headers()
                    if body:
                        try:
                            self.wfile.write(content[start:])
                        except (BrokenPipeError, ConnectionResetError):
                            pass
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            queue = Queue(Store(root / "state"))
            job = Job(f"http://127.0.0.1:{server.server_port}/tone.wav", "Fixture", str(root / "output"),
                      filename="Fixture", include_id=False, metadata=False, rate_limit_kib=64)
            try:
                queue.add([job])
                deadline = time.monotonic() + 15
                while job.progress < 0.05 and job.id in queue.running and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertGreater(job.progress, 0.05, job.detail)
                # Pause only after data reaches disk, not just a progress event.
                def written_partial():
                    return any(path.stat().st_size > 0 for path in staging_directory(job).glob("*.part"))
                while not written_partial() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(written_partial())
                queue.pause_transfer(job.id, True)
                time.sleep(0.15)
                progress = job.progress
                time.sleep(0.4)
                self.assertEqual(job.progress, progress)
                queue.pause_transfer(job.id, False)
                time.sleep(0.2)
                queue.cancel(job.id)
                await_idle(queue)
                self.assertEqual(job.status, "Canceled")
                partials = list(staging_directory(job).glob("*.part"))
                self.assertTrue(partials)
                self.assertGreater(partials[0].stat().st_size, 0)
                restored = Store(queue.store.directory)
                queue = Queue(restored)
                job = restored.jobs[0]
                start = time.monotonic()
                queue.retry(job.id)
                await_idle(queue, 45)
                elapsed = time.monotonic() - start
                self.assertEqual(job.status, "Completed", job.detail)
                self.assertTrue(any(offset > 0 for offset in ranges), ranges)
                self.assertGreater(elapsed, 1, "Rate limit should make the remaining transfer measurable")
                self.assertEqual(hashlib.sha256(Path(job.files[0]).read_bytes()).digest(), hashlib.sha256(content).digest())
                streams = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json", job.files[0]]))["streams"]
                self.assertEqual(streams[0]["codec_name"], "pcm_s16le")
            finally:
                queue.shutdown()
                await_idle(queue)
                server.shutdown()
                server.server_close()
