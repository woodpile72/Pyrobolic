# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from pyrobolic.core import Job, Process, Queue, Settings, Store, download_arguments
from pyrobolic.artwork import artwork_arguments, finish_artwork, validate_artwork
from test_core import await_idle
import test_media_policies


class ArtworkTests(unittest.TestCase):
    def test_default_validation_auth_and_arguments(self):
        job = Job("https://example.org/fixture", "Fixture", "/tmp", filename="Fixture", output_stem="Fixture")
        self.assertEqual(artwork_arguments(job), [])
        for mode in (None, True, "shell"):
            with self.assertRaises(ValueError):
                Settings(artwork_mode=mode)
        for container in ("avi", "webm"):
            with self.assertRaisesRegex(ValueError, "cannot embed"):
                validate_artwork(replace(job, artwork_mode="embed", container=container))
        args = download_arguments(replace(job, artwork_mode="both"), "yt-dlp")
        self.assertIn("--write-thumbnail", args)
        self.assertIn("--embed-thumbnail", args)
        self.assertEqual(artwork_arguments(replace(job, artwork_mode="both", auth_ref="fixture")), [])

    def test_persistence_retry_and_redownload(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state")
            store.paused = True
            queue = Queue(store)
            job = Job("https://example.org/fixture", "Fixture", directory, filename="Fixture", artwork_mode="both")
            queue.add([job], Settings(artwork_mode="both"))
            restored = Store(store.directory)
            self.assertEqual(restored.settings.artwork_mode, "both")
            restored.settings.artwork_mode = "off"
            queue = Queue(restored)
            queue.retry(job.id)
            self.assertEqual(restored.jobs[0].artwork_mode, "both")
            restored.jobs[0].status = "Completed"
            queue.redownload(job.id)
            self.assertEqual(restored.jobs[-1].artwork_mode, "both")

    def test_foreign_paths_and_missing_embedding_fail_without_deleting_images(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            image = stage / "media.png"
            image.write_bytes(b"fixture")
            job = Job("https://example.org/fixture", "Fixture", directory, artwork_mode="embed", files=[str(stage / "media.mp4")])
            with self.assertRaisesRegex(RuntimeError, "outside"):
                finish_artwork(job, stage, [str(stage / "nested" / "foreign.png")], True, lambda _: "{}")
            def run(args):
                if args[0] == "ffmpeg":
                    return ""
                return json.dumps({"streams": [{"codec_type": "video", "width": 32, "height": 32}]})
            with self.assertRaisesRegex(RuntimeError, "not embedded"):
                finish_artwork(job, stage, [str(image)], True, run)
            self.assertTrue(image.exists())
            image.write_bytes(b"")
            with self.assertRaisesRegex(RuntimeError, "empty"):
                finish_artwork(job, stage, [str(image)], True, run)
            self.assertFalse(image.exists())


@unittest.skipUnless(os.environ.get("PYROBOLIC_INTEGRATION") == "1", "Set PYROBOLIC_INTEGRATION=1")
class ArtworkMediaTests(unittest.TestCase):
    setUp = test_media_policies.MediaPolicyTests.setUp
    job = test_media_policies.MediaPolicyTests.job
    run_jobs = test_media_policies.MediaPolicyTests.run_jobs

    def artwork(self):
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=128x72",
                        "-frames:v", "1", "-threads", "1", str(self.root / "cover.png")], check=True)
        self.info["thumbnail"] = self.base + "/cover.png"
        self.info["thumbnails"] = [{"url": self.base + "/cover.png", "id": "cover", "width": 128, "height": 72}]

    def test_save_embed_both_audio_collision_and_no_artwork(self):
        self.artwork()
        jobs = [self.job("Save", source_format="low", artwork_mode="save"),
                self.job("Embed", source_format="low", artwork_mode="embed", container="mp4"),
                self.job("Both", source_format="low", artwork_mode="both", container="mkv"),
                self.job("Audio", source_format="low", mode="audio", audio_format="mp3", artwork_mode="both")]
        Path(jobs[0].output).mkdir()
        existing = Path(jobs[0].output) / "Save.png"
        existing.write_bytes(b"existing image")
        self.run_jobs(jobs)
        for job in jobs:
            self.assertEqual(job.status, "Completed", job.detail + "\n" + "\n".join(job.log))
        self.assertEqual(existing.read_bytes(), b"existing image")
        self.assertEqual(len(jobs[1].files), 1)
        for job in (jobs[0], jobs[2], jobs[3]):
            self.assertEqual(len(job.files), 2)
            sidecar = next(Path(name) for name in job.files if name.endswith(".png"))
            self.assertEqual(sidecar.read_bytes(), (self.root / "cover.png").read_bytes())
            self.assertEqual(sidecar.stem, Path(job.files[0]).stem)
        for job in jobs[1:]:
            streams = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json", job.files[0]]))["streams"]
            self.assertTrue(any(stream.get("disposition", {}).get("attached_pic") or stream.get("codec_type") == "attachment" for stream in streams))
        self.info.pop("thumbnail")
        self.info.pop("thumbnails")
        missing = self.job("No art", source_format="low", artwork_mode="both")
        self.run_jobs([missing])
        self.assertEqual(missing.status, "Completed", missing.detail)
        self.assertIn("No source artwork", missing.warnings[0])
        self.assertEqual(len(missing.files), 1)

    def test_artwork_subtitles_and_default_clipped_chapters_together(self):
        self.artwork()
        self.info["chapters"] = [{"start_time": 0, "end_time": 0.6, "title": "Opening"},
                                 {"start_time": 0.6, "end_time": 1.2, "title": "Closing"}]
        job = self.job("Combined", source_format="low", container="mp4", artwork_mode="both",
                       subtitle_mode="both", subtitle_format="srt", clip_start=0.6, clip_end=1.1,
                       precise_cuts=True)
        job.metadata = True
        self.run_jobs([job])
        self.assertEqual(job.status, "Completed", job.detail + "\n" + "\n".join(job.log))
        self.assertEqual(len(job.files), 3)
        result = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-show_chapters", "-of", "json", job.files[0]]))
        self.assertEqual([chapter["tags"]["title"] for chapter in result["chapters"]], ["Closing"])
        self.assertAlmostEqual(float(result["chapters"][0]["start_time"]), 0)
        self.assertTrue(any(stream.get("disposition", {}).get("attached_pic") for stream in result["streams"]))
        self.assertTrue(any(stream["codec_type"] == "subtitle" for stream in result["streams"]))

    def test_square_audio_cover_from_png_and_jpeg_and_video_unchanged(self):
        self.artwork()
        png = self.job("Square PNG", source_format="low", mode="audio", audio_format="mp3",
                       artwork_mode="both", square_audio_artwork=True)
        video = self.job("Original video artwork", source_format="low", artwork_mode="save", square_audio_artwork=True)
        self.run_jobs([png, video])
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(self.root / "cover.png"), "-threads", "1",
                        str(self.root / "cover.jpg")], check=True)
        self.info["thumbnail"] = self.base + "/cover.jpg"
        self.info["thumbnails"] = [{"url": self.base + "/cover.jpg", "id": "cover"}]
        jpeg = self.job("Square JPEG", source_format="low", mode="audio", audio_format="mp3",
                        artwork_mode="both", square_audio_artwork=True)
        self.run_jobs([jpeg])
        for job in (png, jpeg, video):
            self.assertEqual(job.status, "Completed", job.detail + "\n" + "\n".join(job.log))
            image = next(path for path in job.files if path.endswith((".jpg", ".png")))
            stream = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json", image]))["streams"][0]
            self.assertEqual((stream["width"], stream["height"]), (128, 72) if job is video else (72, 72))
            if job is not video:
                self.assertTrue(image.endswith(".jpg"))

    def test_corrupt_artwork_fails_then_retries_and_cancellation_reaches_verification(self):
        self.artwork()
        good = (self.root / "cover.png").read_bytes()
        (self.root / "cover.png").write_bytes(b"not an image")
        job = self.job("Retry art", source_format="low", artwork_mode="save")
        cropped = self.job("Retry cropped art", source_format="low", mode="audio", audio_format="mp3",
                           artwork_mode="both", square_audio_artwork=True)
        self.run_jobs([job, cropped])
        self.assertEqual(job.status, "Failed")
        self.assertEqual(cropped.status, "Failed")
        self.assertFalse(any(Path(job.output).glob("Retry art*")))
        self.assertEqual(self.queue.take_completions(), [])
        (self.root / "cover.png").write_bytes(good)
        def arguments(item, executable):
            return download_arguments(item, executable)[:-2] + ["--load-info-json", str(self.info_path)]
        with patch("pyrobolic.core.download_arguments", side_effect=arguments):
            self.queue.retry(job.id)
            self.queue.retry(cropped.id)
            await_idle(self.queue, 30)
        self.assertEqual(job.status, "Completed", job.detail)
        self.assertEqual(cropped.status, "Completed", cropped.detail)
        self.queue.take_completions()
        entered = threading.Event()
        original = Process.communicate
        def wait_probe(process, args, timeout=20):
            if args[0] == "ffprobe":
                entered.set()
                if not process.cancelled.wait(15):
                    raise RuntimeError("Artwork cancellation was not delivered")
                raise InterruptedError("Canceled")
            return original(process, args, timeout)
        canceled = self.job("Cancel art", source_format="low", artwork_mode="save")
        with patch("pyrobolic.core.download_arguments", side_effect=arguments), patch.object(Process, "communicate", wait_probe):
            self.queue.add([canceled])
            self.assertTrue(entered.wait(30))
            self.queue.cancel(canceled.id)
            await_idle(self.queue, 20)
        self.assertEqual(canceled.status, "Canceled")
        self.assertFalse(any(Path(canceled.output).glob("Cancel art*")))
        self.assertEqual(self.queue.take_completions(), [])
