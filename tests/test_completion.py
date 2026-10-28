# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods

from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from pyrobolic.core import Job, Process, Queue, Settings, Store
from test_core import await_idle


class CompletionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / "state")
        self.store.paused = True
        self.queue = Queue(self.store)
        self.media = self.root / "video.mp4"
        self.media.write_bytes(b"media fixture")

    def job(self):
        return Job("https://example.org/video", "Video", str(self.root))

    def finish(self, job, output=None, script=None):
        job.status = "Downloading"
        process = Process()
        self.queue.running[job.id] = process
        if script is None:
            script = "print(" + repr("PYRO_FILE:" + json.dumps(str(output or self.media))) + ")"
        with patch("pyrobolic.core.engine_path", return_value="unused"), \
                patch("pyrobolic.core.shutil.which", return_value="present"), \
                patch("pyrobolic.core.download_arguments", return_value=[sys.executable, "-c", script]):
            self.queue._download(job, process)

    def test_verified_completion_delivered_once_and_not_replayed_on_restart(self):
        job = self.job()
        self.queue.add([job])
        self.finish(job)
        events = self.queue.take_completions()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].jobs[0].files, [str(self.media)])
        self.assertEqual(events[0].jobs[0].total_bytes, self.media.stat().st_size)
        self.assertIsNotNone(events[0].jobs[0].completed_at)
        self.assertEqual(Store(self.store.directory).jobs[0].total_bytes, self.media.stat().st_size)
        self.assertEqual(self.queue.take_completions(), [])
        self.assertEqual(Queue(Store(self.store.directory)).take_completions(), [])

    def test_batch_waits_for_settled_work_and_reports_mixed_results(self):
        jobs = [self.job() for _ in range(4)]
        self.queue.add(jobs)
        self.assertEqual(len({job.batch_id for job in jobs}), 1)
        self.finish(jobs[0])
        self.assertEqual(self.queue.take_completions(), [])
        self.finish(jobs[1])
        self.finish(jobs[2], script="raise SystemExit(1)")
        self.queue.cancel(jobs[3].id)
        events = self.queue.take_completions()
        self.assertEqual(len(events), 1)
        self.assertEqual(len(events[0].jobs), 2)
        self.assertEqual((events[0].failed, events[0].canceled), (1, 1))

    def test_invalid_output_failed_and_canceled_do_not_announce_success(self):
        for path in (self.root / "missing", self.root):
            job = self.job()
            self.queue.add([job])
            self.finish(job, output=path)
            self.assertEqual(job.status, "Failed")
        job = self.job()
        self.queue.add([job])
        self.queue.cancel(job.id)
        self.assertEqual(self.queue.take_completions(), [])

    def test_save_failure_defers_feedback_until_retry_save(self):
        job = self.job()
        self.queue.add([job])
        with patch.object(self.store, "save", side_effect=OSError("disk full")):
            self.finish(job)
        self.assertEqual(self.queue.take_completions(), [])
        self.queue.retry_save()
        self.assertEqual(len(self.queue.take_completions()), 1)

    def test_immediate_clear_after_failed_completion_save_is_retried(self):
        self.queue.set_history_clear(-1)
        job = self.job()
        self.queue.add([job])
        with patch.object(self.store, "save", side_effect=OSError("disk full")):
            self.finish(job)
        self.assertIn(job, self.store.jobs)
        self.assertEqual(self.queue.take_completions(), [])
        self.queue.retry_save()
        self.assertNotIn(job, self.store.jobs)
        self.assertEqual(len(self.queue.take_completions()), 1)
        self.assertTrue(self.media.is_file())

    def test_retry_only_announces_newly_completed_items(self):
        first, second = self.job(), self.job()
        self.queue.add([first, second])
        self.finish(first)
        self.finish(second, script="raise SystemExit(1)")
        self.assertEqual([j.id for j in self.queue.take_completions()[0].jobs], [first.id])
        self.queue.retry(second.id)
        self.finish(second)
        self.assertEqual([j.id for j in self.queue.take_completions()[0].jobs], [second.id])

    def test_clear_history_discards_unseen_completion(self):
        job = self.job()
        self.queue.add([job])
        self.finish(job)
        self.queue.clear_history()
        self.assertEqual(self.queue.take_completions(), [])
        self.assertEqual(self.queue.batches, {})

    def test_immediate_clear_preserves_batch_feedback_and_failed_records(self):
        self.queue.set_history_clear(-1)
        first, second, failed = self.job(), self.job(), self.job()
        self.queue.add([first, second, failed])
        self.finish(first)
        self.assertNotIn(first, self.store.jobs)
        self.assertEqual(self.queue.take_completions(), [])
        self.finish(second)
        self.finish(failed, script="raise SystemExit(1)")
        events = self.queue.take_completions()
        self.assertEqual(len(events), 1)
        self.assertEqual({j.id for j in events[0].jobs}, {first.id, second.id})
        self.assertEqual(events[0].failed, 1)
        self.assertEqual(self.store.jobs, [failed])
        self.assertEqual([j.id for j in Store(self.store.directory).jobs], [failed.id])
        self.assertEqual(self.media.read_bytes(), b"media fixture")
        self.assertEqual(self.queue.take_completions(), [])

    def test_busy_queue_does_not_block_ui(self):
        held, release = threading.Event(), threading.Event()
        def work():
            with self.queue.lock:
                held.set()
                release.wait(3)
        worker = threading.Thread(target=work)
        worker.start()
        held.wait(3)
        try:
            self.assertIsNone(self.queue.take_completions())
        finally:
            release.set()
            worker.join()

    def test_settings_round_trip_and_legacy_defaults(self):
        self.assertTrue(Settings().notifications)
        self.assertFalse(Settings().open_folder_on_completion)
        self.queue.configure(replace(self.store.settings, notifications=False, open_folder_on_completion=True))
        restored = Store(self.store.directory)
        self.assertFalse(restored.settings.notifications)
        self.assertTrue(restored.settings.open_folder_on_completion)
        self.assertTrue(Store.decode('{}')[0].notifications)
        with self.assertRaises(ValueError):
            Store.decode('{"settings":{"notifications":"yes"}}')
