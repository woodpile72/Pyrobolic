# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pyrobolic.core import Job, Queue, Settings, Store, staging_directory
from pyrobolic.history import expired_completed, expired_completed_seconds, retention_seconds, RETENTION_SECONDS

NOW = 2000000000


class HistoryRetentionTests(unittest.TestCase):
    def test_seconds_intervals_and_legacy_compatibility(self):
        jobs = [Job("https://example.org/", "Fixture", "/tmp", status=status, completed_at=1)
                for status in ("Completed", "Failed", "Canceled", "Interrupted", "Queued", "Downloading")]
        for seconds in RETENTION_SECONDS:
            settings = Settings(history_retention_seconds=seconds)
            self.assertEqual(retention_seconds(settings), seconds)
            self.assertEqual(expired_completed_seconds(jobs, seconds, now=NOW),
                             {jobs[0].id} if seconds else set())
        self.assertEqual(retention_seconds(Settings(history_retention_days=7)), 604800)
        self.assertEqual(expired_completed_seconds(jobs, -1, [jobs[0].id]), set())
        for invalid in (True, "3600", 60, -2):
            with self.assertRaises(ValueError):
                Settings(history_retention_seconds=invalid)

    def test_seconds_save_rollback_and_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            job = Job("https://example.org/", "Fixture", directory, status="Completed", completed_at=1)
            store.jobs.append(job)
            store.save()
            queue = Queue(store)
            with patch.object(store, "save", side_effect=OSError("full")), self.assertRaises(OSError):
                queue.set_history_clear(-1)
            self.assertEqual(store.jobs, [job])
            self.assertIsNone(store.settings.history_retention_seconds)
            queue.retry_save()
            self.assertEqual(queue.set_history_clear(3600), 1)
            self.assertEqual(Store(store.directory).settings.history_retention_seconds, 3600)

    def test_default_boundary_completion_time_legacy_and_recoverable_records(self):
        old = NOW - 30 * 86400
        jobs = [Job("https://example.org/", "Fixture", "/tmp", status=status, created=old)
                for status in ("Completed", "Failed", "Canceled", "Interrupted", "Queued", "Downloading")]
        self.assertEqual(expired_completed(jobs, 0, now=NOW), set())
        self.assertEqual(expired_completed(jobs, 7, now=NOW), {jobs[0].id})
        self.assertEqual(expired_completed(jobs, 7, [jobs[0].id], now=NOW), set())
        jobs[0].completed_at = NOW - 7 * 86400
        self.assertFalse(expired_completed(jobs, 7, now=NOW))
        jobs[0].completed_at -= 1
        self.assertEqual(expired_completed(jobs, 7, now=NOW), {jobs[0].id})
        jobs[0].completed_at = NOW
        self.assertFalse(expired_completed(jobs, 7, now=NOW))
        for value in (-1, 1, True, "30"):
            with self.assertRaises(ValueError):
                Settings(history_retention_days=value)

    def test_transaction_persistence_file_preservation_and_failed_save(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "state")
            old = Job("https://example.org/old", "Old", str(root), filename="Old", output_stem="Old",
                      status="Completed", completed_at=1, created=1)
            current = replace(old, id="current", completed_at=NOW)
            failed = replace(old, id="failed", status="Failed")
            media = root / "Old.mp4"
            media.write_bytes(b"preserved media")
            old.files = [str(media)]
            from pyrobolic.core import prepare_staging
            stage = prepare_staging(old)
            (stage / "media.mp4.part").write_bytes(b"preserved partial")
            store.jobs[:] = [old, current, failed]
            queue = Queue(store)
            store.save()
            queue.completed[old.id] = old
            queue.batches["fixture"] = [old, current]
            with patch.object(store, "save", side_effect=OSError("full")), self.assertRaises(OSError):
                queue.set_history_retention(7)
            self.assertEqual(store.settings.history_retention_days, 0)
            self.assertEqual(store.jobs, [old, current, failed])
            queue.retry_save()
            with patch("pyrobolic.history.time.time", return_value=NOW):
                self.assertEqual(queue.set_history_retention(7), 1)
            self.assertEqual(store.jobs, [current, failed])
            self.assertNotIn(old.id, queue.completed)
            self.assertEqual(queue.batches["fixture"], [current])
            self.assertEqual(media.read_bytes(), b"preserved media")
            self.assertFalse(stage.exists())
            restored = Store(store.directory)
            self.assertEqual(restored.settings.history_retention_days, 7)
            self.assertEqual([j.id for j in restored.jobs], [current.id, failed.id])
            with patch.object(store, "save") as save:
                self.assertEqual(queue.prune_history(now=NOW), 0)
                save.assert_not_called()
