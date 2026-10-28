# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
from dataclasses import replace
from functools import partial
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from pyrobolic.core import Job, Process, Queue, Settings, Store, download_arguments, staging_directory
from pyrobolic.encoding import ENCODERS, QUALITIES, COMPATIBLE, encoding_arguments, encoding_detail
from test_core import await_idle
from test_integration import QuietHandler


class EncodingTests(unittest.TestCase):
    def job(self, **options):
        return Job("https://example.org/fixture", "Fixture", "/tmp", **options)

    def test_defaults_and_inactive_modes_do_not_change_existing_output(self):
        self.assertEqual(encoding_arguments(self.job(container="mp4", video_processing="convert")), [])
        for mode, processing in (("video", "remux"), ("audio", "convert")):
            job = self.job(mode=mode, container="mp4", video_processing=processing,
                           video_encoder="h264", video_encoding_quality="high")
            self.assertEqual(encoding_arguments(job), [])
            self.assertNotIn("--postprocessor-args", download_arguments(job, "yt-dlp"))

    def test_compatibility_and_invalid_saved_values(self):
        for container, compatible in COMPATIBLE.items():
            for encoder in ENCODERS:
                job = self.job(container=container, video_processing="convert", video_encoder=encoder)
                if encoder == "default" or encoder in compatible:
                    download_arguments(job, "yt-dlp")
                else:
                    with self.assertRaises(ValueError):
                        download_arguments(job, "yt-dlp")
        for value in ("unsafe -x", None, 23):
            with self.assertRaises(ValueError):
                Settings(video_encoding_quality=value)
            with self.assertRaises(ValueError):
                Store.decode(json.dumps({"jobs": [{"url": "https://example.org/", "title": "Fixture",
                                                  "output": "/tmp", "video_encoder": value}]}))
        for threads in (-1, 65, 1.5, True):
            with self.assertRaises(ValueError):
                Settings(video_encoding_threads=threads)
        for value in (1, "yes", None):
            with self.assertRaises(ValueError):
                Settings(reencode_video=value)

    def test_reencoding_is_explicit_and_requires_owned_staging(self):
        job = self.job(container="mp4", video_processing="convert", reencode_video=True,
                       source_format="exact")
        with self.assertRaisesRegex(ValueError, "owned staging"):
            download_arguments(job, "yt-dlp")
        job = replace(job, filename="Fixture", output_stem="Fixture")
        args = download_arguments(job, "yt-dlp")
        self.assertIn("before_dl:PYRO_INPUT_EXT:%(ext)j", args)
        self.assertEqual(args[args.index("-f") + 1], 'best*[format_id="exact"]')
        self.assertNotIn("PYRO_INPUT_EXT", " ".join(download_arguments(replace(job, video_processing="remux"), "yt-dlp")))
        self.assertNotIn("PYRO_INPUT_EXT", " ".join(download_arguments(replace(job, mode="audio"), "yt-dlp")))

    def test_threads_are_scoped_and_do_not_force_default_codec(self):
        job = self.job(container="avi", video_processing="convert", video_encoding_threads=2)
        self.assertEqual(encoding_arguments(job), ["--postprocessor-args", "VideoConvertor+ffmpeg_o:-threads 2"])
        hevc = replace(job, container="mkv", video_encoder="hevc", video_encoding_threads=24)
        self.assertIn("pools=24:frame-threads=16", encoding_arguments(hevc)[1])

    def test_profiles_target_only_video_converter_and_exact_source_is_preserved(self):
        for quality in QUALITIES[1:]:
            job = self.job(container="webm", video_processing="convert", video_encoder="vp9",
                           video_encoding_quality=quality, source_format="exact", preferred_video_codec="h264")
            args = download_arguments(job, "yt-dlp")
            self.assertEqual(args[args.index("-f") + 1], 'best*[format_id="exact"]')
            self.assertNotIn("--format-sort", args)
            self.assertIn("VideoConvertor+ffmpeg_o:-c:v libvpx-vp9 -crf", args[args.index("--postprocessor-args") + 1])
            self.assertIn("-b:v 0", args[args.index("--postprocessor-args") + 1])
            self.assertIn("VP9", encoding_detail(job))

    def test_persistence_retry_redownload_and_failed_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            store.paused = True
            queue = Queue(store)
            settings = Settings(container="mp4", video_processing="convert", video_encoder="h264", video_encoding_quality="high", reencode_video=True)
            job = self.job(container="mp4", video_processing="convert", video_encoder="h264", video_encoding_quality="high", reencode_video=True)
            queue.add([job], settings)
            restored = Store(store.directory)
            self.assertEqual(restored.settings.video_encoding_quality, "high")
            self.assertTrue(restored.settings.reencode_video)
            restored.settings.reencode_video = False
            restored.settings.video_encoding_quality = "small"
            resumed = Queue(restored)
            resumed.retry(job.id)
            self.assertEqual(restored.jobs[0].video_encoding_quality, "high")
            self.assertTrue(restored.jobs[0].reencode_video)
            restored.jobs[0].status = "Completed"
            resumed.redownload(job.id)
            self.assertEqual(restored.jobs[-1].video_encoder, "h264")
            self.assertEqual(restored.jobs[-1].video_encoding_quality, "high")
            self.assertTrue(restored.jobs[-1].reencode_video)
            with patch.object(store, "save", side_effect=OSError("full")), self.assertRaises(OSError):
                queue.add([replace(job, id="new-fixture")], replace(settings, video_encoding_quality="small"))
            self.assertEqual(store.settings.video_encoding_quality, "high")


