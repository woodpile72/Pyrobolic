# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
from dataclasses import asdict, replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pyrobolic.core import Job, Queue, Settings, Store
from pyrobolic.preferences import preference_values


class HistoryActionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / "state")
        self.store.paused = True
        self.queue = Queue(self.store)
        self.job = Job("https://example.org/fixture", "Fixture", str(self.root), filename="Chosen",
                       source_format="stream-1", subtitle_mode="sidecar", subtitle_languages="en,es",
                       rate_limit_kib=256, clip_start=1, clip_end=3, precise_cuts=True)
        self.queue.add([self.job])
        self.media = self.root / (self.job.output_stem + ".mp4")
        self.media.write_bytes(b"published fixture")
        self.job.status = "Completed"
        self.job.progress = 1
        self.job.files = [str(self.media)]
        self.job.log = ["previous attempt"]
        self.job.warnings = ["previous warning"]
        self.job.completed_at = 123
        self.store.save()

    def test_redownload_retains_policy_without_mutating_original(self):
        before = asdict(self.job)
        identifier = self.queue.redownload(self.job.id)
        fresh = self.store.jobs[-1]
        self.assertNotEqual(identifier, self.job.id)
        self.assertNotEqual(fresh.batch_id, self.job.batch_id)
        self.assertNotEqual(fresh.output_stem, self.job.output_stem)
        self.assertEqual(asdict(self.job), before)
        self.assertEqual(self.media.read_bytes(), b"published fixture")
        self.assertEqual(preference_values(fresh), preference_values(self.job))
        self.assertEqual(fresh.source_format, "stream-1")
        self.assertEqual((fresh.clip_start, fresh.clip_end, fresh.precise_cuts), (1, 3, True))
        self.assertEqual(fresh.status, "Queued")
        self.assertEqual((fresh.files, fresh.log, fresh.warnings), ([], [], []))
        self.assertIsNone(fresh.completed_at)
        restored = Store(self.store.directory)
        self.assertEqual(restored.jobs[1].source_format, "stream-1")
        self.assertEqual(restored.jobs[0].files, [str(self.media)])

    def test_legacy_redownload_uses_owned_named_staging(self):
        self.job.filename = self.job.output_stem = None
        self.queue.redownload(self.job.id)
        self.assertIsNotNone(self.store.jobs[-1].filename)
        self.assertIsNotNone(self.store.jobs[-1].output_stem)

    def test_save_failure_does_not_create_record_or_change_original(self):
        before = asdict(self.job)
        with patch.object(self.store, "save", side_effect=OSError("full")), self.assertRaises(OSError):
            self.queue.redownload(self.job.id)
        self.assertEqual(self.store.jobs, [self.job])
        self.assertEqual(asdict(self.job), before)
        self.assertEqual(self.media.read_bytes(), b"published fixture")

    def test_active_or_tearing_down_download_is_not_duplicated(self):
        self.job.status = "Processing"
        with self.assertRaises(RuntimeError):
            self.queue.redownload(self.job.id)
        self.job.status = "Completed"
        self.queue.running[self.job.id] = object()
        with self.assertRaises(RuntimeError):
            self.queue.redownload(self.job.id)

    def test_saved_file_checks_record_existence_and_nonempty_content(self):
        self.assertEqual(self.queue.saved_file(self.job.id, str(self.media)), str(self.media))
        with self.assertRaises(ValueError):
            self.queue.saved_file(self.job.id, str(self.root / "unrecorded.mp4"))
        self.media.write_bytes(b"")
        with self.assertRaises(RuntimeError):
            self.queue.saved_file(self.job.id, str(self.media))
        self.media.unlink()
        with self.assertRaises(RuntimeError):
            self.queue.saved_file(self.job.id, str(self.media))

    def test_saved_file_never_launches_script_extension(self):
        script = self.root / "unsafe.desktop"
        script.write_bytes(b"not media")
        self.job.files = [str(script)]
        with self.assertRaises(ValueError):
            self.queue.saved_file(self.job.id, str(script))

    def test_appearance_and_sort_persist_and_bad_values_fail(self):
        settings = replace(self.store.settings, appearance="dark", history_sort="title", window_width=720,
                           window_height=500, window_maximized=True)
        self.queue.configure(settings)
        restored = Store(self.store.directory)
        self.assertEqual(restored.settings.appearance, "dark")
        self.assertEqual(restored.settings.history_sort, "title")
        self.assertEqual((restored.settings.window_width, restored.settings.window_height, restored.settings.window_maximized),
                         (720, 500, True))
        for values in ({"appearance": "purple"}, {"history_sort": "random"}, {"window_width": 0},
                       {"window_height": 999999}, {"window_maximized": 1}, {"remember_window": "yes"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                Settings(**values)

    def test_setting_failure_rolls_back_and_does_not_schedule(self):
        previous = self.store.settings
        with patch.object(self.store, "save", side_effect=OSError("full")), \
                patch.object(self.queue, "_schedule") as schedule, self.assertRaises(OSError):
            self.queue.configure(replace(previous, appearance="light", history_sort="oldest"))
        self.assertIs(self.store.settings, previous)
        self.assertEqual(self.queue.persistence_error, "full")
        schedule.assert_not_called()
        self.queue.configure(replace(previous, appearance="dark"))
        self.assertFalse(self.queue.persistence_error)
        self.assertEqual(Store(self.store.directory).settings.appearance, "dark")
