# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from pyrobolic.core import Job, Process, Queue, Settings, Store, startup_detail, verify_outputs
from test_core import await_idle


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "test.mp4"
        self.output.write_bytes(b"test media")

    def job(self, **kwargs):
        return Job("https://example.org/media", "Test", str(self.root), **kwargs)

    def script(self, paths):
        return ";".join("print(" + repr("PYRO_FILE:" + json.dumps(str(p))) + ")" for p in paths)

    def execute(self, queue, jobs, script):
        with patch("pyrobolic.core.engine_path", return_value="unused"), \
                patch("pyrobolic.core.shutil.which", return_value="present"), \
                patch("pyrobolic.core.download_arguments", return_value=[sys.executable, "-c", script]):
            queue.add(jobs)
            await_idle(queue)

    def test_startup_feedback_is_elapsed_not_a_progress_estimate(self):
        self.assertEqual(startup_detail(False, 5.9), "Inspecting source  \u00b7  0:05 elapsed")
        self.assertIn("Starting transfer", startup_detail(True, 15))
        self.assertIn("1:02 elapsed", startup_detail(False, 62))
        self.assertIn("No transfer progress reported yet", startup_detail(True, 62))
        self.assertNotIn("%", startup_detail(False, 62))

    def test_startup_feedback_changes_phase_and_stops_at_transfer(self):
        queue = Queue(Store(self.root))
        job = self.job()
        reports = []
        queue.on_change = lambda: reports.append((job.detail, job.progress))
        selection = "PYRO_SELECTION:" + json.dumps({"format_id": "fixture"})
        progress = 'PYRO_PROGRESS:{"downloaded_bytes": 50, "total_bytes": 100}'
        script = ("import time; time.sleep(0.12); "
                  f"print({selection!r}, flush=True); time.sleep(0.12); "
                  f"print({progress!r}, flush=True); time.sleep(0.12); " + self.script([self.output]))
        with patch("pyrobolic.core.STARTUP_FEEDBACK_INTERVAL", 0.02):
            self.execute(queue, [job], script)
        self.assertEqual(job.status, "Completed")
        self.assertTrue(any(detail.startswith("Inspecting source") for detail, _ in reports))
        self.assertTrue(any(detail.startswith("Starting transfer") for detail, _ in reports))
        self.assertTrue(any(progress == 0.5 for _, progress in reports))
        self.assertFalse(any(detail.startswith(("Inspecting source", "Starting transfer"))
                             for detail, progress in reports if progress > 0))
        count = len(reports)
        time.sleep(0.06)
        self.assertEqual(len(reports), count)

    def test_cancel_and_failure_stop_startup_feedback(self):
        queue = Queue(Store(self.root))
        job = self.job()
        ready = threading.Event()
        queue.on_change = lambda: ready.set() if job.detail.startswith("Inspecting source") else None
        with patch("pyrobolic.core.engine_path", return_value="unused"), \
                patch("pyrobolic.core.shutil.which", return_value="present"), \
                patch("pyrobolic.core.STARTUP_FEEDBACK_INTERVAL", 0.02), \
                patch("pyrobolic.core.download_arguments", return_value=[sys.executable, "-c", "import time; time.sleep(30)"]):
            queue.add([job])
            self.assertTrue(ready.wait(5))
            queue.cancel(job.id)
            await_idle(queue)
        self.assertEqual(job.status, "Canceled")
        failed = self.job()
        with patch("pyrobolic.core.STARTUP_FEEDBACK_INTERVAL", 0.02):
            self.execute(queue, [failed], "import sys; sys.exit(1)")
        self.assertEqual(failed.status, "Failed")
        time.sleep(0.06)
        self.assertFalse(any(t.name == "pyro-startup-feedback" for t in threading.enumerate()))

    def test_completion_save_failure_does_not_strand_next_job(self):
        store = Store(self.root)
        store.settings.concurrency = 1
        queue = Queue(store)
        original = store.save
        def save():
            if any(job.status == "Completed" for job in store.jobs):
                raise OSError("Disk full")
            original()
        with patch.object(store, "save", side_effect=save):
            self.execute(queue, [self.job(), self.job()], self.script([self.output]))
            self.assertEqual([job.status for job in store.jobs], ["Completed", "Completed"])
            self.assertEqual(queue.persistence_error, "Disk full")
            with self.assertRaises(OSError):
                queue.add([self.job()])
        queue.retry_save()
        self.assertFalse(queue.persistence_error)
        self.assertEqual([job.status for job in Store(self.root).jobs], ["Completed", "Completed"])

    def test_failed_save_does_not_prevent_cancel_and_shutdown(self):
        queue = Queue(Store(self.root))
        queue.set_paused(True)
        job = self.job()
        queue.add([job])
        with patch.object(queue.store, "save", side_effect=PermissionError("Read only")):
            queue.cancel_all()
            queue.shutdown()
        self.assertEqual(job.status, "Canceled")
        self.assertTrue(queue.stopped)
        self.assertTrue(queue.persistence_error)

    def test_zero_exit_with_invalid_output_is_not_success(self):
        empty = self.root / "empty.mp4"
        empty.touch()
        for output in (self.root / "missing.mp4", empty, self.root):
            with self.subTest(output=output):
                queue = Queue(Store(self.root / "state"))
                job = self.job()
                self.execute(queue, [job], self.script([output]))
                self.assertEqual(job.status, "Failed")

    def test_every_reported_output_must_be_valid(self):
        queue = Queue(Store(self.root))
        job = self.job()
        self.execute(queue, [job], self.script([self.output, self.root / "missing.mp4"]))
        self.assertEqual(job.status, "Failed")

    def test_cannot_stat_output_is_not_success(self):
        with patch.object(Path, "stat", side_effect=PermissionError("Denied")), self.assertRaises(RuntimeError):
            verify_outputs([str(self.output)])

    def test_invalid_reported_path_is_rejected(self):
        for value in (None, {}, [], ""):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                verify_outputs([value])

    def test_valid_output_is_accepted_without_media_probe(self):
        verify_outputs([str(self.output)])

    def test_fresh_start_preserves_exact_damaged_bytes(self):
        original = b'{"settings":\xff\x00broken'
        (self.root / "state.json").write_bytes(original)
        store, backup = Store.recover(self.root)
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(store.jobs, [])
        self.assertEqual(store.settings, Settings())
        self.assertEqual(self.output.read_bytes(), b"test media")

    def test_restore_valid_file_preserves_damaged_state_and_interrupts_jobs(self):
        (self.root / "state.json").write_bytes(b"broken")
        source = self.root / "backup.json"
        source.write_text(json.dumps({"settings": {"concurrency": 3}, "jobs": [asdict(self.job())]}))
        before = source.read_bytes()
        store, preserved = Store.recover(self.root, source)
        self.assertEqual(store.jobs[0].status, "Interrupted")
        self.assertEqual(store.settings.concurrency, 3)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(preserved.read_bytes(), b"broken")

    def test_invalid_replacement_does_not_touch_original(self):
        path = self.root / "state.json"
        path.write_bytes(b"original")
        source = self.root / "replacement.json"
        source.write_bytes(b"also broken")
        with self.assertRaises(ValueError):
            Store.recover(self.root, source)
        self.assertEqual(path.read_bytes(), b"original")
        self.assertFalse(list(self.root.glob("state-damaged-*")))

    def test_unreadable_original_cannot_be_replaced(self):
        path = self.root / "state.json"
        path.write_bytes(b"original")
        with patch.object(Path, "read_bytes", side_effect=PermissionError("Denied")), \
                self.assertRaises(PermissionError):
            Store.recover(self.root)
        self.assertEqual(path.read_bytes(), b"original")

    def test_failed_preservation_leaves_original_untouched(self):
        path = self.root / "state.json"
        path.write_bytes(b"original")
        with patch("pyrobolic.core.os.fsync", side_effect=OSError("Disk full")), self.assertRaises(OSError):
            Store.recover(self.root)
        self.assertEqual(path.read_bytes(), b"original")

    def test_failed_replacement_keeps_original_and_preserved_copy(self):
        path = self.root / "state.json"
        path.write_bytes(b"original")
        with patch("pyrobolic.core.atomic_json", side_effect=OSError("Disk full")), self.assertRaises(OSError):
            Store.recover(self.root)
        self.assertEqual(path.read_bytes(), b"original")
        self.assertEqual(next(self.root.glob("state-damaged-*")).read_bytes(), b"original")

    def test_invalid_state_structures_raise_without_writing(self):
        job = asdict(self.job())
        cases = [[], {"settings": []}, {"settings": {"output": None}},
                 {"settings": {"concurrency": "three"}}, {"settings": {"metadata": "false"}},
                 {"paused": "yes"}, {"jobs": {}}, {"jobs": [job, job]},
                 {"jobs": [dict(job, status="unknown")]}, {"jobs": [dict(job, progress=float("nan"))]},
                 {"jobs": [dict(job, files="/tmp/wrong")]}, {"jobs": [dict(job, title=None)]}]
        for data in cases:
            with self.subTest(data=data):
                path = self.root / "state.json"
                content = json.dumps(data)
                path.write_text(content)
                with self.assertRaises((ValueError, TypeError)):
                    Store(self.root)
                self.assertEqual(path.read_text(), content)

    def test_progress_copies_only_changed_job_not_history(self):
        store = Store(self.root)
        store.jobs = [self.job(status="Completed", log=["entry"] * 200) for _ in range(10000)]
        current = self.job(status="Downloading")
        store.jobs.append(current)
        queue = Queue(store)
        self.assertTrue(queue.changes()[0])
        with queue.lock:
            current.progress = 0.5
            queue._notify(persist=False, job=current)
        full, jobs, _, _ = queue.changes()
        self.assertFalse(full)
        self.assertEqual([job.id for job in jobs], [current.id])
        jobs[0].log.append("not in source")
        self.assertEqual(current.log, [])
        self.assertEqual(queue.changes()[1], [])

    def test_ui_changes_never_wait_for_save_lock(self):
        queue = Queue(Store(self.root))
        locked, release = threading.Event(), threading.Event()
        def hold():
            with queue.lock:
                locked.set()
                release.wait(3)
        worker = threading.Thread(target=hold)
        worker.start()
        self.assertTrue(locked.wait(2))
        try:
            start = time.monotonic()
            self.assertIsNone(queue.changes())
            self.assertLess(time.monotonic() - start, 0.1)
        finally:
            release.set()
            worker.join()


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux process group supervision")
class ProcessTests(unittest.TestCase):
    def test_natural_completion_is_not_cancellation(self):
        process = Process()
        self.assertEqual(process.communicate([sys.executable, "-c", "print('done')"]).strip(), "done")
        self.assertFalse(process.cancelled.is_set())
        self.assertTrue(process.done.is_set())

    def test_timeout_reaps_the_child(self):
        process = Process()
        process.grace = 0.1
        with self.assertRaises(subprocess.TimeoutExpired):
            process.communicate([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.1)
        self.assertTrue(process.done.is_set())
        self.assertIsNotNone(process.process.poll())

    def test_descendant_killed_even_after_parent_exits(self):
        process = Process()
        process.grace = 0.15
        child_code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready',flush=True); time.sleep(30)"
        parent_code = (
            "import subprocess,sys; "
            f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}],stdout=subprocess.PIPE,text=True); "
            "p.stdout.readline(); print(p.pid,flush=True)"
        )
        child = process.start([sys.executable, "-c", parent_code])
        descendant = int(child.stdout.readline())
        try:
            child.wait(timeout=2)
            self.assertTrue(process.done.wait(3))
            self.assert_descendant_stopped(descendant)
        finally:
            process.close()
            try:
                os.kill(descendant, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def assert_descendant_stopped(self, pid):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                state = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()[0]
            except FileNotFoundError:
                return
            if state == "Z":
                return  # An orphan's final reap belongs to its new parent, not this process.
            time.sleep(0.02)
        self.fail("Owned descendant is still running")

    def test_cancel_ignoring_term_and_unrelated_process_survives(self):
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        process = Process()
        process.grace = 0.1
        try:
            child = process.start([sys.executable, "-c",
                                   "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(30)"])
            child.stdout.readline()
            process.cancel()
            process.close()
            self.assertEqual(child.returncode, -signal.SIGKILL)
            self.assertIsNone(unrelated.poll())
        finally:
            process.close()
            unrelated.terminate()
            unrelated.wait(timeout=2)

    def test_worker_exception_cleans_up_subprocess(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            queue = Queue(store)
            job = Job("https://example.org/video", "Test", directory)
            process = Process()
            process.grace = 0.1
            queue.running[job.id] = process
            code = "import time; print('trigger',flush=True); time.sleep(30)"
            with patch("pyrobolic.core.engine_path", return_value="unused"), \
                    patch("pyrobolic.core.shutil.which", return_value="present"), \
                    patch("pyrobolic.core.download_arguments", return_value=[sys.executable, "-c", code]), \
                    patch("pyrobolic.core.progress_update", side_effect=RuntimeError("fixture failure")):
                queue._download(job, process)
            self.assertEqual(job.status, "Failed")
            self.assertTrue(process.done.is_set())
            self.assertIsNotNone(process.process.poll())
            self.assertFalse(queue.running)
