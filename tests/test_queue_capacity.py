# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Queue capacity tests with real, controlled subprocesses and no network access."""
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from pyrobolic.core import Job, Queue, Store
from test_core import await_idle


WORKER = """
import json, pathlib, sys, time
root, identifier = pathlib.Path(sys.argv[1]), sys.argv[2]
(root / (identifier + '.started')).touch()
print('PYRO_PROGRESS:' + json.dumps({'downloaded_bytes': 1, 'total_bytes': 2}), flush=True)
deadline = time.monotonic() + 30
while not (root / (identifier + '.release')).exists():
    if time.monotonic() >= deadline:
        raise SystemExit('Fixture release timed out')
    time.sleep(.02)
output = root / (identifier + '.mp4')
output.write_bytes(b'synthetic output, not encoded media')
print('PYRO_FILE:' + json.dumps(str(output)), flush=True)
"""


class QueueCapacityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / "state")
        self.store.settings.concurrency = 10
        self.queue = Queue(self.store)
        for patcher in (
            patch("pyrobolic.core.engine_path", return_value="fixture"),
            patch("pyrobolic.core.shutil.which", return_value="fixture"),
            patch("pyrobolic.core.download_arguments", side_effect=lambda job, _engine:
                  [sys.executable, "-c", WORKER, str(self.root), job.id]),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.stop)

    def stop(self):
        self.queue.shutdown()
        await_idle(self.queue, 15)

    def job(self, number, *, url=None):
        return Job(url or f"https://example.org/{number}", f"Fixture {number}",
                   str(self.root), metadata=False)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with self.queue.lock:
                if predicate():
                    return
            time.sleep(.02)
        self.fail("Queue condition timed out")

    def started(self, job):
        return (self.root / (job.id + ".started")).exists()

    def release(self, job):
        (self.root / (job.id + ".release")).touch()

    def test_ten_slots_skip_blocked_duplicate_then_admit_it_without_starvation(self):
        first = self.job(0)
        duplicate = self.job(1, url=first.url)
        others = [self.job(n) for n in range(2, 13)]
        jobs = [first, duplicate, *others]
        self.queue.add(jobs)
        initial = [first, *others[:9]]
        self.wait_for(lambda: all(self.started(job) for job in initial))
        with self.queue.lock:
            self.assertEqual(set(self.queue.running), {job.id for job in initial})
            processes = list(self.queue.running.values())
            self.assertEqual(duplicate.status, "Queued")
            self.assertTrue(all(job.status == "Queued" for job in others[9:]))

        self.release(first)
        self.wait_for(lambda: self.started(duplicate))
        with self.queue.lock:
            self.assertEqual(first.status, "Completed")
            self.assertEqual(len(self.queue.running), 10)
            self.assertFalse(self.started(others[9]))
            self.assertTrue(processes[0].done.is_set())
        for job in jobs:
            self.release(job)
        await_idle(self.queue, 15)
        self.assertTrue(all(job.status == "Completed" for job in jobs))
        self.assertEqual(len({job.files[0] for job in jobs}), len(jobs))
        self.assertTrue(all(Path(job.files[0]).stat().st_size > 0 for job in jobs))
        self.assertTrue(all(job.status == "Completed" for job in Store(self.store.directory).jobs))
        events = self.queue.take_completions()
        self.assertEqual(sum(len(event.jobs) for event in events), len(jobs))
        self.assertEqual(self.queue.take_completions(), [])

    def test_cancel_frees_one_slot_and_progress_leaves_history_untouched(self):
        historical = [self.job(n) for n in range(100, 1100)]
        for job in historical:
            job.status = "Completed"
        self.store.jobs.extend(historical)
        jobs = [self.job(n) for n in range(12)]
        self.queue.add(jobs)
        self.wait_for(lambda: all(job.progress == .5 for job in jobs[:10]))
        self.queue.changes()
        with self.queue.lock:
            self.queue._notify(persist=False, job=jobs[0])
        full, changed, *_ = self.queue.changes()
        self.assertFalse(full)
        self.assertEqual([job.id for job in changed], [jobs[0].id])
        with self.queue.lock:
            canceled_process = self.queue.running[jobs[0].id]
        self.queue.cancel(jobs[0].id)
        self.wait_for(lambda: self.started(jobs[10]))
        with self.queue.lock:
            self.assertEqual(jobs[0].status, "Canceled")
            self.assertEqual(len(self.queue.running), 10)
            self.assertEqual(jobs[11].status, "Queued")
        self.assertTrue(canceled_process.done.is_set())
        self.assertIsNotNone(canceled_process.process.poll())
        for job in jobs:
            self.release(job)
        await_idle(self.queue, 15)
        self.assertTrue(all(job.status == "Completed" for job in jobs[1:]))
        self.assertEqual(jobs[0].files, [])
        events = self.queue.take_completions()
        self.assertEqual(sum(len(event.jobs) for event in events), 11)
        self.assertEqual(sum(event.canceled for event in events), 1)

    def test_lower_limit_does_not_kill_workers_and_shutdown_reaps_all(self):
        jobs = [self.job(n) for n in range(12)]
        self.queue.add(jobs)
        self.wait_for(lambda: all(self.started(job) for job in jobs[:10]))
        with self.queue.lock:
            processes = list(self.queue.running.values())
        self.queue.configure(replace(self.store.settings, concurrency=2))
        self.assertEqual(len(self.queue.running), 10)
        for job in jobs[:8]:
            self.release(job)
        self.wait_for(lambda: len(self.queue.running) == 2)
        self.assertTrue(all(job.status == "Queued" for job in jobs[10:]))
        self.queue.shutdown()
        await_idle(self.queue, 15)
        self.assertTrue(all(process.done.is_set() for process in processes))
        self.assertTrue(all(process.process.poll() is not None for process in processes))
        self.assertEqual([job.status for job in jobs[10:]], ["Interrupted"] * 2)
        self.assertFalse(any(self.started(job) for job in jobs[10:]))

    def test_shutdown_at_full_capacity_reaps_ten_and_never_starts_waiting_jobs(self):
        jobs = [self.job(n) for n in range(12)]
        self.queue.add(jobs)
        self.wait_for(lambda: all(self.started(job) for job in jobs[:10]))
        with self.queue.lock:
            processes = list(self.queue.running.values())
        self.queue.shutdown()
        await_idle(self.queue, 15)
        self.assertEqual(len(processes), 10)
        self.assertTrue(all(process.done.is_set() for process in processes))
        self.assertTrue(all(process.process.poll() is not None for process in processes))
        self.assertEqual([job.status for job in jobs[:10]], ["Canceled"] * 10)
        self.assertEqual([job.status for job in jobs[10:]], ["Interrupted"] * 2)
        self.assertFalse(any(self.started(job) for job in jobs[10:]))
        self.assertEqual(self.queue.take_completions(), [])
