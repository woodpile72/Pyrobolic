# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
from dataclasses import replace
from functools import partial
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from pyrobolic.core import Job, Queue, Settings, Store, download_arguments
from pyrobolic.ranges import validate_range
from test_core import await_idle
from test_integration import QuietHandler


class RangeTests(unittest.TestCase):
    def test_validation_and_per_job_persistence(self):
        for values in ((-1, None, False), (0, 0, False), (2, 1, False), (float("nan"), None, False),
                       (0, float("inf"), False), (False, 2, False), (0, 2, "yes")):
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_range(*values)
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            store.paused = True
            job = Job("https://example.org/fixture", "Fixture", directory, clip_start=1.25, clip_end=3.5, precise_cuts=True)
            queue = Queue(store)
            queue.add([job])
            restored = Store(store.directory)
            Queue(restored).retry(job.id)
            self.assertEqual((restored.jobs[0].clip_start, restored.jobs[0].clip_end), (1.25, 3.5))
            self.assertFalse(hasattr(Settings(), "clip_start"))
            args = download_arguments(restored.jobs[0], "yt-dlp")
            self.assertEqual(args[args.index("--download-sections") + 1], "*1.25-3.5")
            self.assertIn("--force-keyframes-at-cuts", args)
            self.assertNotIn("--download-sections", download_arguments(replace(job, clip_start=0, clip_end=None), "yt-dlp"))

    def test_audio_quality_is_output_policy_not_source_selection(self):
        job = Job("https://example.org/fixture", "Fixture", "/tmp", mode="audio", audio_format="mp3",
                  audio_encoding_quality="192K", source_format="exact", preferred_abr=64)
        args = download_arguments(job, "yt-dlp")
        self.assertEqual(args[args.index("-f") + 1], 'best*[format_id="exact"]')
        self.assertEqual(args[args.index("--audio-quality") + 1], "192K")
        self.assertNotIn("--format-sort", args)
        self.assertNotIn("--audio-quality", download_arguments(replace(job, audio_format="best"), "yt-dlp"))


@unittest.skipUnless(os.environ.get("PYROBOLIC_INTEGRATION") == "1", "Set PYROBOLIC_INTEGRATION=1")
class RangeMediaTests(unittest.TestCase):
    def test_precise_video_range_and_audio_encoder_bitrate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24",
                            "-f", "lavfi", "-i", "sine=frequency=440", "-t", "6", "-c:v", "libx264", "-g", "24",
                            "-c:a", "aac", "-movflags", "+faststart", str(root / "fixture.mp4")], check=True)
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440", "-t", "3",
                            str(root / "tone.wav")], check=True)
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=directory))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            queue = Queue(Store(root / "state"))
            base = f"http://127.0.0.1:{server.server_port}"
            clip = Job(base + "/fixture.mp4", "Clip", str(root / "output"), filename="Clip", metadata=False,
                       clip_start=1.5, clip_end=3.5, precise_cuts=True, include_id=False)
            audio = Job(base + "/tone.wav", "Audio", str(root / "output"), filename="Audio", metadata=False,
                        mode="audio", audio_format="mp3", audio_encoding_quality="192K", include_id=False)
            try:
                queue.add([clip, audio])
                await_idle(queue, 45)
                self.assertEqual([j.status for j in (clip, audio)], ["Completed", "Completed"], [j.detail for j in (clip, audio)])
                def probe(job):
                    return json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", job.files[0]]))
                output = probe(clip)
                self.assertAlmostEqual(float(output["format"]["duration"]), 2, delta=0.25)
                self.assertEqual({s["codec_type"] for s in output["streams"]}, {"audio", "video"})
                output = probe(audio)
                self.assertEqual(output["streams"][0]["codec_name"], "mp3")
                self.assertEqual(int(output["streams"][0]["bit_rate"]), 192000)
                restored = Store(queue.store.directory)
                self.assertEqual(restored.jobs[0].clip_start, 1.5)
                self.assertEqual(restored.jobs[1].audio_encoding_quality, "192K")
            finally:
                queue.shutdown()
                await_idle(queue)
                server.shutdown()
                server.server_close()
