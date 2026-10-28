# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
from functools import partial
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

from pyrobolic.batch import BatchInspection, edited_entry, export_m3u, inspect_batch_url, numbered_name, parse_batch
from pyrobolic.core import Job, Queue, Store
from test_core import await_idle
from test_integration import QuietHandler


@unittest.skipUnless(os.environ.get("PYROBOLIC_INTEGRATION") == "1", "Set PYROBOLIC_INTEGRATION=1")
class BatchMediaTests(unittest.TestCase):
    def test_corrected_entry_downloads_without_reinspecting_successes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440",
                            "-t", "1", str(root / "tone.wav")], check=True)
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=directory))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            queue = None
            try:
                url = f"http://127.0.0.1:{server.server_port}/tone.wav"
                rows = parse_batch(f"{url}\tFirst\t{root}/first\ninvalid\tFixed\t{root}/fixed")
                BatchInspection().run(rows, root / "state")
                first_items = rows[0].items
                self.assertTrue(first_items and rows[1].error)
                rows[1] = edited_entry(rows, rows[1], url, rows[1].name, rows[1].destination)
                calls = []
                def inspect(url, process, directory):
                    calls.append(url)
                    return inspect_batch_url(url, process, directory)
                BatchInspection().run(rows, root / "state", inspect=inspect)
                self.assertEqual(calls, [url])
                self.assertIs(rows[0].items, first_items)
                collision = root / "fixed" / "Fixed.opus"
                collision.parent.mkdir()
                collision.write_bytes(b"keep existing file")
                jobs = [Job(item["url"], item["title"], item["destination"], filename=item["import_name"],
                            include_id=False, mode="audio", audio_format="opus", metadata=False)
                        for entry in rows for item in entry.items]
                store = Store(root / "state")
                queue = Queue(store)
                queue.add(jobs)
                await_idle(queue, 45)
                self.assertTrue(all(job.status == "Completed" for job in jobs), [j.detail for j in jobs])
                self.assertEqual(Path(jobs[1].files[0]).name, "Fixed (2).opus")
                self.assertEqual(collision.read_bytes(), b"keep existing file")
                for job in jobs:
                    self.assertGreater(Path(job.files[0]).stat().st_size, 0)
                    streams = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams",
                                                                 "-of", "json", job.files[0]]))["streams"]
                    self.assertEqual([s["codec_name"] for s in streams], ["opus"])
                saved = Store(store.directory)
                self.assertEqual([j.filename for j in saved.jobs], ["First", "Fixed"])
                self.assertEqual([j.files for j in saved.jobs], [j.files for j in jobs])
            finally:
                if queue:
                    queue.shutdown()
                server.shutdown()
                server.server_close()

    def test_real_inspection_download_collisions_and_playlist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "first.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24",
                            "-f", "lavfi", "-i", "sine=frequency=440", "-t", "1",
                            "-c:v", "libx264", "-c:a", "aac", str(source)], check=True)
            shutil.copyfile(source, root / "second.mp4")
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=directory))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            queue = None
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                rows = parse_batch(f"{base}/first.mp4\tFirst\t{root}/media\n"
                                   f"{base}/second.mp4\tSecond\t{root}/audio\n"
                                   f"{base}/missing.mp4\ninvalid\n{base}/first.mp4\tFirst\t{root}/media")
                BatchInspection().run(rows, root / "state")
                self.assertEqual([bool(r.items) for r in rows], [True, True, False, False, False])
                self.assertTrue(all(rows[i].error for i in (2, 3, 4)))
                items = [r.items[0] for r in rows if r.items][::-1]
                jobs = [Job(item["url"], item["title"], item["destination"],
                            filename=numbered_name(item["import_name"], index, len(items)), include_id=False,
                            mode="audio" if index == 1 else "video", audio_format="mp3", metadata=False)
                        for index, item in enumerate(items, 1)]
                collision = root / "media" / "02 - First.mp4"
                collision.parent.mkdir()
                collision.write_bytes(b"existing file")
                store = Store(root / "state")
                queue = Queue(store)
                queue.add(jobs)
                await_idle(queue, 45)
                self.assertEqual([j.status for j in jobs], ["Completed", "Completed"], [j.log for j in jobs])
                self.assertEqual(collision.read_bytes(), b"existing file")
                self.assertEqual(Path(jobs[1].files[0]).name, "02 - First (2).mp4")
                streams = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams",
                                                               "-of", "json", jobs[0].files[0]]))["streams"]
                self.assertEqual([s["codec_name"] for s in streams], ["mp3"])
                def video_hash(path):
                    return subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v",
                                                    "-c", "copy", "-f", "hash", "-"], text=True)
                self.assertEqual(video_hash(source), video_hash(jobs[1].files[0]))
                playlist = root / "Batch.m3u8"
                self.assertEqual(export_m3u(Store(store.directory).jobs, playlist), 2)
                paths = [(root / line).resolve() for line in playlist.read_text().splitlines()
                         if not line.startswith("#")]
                self.assertEqual(paths, [Path(j.files[0]) for j in jobs])
                self.assertTrue(all(path.stat().st_size for path in paths))
            finally:
                if queue:
                    queue.shutdown()
                server.shutdown()
                server.server_close()