@unittest.skipUnless(os.environ.get("PYROBOLIC_INTEGRATION") == "1", "Set PYROBOLIC_INTEGRATION=1")
class EncodingMediaTests(unittest.TestCase):
    def test_encoded_streams_quality_sizes_and_matching_container_passthrough(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fixture.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=256x144:rate=24",
                            "-f", "lavfi", "-i", "sine=frequency=440", "-t", "2", "-c:v", "libx264",
                            "-crf", "12", "-c:a", "aac", str(source)], check=True)
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=directory))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            store = Store(root / "state")
            queue = Queue(store)
            url = f"http://127.0.0.1:{server.server_port}/fixture.mp4"
            def job(name, container, encoder, quality, always=False):
                return Job(url, name, str(root / "output"), filename=name, metadata=False, include_id=False,
                           container=container, video_processing="convert", video_encoder=encoder,
                           video_encoding_quality=quality, video_encoding_threads=2, reencode_video=always)
            jobs = [job("High", "mkv", "h264", "high"), job("Small", "mkv", "h264", "small"),
                    job("VP9", "webm", "vp9", "balanced"), job("Keep", "mp4", "hevc", "small"),
                    job("HEVC", "mkv", "hevc", "balanced"), job("AV1", "mkv", "av1", "balanced"),
                    job("MPEG4", "avi", "mpeg4", "balanced"),
                    job("Forced HEVC", "mp4", "hevc", "balanced", True),
                    job("Forced H264", "mp4", "h264", "small", True),
                    job("Forced different", "mkv", "h264", "high", True),
                    job("Forced default", "mp4", "default", "default", True)]
            try:
                queue.add(jobs)
                await_idle(queue, 60)
                for item in jobs:
                    self.assertEqual(item.status, "Completed", "\n".join(item.log))
                    streams = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json", item.files[0]]))["streams"]
                    self.assertEqual({s["codec_type"] for s in streams}, {"audio", "video"})
                    video = next(s for s in streams if s["codec_type"] == "video")
                    self.assertEqual(video["codec_name"], {"VP9": "vp9", "HEVC": "hevc", "Forced HEVC": "hevc", "AV1": "av1", "MPEG4": "mpeg4"}.get(item.title, "h264"))
                    self.assertEqual((video["width"], video["height"]), (256, 144))
                self.assertGreater(Path(jobs[0].files[0]).stat().st_size, Path(jobs[1].files[0]).stat().st_size)
                def digest(path):
                    return subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v", "-c", "copy", "-f", "hash", "-"])
                self.assertEqual(digest(source), digest(jobs[3].files[0]))
                self.assertNotEqual(digest(source), digest(jobs[0].files[0]))
                self.assertNotEqual(digest(source), digest(jobs[8].files[0]))
                self.assertEqual(digest(jobs[0].files[0]), digest(jobs[9].files[0]))
                for item in jobs[7:9]:
                    def audio_hash(path):
                        return subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a", "-c", "copy", "-f", "hash", "-"])
                    self.assertEqual(audio_hash(source), audio_hash(item.files[0]))
                self.assertEqual(Store(store.directory).jobs[0].video_encoding_quality, "high")
                missing = job("Missing encoder", "mkv", "h264", "high")
                with patch.dict("pyrobolic.encoding.IMPLEMENTATIONS", {"h264": "fixture_missing_encoder"}):
                    queue.add([missing])
                    await_idle(queue, 30)
                self.assertEqual(missing.status, "Failed")
                self.assertFalse(missing.files)
                queue.retry(missing.id)
                await_idle(queue, 30)
                self.assertEqual(missing.status, "Completed", missing.log)
                self.assertEqual(missing.video_encoding_quality, "high")
                forced_missing = job("Missing matching encoder", "mp4", "h264", "high", True)
                with patch.dict("pyrobolic.encoding.IMPLEMENTATIONS", {"h264": "fixture_missing_encoder"}):
                    queue.add([forced_missing])
                    await_idle(queue, 30)
                self.assertEqual(forced_missing.status, "Failed")
                self.assertFalse(list((root / "output").glob("Missing matching encoder*.mp4")))
                queue.retry(forced_missing.id)
                await_idle(queue, 30)
                self.assertEqual(forced_missing.status, "Completed", forced_missing.log)
                self.assertTrue(forced_missing.reencode_video)
                # Exercise cancellation during the app-owned phase, not only the engine phase.
                interrupted = job("Canceled encoder", "mp4", "h264", "high", True)
                entered = threading.Event()
                communicate = Process.communicate
                def stalled(process, args, timeout=30):
                    if args[0] == "ffmpeg" and "encoding-output" in args[-1]:
                        entered.set()
                        args = [sys.executable, "-c", "import time; time.sleep(30)"]
                    return communicate(process, args, timeout)
                with patch.object(Process, "communicate", stalled):
                    queue.add([interrupted])
                    self.assertTrue(entered.wait(20))
                    with queue.lock:
                        worker = queue.running[interrupted.id]
                    queue.cancel(interrupted.id)
                    await_idle(queue, 15)
                    self.assertTrue(worker.cancelled.is_set())
                    self.assertTrue(worker.process is None or worker.process.poll() is not None)
                self.assertEqual(interrupted.status, "Canceled")
                self.assertEqual((staging_directory(interrupted) / "media.encoding-source.mp4").read_bytes(), source.read_bytes())
                self.assertFalse(list((root / "output").glob("Canceled encoder*.mp4")))
                queue.retry(interrupted.id)
                await_idle(queue, 30)
                self.assertEqual(interrupted.status, "Completed", interrupted.log)
                captions = root / "fixture.srt"
                captions.write_text("1\n00:00:00,000 --> 00:00:01,500\nFixture subtitle\n", encoding="utf-8")
                chapters = root / "chapters.txt"
                chapters.write_text(";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1500\ntitle=Fixture Chapter\n")
                attachment = root / "source.json"
                attachment.write_text('{"fixture": true}')
                enriched = root / "enriched.mkv"
                subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), "-i", str(captions),
                                "-f", "ffmetadata", "-i", str(chapters), "-map", "0", "-map", "1",
                                "-map_chapters", "2", "-c", "copy", "-attach", str(attachment),
                                "-metadata:s:t", "mimetype=application/json", str(enriched)], check=True)
                preserved = replace(job("Preserved tracks", "mkv", "hevc", "balanced", True),
                                    url=url.replace("fixture.mp4", "enriched.mkv"))
                queue.add([preserved])
                await_idle(queue, 30)
                self.assertEqual(preserved.status, "Completed", preserved.log)
                probe = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams",
                                                          "-show_chapters", "-of", "json", preserved.files[0]]))
                self.assertEqual([s["codec_type"] for s in probe["streams"]], ["video", "audio", "subtitle", "attachment"])
                self.assertEqual(probe["chapters"][0]["tags"]["title"], "Fixture Chapter")
                self.assertIn(b"Fixture subtitle", subprocess.check_output([
                    "ffmpeg", "-v", "error", "-i", preserved.files[0], "-map", "0:s", "-f", "srt", "-"]))
            finally:
                queue.shutdown()
                await_idle(queue)
                server.shutdown()
                server.server_close()
