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

from pyrobolic.core import Job, Process, Queue, Settings, Store, download_arguments, progress_update
from test_core import await_idle
from pyrobolic.metadata import metadata_arguments, expected_chapters
import test_media_policies


class MetadataTests(unittest.TestCase):
    def test_legacy_independent_choices_and_authentication_guard(self):
        job = Job("https://example.org/fixture", "Fixture", "/tmp")
        self.assertEqual(metadata_arguments(job), ["--embed-metadata"])
        self.assertEqual(metadata_arguments(replace(job, metadata=False)), [])
        args = metadata_arguments(replace(job, chapter_policy="omit", source_info_policy="omit"))
        self.assertEqual(args, ["--embed-metadata", "--no-embed-chapters", "--no-embed-info-json"])
        args = metadata_arguments(replace(job, metadata=False, chapter_policy="embed", source_info_policy="embed"))
        self.assertIn("--embed-chapters", args)
        self.assertIn("--embed-info-json", args)
        self.assertNotIn("--embed-metadata", args)
        args = download_arguments(replace(job, metadata=False, auth_ref="fixture", chapter_policy="embed", source_info_policy="embed"), "yt-dlp")
        self.assertIn("--no-embed-chapters", args)
        self.assertIn("--no-embed-info-json", args)
        self.assertNotIn("--embed-chapters", args)

    def test_validation_protocol_and_range_overlap(self):
        for field in ("chapter_policy", "source_info_policy"):
            for value in (True, None, "shell flags"):
                with self.assertRaises(ValueError):
                    Settings(**{field: value})
        for field in ("split_chapters", "chapter_titles", "precise_chapters"):
            with self.assertRaises(ValueError):
                Settings(**{field: "yes"})
        with self.assertRaisesRegex(ValueError, "entire media"):
            download_arguments(Job("https://example.org/fixture", "Fixture", "/tmp", split_chapters=True, clip_end=10), "yt-dlp")
        chapters = [{"start_time": 0, "end_time": 10}, {"start_time": 10, "end_time": 20}]
        self.assertEqual(progress_update("PYRO_CHAPTERS:" + json.dumps(chapters)), {"source_chapters": chapters})
        job = Job("https://example.org/fixture", "Fixture", "/tmp", clip_start=20)
        self.assertFalse(expected_chapters(job, chapters))
        self.assertTrue(expected_chapters(replace(job, clip_start=15), chapters))
        self.assertFalse(expected_chapters(job, [None, {"start_time": "bad", "end_time": 30}]))

    def test_saved_defaults_job_snapshots_retry_and_redownload(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            store.paused = True
            queue = Queue(store)
            job = Job("https://example.org/fixture", "Fixture", directory, chapter_policy="embed", source_info_policy="omit", chapter_titles=True)
            queue.add([job], Settings(chapter_policy="embed", source_info_policy="omit", chapter_titles=True))
            restored = Store(store.directory)
            self.assertEqual(restored.settings.chapter_policy, "embed")
            self.assertTrue(restored.settings.chapter_titles)
            restored.settings.chapter_policy = "omit"
            queue = Queue(restored)
            queue.retry(job.id)
            self.assertEqual(restored.jobs[0].chapter_policy, "embed")
            self.assertTrue(restored.jobs[0].chapter_titles)
            restored.jobs[0].status = "Completed"
            queue.redownload(job.id)
            self.assertEqual(restored.jobs[-1].chapter_policy, "embed")
            self.assertEqual(restored.jobs[-1].source_info_policy, "omit")
            self.assertTrue(restored.jobs[-1].chapter_titles)


@unittest.skipUnless(os.environ.get("PYROBOLIC_INTEGRATION") == "1", "Set PYROBOLIC_INTEGRATION=1")
class MetadataMediaTests(unittest.TestCase):
    setUp = test_media_policies.MediaPolicyTests.setUp
    job = test_media_policies.MediaPolicyTests.job
    run_jobs = test_media_policies.MediaPolicyTests.run_jobs

    def test_chapter_title_names_unicode_long_titles_and_collisions(self):
        self.info["chapters"] = [{"start_time": 0, "end_time": 0.6, "title": "../Opening / movement: \u00e9\u00e9\u00e9 " * 10},
                                 {"start_time": 0.6, "end_time": 1.2, "title": "Closing 100%"}]
        job = self.job("T" * 140, source_format="low", split_chapters=True, chapter_titles=True, precise_chapters=True)
        self.run_jobs([job])
        self.assertEqual(job.status, "Completed", job.detail + "\n" + "\n".join(job.log))
        first = [Path(path) for path in job.files]
        self.assertEqual(len(first), 3)
        for index, path in enumerate(first[1:], 1):
            self.assertIn(f".chapter-{index:03d}-", path.name)
            self.assertEqual(path.parent, Path(job.output))
            self.assertLessEqual(len(path.name.encode("utf-8")), 255)
        self.assertIn("Closing 100%", first[2].name)
        original = {path: path.read_bytes() for path in first}
        again = self.job("T" * 140, source_format="low", split_chapters=True, chapter_titles=True, precise_chapters=True)
        self.run_jobs([again])
        self.assertEqual(again.status, "Completed", again.detail)
        self.assertTrue(set(again.files).isdisjoint(job.files))
        for path, data in original.items():
            self.assertEqual(path.read_bytes(), data)

    def test_split_chapters_precise_video_audio_and_missing_source(self):
        self.info["chapters"] = [{"start_time": 0, "end_time": 0.6, "title": "Opening"},
                                 {"start_time": 0.6, "end_time": 1.2, "title": "Closing"}]
        jobs = [self.job("Split video", source_format="low", container="mp4", split_chapters=True, precise_chapters=True),
                self.job("Split audio", source_format="low", mode="audio", audio_format="mp3", split_chapters=True)]
        self.run_jobs(jobs)
        for job in jobs:
            self.assertEqual(job.status, "Completed", job.detail + "\n" + "\n".join(job.log))
            self.assertEqual(len(job.files), 3)
            for number, path in enumerate(job.files[1:], 1):
                self.assertIn(f".chapter-{number:03d}.", path)
                result = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_format", "-of", "json", path]))
                self.assertAlmostEqual(float(result["format"]["duration"]), 0.6, delta=0.15)
        self.info.pop("chapters")
        missing = self.job("No splits", source_format="low", split_chapters=True)
        self.run_jobs([missing])
        self.assertEqual(missing.status, "Completed", missing.detail)
        self.assertEqual(len(missing.files), 1)
        self.assertIn("only the full media", missing.warnings[0])

    def test_failed_verification_retry_and_cancel_before_publication(self):
        self.info["chapters"] = [{"start_time": 0, "end_time": 1.2, "title": "Fixture chapter"}]
        job = self.job("Verified retry", source_format="low", chapter_policy="embed")
        original = Process.communicate
        def empty_probe(process, args, timeout=20):
            return "{}" if args[0] == "ffprobe" else original(process, args, timeout)
        with patch.object(Process, "communicate", empty_probe):
            self.run_jobs([job])
        self.assertEqual(job.status, "Failed")
        self.assertIn("not embedded", job.detail)
        self.assertEqual(self.queue.take_completions(), [])
        self.assertFalse(any(Path(job.output).glob("Verified retry*")))
        def arguments(item, executable):
            return download_arguments(item, executable)[:-2] + ["--load-info-json", str(self.info_path)]
        with patch("pyrobolic.core.download_arguments", side_effect=arguments):
            self.queue.retry(job.id)
            await_idle(self.queue, 30)
        self.assertEqual(job.status, "Completed", job.detail)
        self.queue.take_completions()
        entered = threading.Event()
        def cancel_probe(process, args, timeout=20):
            if args[0] == "ffprobe":
                entered.set()
                if not process.cancelled.wait(15):
                    raise RuntimeError("Cancellation did not reach chapter verification")
                raise InterruptedError("Canceled")
            return original(process, args, timeout)
        canceled = self.job("Cancel chapters", source_format="low", chapter_policy="embed")
        with patch("pyrobolic.core.download_arguments", side_effect=arguments), patch.object(Process, "communicate", cancel_probe):
            self.queue.add([canceled])
            self.assertTrue(entered.wait(30))
            self.queue.cancel(canceled.id)
            await_idle(self.queue, 20)
        self.assertEqual(canceled.status, "Canceled")
        self.assertEqual(self.queue.take_completions(), [])
        self.assertFalse(any(Path(canceled.output).glob("Cancel chapters*")))

    def test_chapters_source_data_omission_and_missing_source(self):
        self.info["chapters"] = [{"start_time": 0, "end_time": 0.6, "title": "Opening"},
                                 {"start_time": 0.6, "end_time": 1.2, "title": "Closing"}]
        self.info["description"] = "Synthetic source description"
        jobs = [self.job("Chapters", source_format="low", container="mp4", chapter_policy="embed"),
                self.job("Attachment", source_format="low", container="mkv", source_info_policy="embed"),
                self.job("Omit", source_format="low", container="mkv", chapter_policy="omit", source_info_policy="omit"),
                self.job("Unsupported attachment", source_format="low", container="mp4", source_info_policy="embed"),
                self.job("Clipped chapters", source_format="low", container="mp4", chapter_policy="embed",
                         clip_start=0.6, clip_end=1.1, precise_cuts=True),
                self.job("Default clipped chapters", source_format="low", container="mp4",
                         clip_start=0.6, clip_end=1.1, precise_cuts=True)]
        jobs[2].metadata = True
        jobs[5].metadata = True
        self.run_jobs(jobs)
        for job in jobs:
            self.assertEqual(job.status, "Completed", "\n".join(job.log) + "\n" + job.detail)
        def probe(job):
            return json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_chapters", "-show_streams", "-of", "json", job.files[0]]))
        chapters = probe(jobs[0])["chapters"]
        self.assertEqual([chapter["tags"]["title"] for chapter in chapters], ["Opening", "Closing"])
        self.assertAlmostEqual(float(chapters[1]["start_time"]), 0.6, places=2)
        attachments = [stream for stream in probe(jobs[1])["streams"] if stream["codec_type"] == "attachment"]
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["tags"]["filename"], "info.json")
        omitted = probe(jobs[2])
        self.assertFalse(omitted["chapters"])
        self.assertFalse(any(stream["codec_type"] == "attachment" for stream in omitted["streams"]))
        self.assertIn("require MKV", jobs[3].warnings[0])
        clipped = probe(jobs[4])["chapters"]
        self.assertEqual([chapter["tags"]["title"] for chapter in clipped], ["Closing"])
        self.assertAlmostEqual(float(clipped[0]["start_time"]), 0, places=2)
        self.assertLessEqual(float(clipped[0]["end_time"]), 0.52)
        self.assertEqual(probe(jobs[5])["chapters"], clipped)
        self.info.pop("chapters")
        missing = self.job("No source chapters", source_format="low", chapter_policy="embed")
        self.run_jobs([missing])
        self.assertEqual(missing.status, "Completed", missing.detail)
        self.assertIn("No source chapters", missing.warnings[0])
