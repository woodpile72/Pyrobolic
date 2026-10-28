# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from dataclasses import replace
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import threading
from unittest.mock import patch

from pyrobolic.core import Job, Settings, Store, Queue, download_arguments
from pyrobolic.sponsorblock import arguments, validate, verify, intervals
from pyrobolic.disclosure import download_summary


class SponsorTests(unittest.TestCase):
    def test_defaults_summary_and_rejected_combinations(self):
        options = Settings()
        self.assertEqual(arguments(options), ["--no-sponsorblock"])
        enabled = replace(options, sponsorblock="remove")
        self.assertIn("SponsorBlock: Remove Segments", download_summary(enabled))
        for changes in ({"subtitle_mode": "sidecar"}, {"split_chapters": True}, {"sponsorblock_categories": "bogus"}):
            with self.assertRaises(ValueError):
                validate(replace(enabled, **changes))

    def test_missing_segments_and_failed_removal(self):
        options = replace(Settings(), sponsorblock="remove")
        self.assertIn("no matching", verify(options, {"sponsorblock_chapters": []}, [], None)[0])
        report = {"duration": 10, "sponsorblock_chapters": [{"start_time": 2, "end_time": 4}]}
        with self.assertRaisesRegex(RuntimeError, "expected duration"):
            verify(options, report, ["fixture"], lambda *args: json.dumps({"format": {"duration": "10"}}))
        self.assertEqual(verify(options, report, ["fixture"], lambda *args: json.dumps({"format": {"duration": "8"}})), [])
        with self.assertRaises(RuntimeError):
            intervals(None)

    def test_persistence_and_per_job_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            store.settings.sponsorblock = "mark"
            store.settings.sponsorblock_categories = "sponsor,intro"
            store.save()
            recovered = Store(Path(directory))
            self.assertEqual(recovered.settings.sponsorblock, "mark")
            self.assertEqual(recovered.settings.sponsorblock_categories, "sponsor,intro")


@unittest.skipUnless(os.environ.get("PYROBOLIC_INTEGRATION") == "1", "Set PYROBOLIC_INTEGRATION=1")
class SponsorMediaTests(unittest.TestCase):
    def test_real_mark_remove_off_and_missing_segments(self):
        import time
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24",
                            "-t", "8", "-c:v", "libx264", "-threads", "1", str(root / "source.mp4")], check=True)
            requests = []
            service = {"mode": "segments"}
            class Handler(SimpleHTTPRequestHandler):
                def log_message(self, *args):
                    pass
                def do_GET(self):
                    if self.path.startswith("/api/"):
                        requests.append(self.path)
                        if service["mode"] == "failure":
                            self.send_error(503)
                            return
                        data = json.dumps([{"videoID": "fixture", "segments": [{"segment": [2, 4], "category": "sponsor", "actionType": "skip", "videoDuration": 8}]}]).encode()
                        if service["mode"] == "missing":
                            data = b"[]"
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(data)
                    else:
                        super().do_GET()
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=directory))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                info = root / "fixture.json"
                info.write_text(json.dumps({"id": "fixture", "title": "Fixture", "duration": 8,
                    "extractor": "youtube", "extractor_key": "Youtube", "webpage_url": base + "/source.mp4",
                    "formats": [{"format_id": "test", "url": base + "/source.mp4", "ext": "mp4", "vcodec": "h264", "acodec": "none"}]}))
                store = Store(root / "state")
                queue = Queue(store)
                engine = os.environ.get("PYROBOLIC_TEST_ENGINE", "/usr/bin/yt-dlp")
                def command(job, executable):
                    return download_arguments(job, engine)[:-2] + ["--sponsorblock-api", base, "--load-info-json", str(info)]
                with patch("pyrobolic.core.download_arguments", side_effect=command):
                    for mode, state in (("off", "segments"), ("mark", "segments"), ("remove", "segments"), ("remove", "missing"), ("remove", "failure")):
                        service["mode"] = state
                        before = len(requests)
                        job = Job(base + "/source.mp4", mode, str(root / "output"), filename=mode + state,
                                  include_id=False, metadata=False, sponsorblock=mode, source_format="test")
                        queue.add([job])
                        deadline = time.monotonic() + 60
                        while job.status not in {"Completed", "Failed"} and time.monotonic() < deadline:
                            time.sleep(.05)
                        self.assertEqual(job.status, "Failed" if state == "failure" else "Completed", job.detail + "\n" + "\n".join(job.log))
                        if state == "missing":
                            self.assertTrue(any("no matching" in warning for warning in job.warnings))
                        if state == "failure":
                            self.assertFalse(list((root / "output").glob("removefailure*")))
                        self.assertEqual(len(requests) > before, mode != "off")
            finally:
                server.shutdown()
                server.server_close()
