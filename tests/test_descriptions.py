# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pyrobolic.core import Job, Queue, Settings, Store, download_arguments, progress_update
from pyrobolic.metadata import description_outputs
from test_core import await_idle
import test_media_policies


class DescriptionTests(unittest.TestCase):
    def test_policy_protocol_and_persistence(self):
        with self.assertRaises(ValueError):
            Settings(save_description="yes")
        self.assertEqual(progress_update("PYRO_DESCRIPTION:1"), {"description_available": True})
        with self.assertRaises(ValueError):
            progress_update("PYRO_DESCRIPTION:unexpected")
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state")
            store.paused = True
            job = Job("https://example.org/video", "Fixture", directory, filename="Fixture", save_description=True)
            Queue(store).add([job], Settings(save_description=True))
            self.assertIn("--write-description", download_arguments(job, "yt-dlp"))
            self.assertNotIn("--write-description", download_arguments(replace(job, auth_ref="fixture"), "yt-dlp"))
            restored = Store(store.directory)
            self.assertTrue(restored.settings.save_description)
            restored.settings.save_description = False
            Queue(restored).retry(job.id)
            self.assertTrue(restored.jobs[0].save_description)
            restored.jobs[0].status = "Completed"
            Queue(restored).redownload(job.id)
            self.assertTrue(restored.jobs[-1].save_description)

    def test_missing_empty_and_symlink_outputs(self):
        job = Job("https://example.org/video", "Fixture", "/tmp", save_description=True)
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            with self.assertRaises(RuntimeError):
                description_outputs(job, stage, None)
            self.assertEqual(description_outputs(job, stage, False), [])
            self.assertIn("No source description", job.warnings[-1])
            path = stage / "media.description"
            for create in (lambda: None, lambda: path.touch()):
                create()
                with self.assertRaises((RuntimeError, ValueError)):
                    description_outputs(job, stage, True)
            self.assertFalse(path.exists())
            target = stage / "unrelated.txt"
            target.write_text("untouched", encoding="utf-8")
            path.symlink_to(target)
            with self.assertRaises(RuntimeError):
                description_outputs(job, stage, True)
            self.assertEqual(target.read_text(), "untouched")


@unittest.skipUnless(os.environ.get("PYROBOLIC_INTEGRATION") == "1", "Set PYROBOLIC_INTEGRATION=1")
class DescriptionMediaTests(unittest.TestCase):
    setUp = test_media_policies.MediaPolicyTests.setUp
    job = test_media_policies.MediaPolicyTests.job
    run_jobs = test_media_policies.MediaPolicyTests.run_jobs

    def test_text_collision_subtitles_and_missing_source(self):
        description = "Source text\nUnicode: caf\u00e9\nhttps://example.org/reference\n"
        self.info["description"] = description
        output = self.root / "media"
        output.mkdir()
        existing = output / "Description.description"
        existing.write_text("Keep me", encoding="utf-8")
        job = self.job("Description", source_format="low", save_description=True,
                       subtitle_mode="sidecar", subtitle_languages="en", subtitle_format="srt")
        self.run_jobs([job])
        self.assertEqual(job.status, "Completed", job.detail + "\n" + "\n".join(job.log))
        paths = [Path(value) for value in job.files]
        self.assertEqual(len(paths), 3)
        sidecar = next(path for path in paths if path.suffix == ".description")
        self.assertEqual(sidecar.read_text(encoding="utf-8").rstrip(), description.rstrip())
        self.assertNotEqual(sidecar, existing)
        self.assertEqual(existing.read_text(), "Keep me")
        self.assertTrue(any(path.suffix == ".srt" for path in paths))
        self.info.pop("description")
        missing = self.job("No description", source_format="low", save_description=True)
        self.run_jobs([missing])
        self.assertEqual(missing.status, "Completed", missing.detail)
        self.assertEqual(len(missing.files), 1)
        self.assertIn("No source description", missing.warnings[-1])

    def test_failed_verification_does_not_publish_and_retry_succeeds(self):
        self.info["description"] = "Retry fixture"
        job = self.job("Retry description", source_format="low", save_description=True)
        with patch("pyrobolic.metadata.description_outputs", side_effect=RuntimeError("Description unavailable")):
            self.run_jobs([job])
        self.assertEqual(job.status, "Failed")
        self.assertEqual(self.queue.take_completions(), [])
        self.assertFalse(list(Path(job.output).glob("Retry description*")))
        def arguments(item, executable):
            return download_arguments(item, executable)[:-2] + ["--load-info-json", str(self.info_path)]
        with patch("pyrobolic.core.download_arguments", side_effect=arguments):
            self.queue.retry(job.id)
            await_idle(self.queue, 30)
        self.assertEqual(job.status, "Completed", job.detail)
        self.assertTrue(any(Path(name).suffix == ".description" for name in job.files))
