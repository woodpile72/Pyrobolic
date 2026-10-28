# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
from dataclasses import replace
import json
import errno
import multiprocessing
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from pyrobolic.core import Job, Process, Queue, Store, discard_staging, prepare_staging, publish_file
from pyrobolic.linux_files import rename_no_replace
from test_core import await_idle


class TransferRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / "state")
        self.queue = Queue(self.store)
        self.job = Job("https://example.org/test", "Fixture", str(self.root),
                       filename="Fixture", output_stem="Fixture", status="Interrupted")
        self.store.jobs.append(self.job)

    def test_pause_resume_process_group_and_cancel_while_paused(self):
        process = Process()
        process.grace = 0.1
        self.addCleanup(process.close)
        child = process.start([sys.executable, "-u", "-c", "import time; print('ready'); time.sleep(60)"])
        self.assertEqual(child.stdout.readline().strip(), "ready")
        process.set_paused(True)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = Path(f"/proc/{child.pid}/stat").read_text().split(") ")[1][0]
            if state == "T":
                break
            time.sleep(0.01)
        self.assertEqual(state, "T")
        self.assertFalse(process.resumed.is_set())
        process.set_paused(False)
        self.assertTrue(process.resumed.is_set())
        process.set_paused(True)
        process.cancel()
        process.close()
        self.assertEqual(child.returncode, -signal.SIGTERM)
        self.assertTrue(process.done.is_set())

    def test_queue_pause_restart_and_cancellation(self):
        job = replace(self.job, status="Queued", id="transfer")
        ready = threading.Event()
        self.queue.on_change = lambda: ready.set() if job.detail.startswith("Inspecting source") else None
        with patch("pyrobolic.core.engine_path", return_value="unused"), \
                patch("pyrobolic.core.download_arguments", return_value=[sys.executable, "-c", "import time; time.sleep(60)"]):
            self.queue.add([job])
            self.assertTrue(ready.wait(5))
            self.queue.pause_transfer(job.id, True)
            self.assertTrue(job.transfer_paused)
            restored = Store(self.store.directory).jobs[-1]
            self.assertEqual(restored.status, "Interrupted")
            self.assertFalse(restored.transfer_paused)
            self.queue.cancel(job.id)
            await_idle(self.queue)
        self.assertEqual(job.status, "Canceled")
        self.assertFalse(job.transfer_paused)
        with self.assertRaises(RuntimeError):
            self.queue.pause_transfer(job.id, True)

    def test_discard_preserves_final_hard_link_and_retries_cleanly(self):
        stage = prepare_staging(self.job)
        partial = stage / "media.mp4"
        partial.write_bytes(b"completed")
        final = self.root / "Fixture.mp4"
        final.hardlink_to(partial)
        (stage / "media.mp4.part").write_bytes(b"partial")
        self.queue.discard_partial(self.job.id)
        self.assertFalse(stage.exists())
        self.assertEqual(final.read_bytes(), b"completed")
        self.assertTrue(prepare_staging(self.job).is_dir())

    def test_discard_rejects_foreign_and_symlink_files_before_removing_anything(self):
        stage = prepare_staging(self.job)
        partial = stage / "media.mp4.part"
        partial.write_bytes(b"retained")
        foreign = stage / "do-not-touch"
        foreign.write_bytes(b"foreign")
        with self.assertRaises(RuntimeError):
            discard_staging(self.job)
        self.assertEqual(partial.read_bytes(), b"retained")
        foreign.unlink()
        (stage / "media.link").symlink_to(partial)
        with self.assertRaises(RuntimeError):
            discard_staging(self.job)
        self.assertEqual(partial.read_bytes(), b"retained")

    def test_discard_rejects_other_owner_and_active_job(self):
        stage = prepare_staging(self.job)
        (stage / "owner.json").write_text(json.dumps({"job": "other", "url": self.job.url}))
        with self.assertRaises(RuntimeError):
            self.queue.discard_partial(self.job.id)
        self.job.status = "Downloading"
        with self.assertRaises(RuntimeError):
            self.queue.discard_partial(self.job.id)

    def test_remove_and_clear_rollback_on_save_failure(self):
        self.store.save()
        with patch.object(self.store, "save", side_effect=OSError("fixture full")):
            with self.assertRaises(OSError):
                self.queue.remove_history({self.job.id})
        self.assertEqual(self.store.jobs, [self.job])
        self.assertEqual(len(Store(self.store.directory).jobs), 1)
        self.queue.retry_save()
        stage = prepare_staging(self.job)
        self.queue.clear_history()
        self.assertEqual(Store(self.store.directory).jobs, [])
        self.assertFalse(stage.exists())

    def test_removal_cannot_race_worker_cleanup(self):
        self.queue.running[self.job.id] = Process()
        with self.assertRaises(RuntimeError):
            self.queue.remove_history({self.job.id})
        with self.assertRaises(RuntimeError):
            self.queue.discard_partial(self.job.id)

    def test_abandoned_cleanup_preserves_known_and_unrecognized_data(self):
        self.store.settings.output = str(self.root)
        known = prepare_staging(self.job)
        orphan = prepare_staging(replace(self.job, id="orphan"))
        (orphan / "media.mp4.part").write_bytes(b"partial")
        foreign = orphan.parent / "foreign"
        foreign.mkdir()
        (foreign / "valuable").write_bytes(b"do not remove")
        published = self.root / "finished.mp4"
        published.write_bytes(b"completed")
        self.assertEqual(self.queue.clean_abandoned(), (1, 1))
        self.assertTrue(known.exists())
        self.assertFalse(orphan.exists())
        self.assertEqual((foreign / "valuable").read_bytes(), b"do not remove")
        self.assertEqual(published.read_bytes(), b"completed")

    def test_completed_leftovers_are_swept_but_running_and_retryable_are_kept(self):
        stage = prepare_staging(self.job)
        self.job.status = "Completed"
        self.queue.running[self.job.id] = Process()
        self.assertEqual(self.queue.clean_abandoned(), (0, 0))
        self.assertTrue(stage.exists())
        self.queue.running.clear()
        for status in ("Failed", "Canceled", "Interrupted", "Queued", "Downloading"):
            self.job.status = status
            self.assertEqual(self.queue.clean_abandoned(), (0, 0))
            self.assertTrue(stage.exists())
        self.job.status = "Completed"
        self.assertEqual(self.queue.clean_abandoned(), (1, 0))
        self.assertFalse(stage.exists())

    def test_removal_preserves_partial_when_history_save_fails(self):
        stage = prepare_staging(self.job)
        partial = stage / "media.mp4.part"
        partial.write_bytes(b"resume me")
        with patch.object(self.store, "save", side_effect=OSError("full")):
            with self.assertRaises(OSError):
                self.queue.remove_history({self.job.id})
        self.assertEqual(partial.read_bytes(), b"resume me")

    def test_auto_clear_removes_staging_not_downloaded_file(self):
        stage = prepare_staging(self.job)
        (stage / "media.mp4").write_bytes(b"temporary")
        final = self.root / "finished.mp4"
        final.write_bytes(b"keep")
        self.job.files = [str(final)]
        self.job.status = "Completed"
        self.store.settings.history_retention_seconds = -1
        self.assertEqual(self.queue.prune_history(), 1)
        self.assertFalse(stage.exists())
        self.assertEqual(final.read_bytes(), b"keep")
        self.assertEqual(Store(self.store.directory).jobs, [])

    def test_cleanup_failure_keeps_owner_marker_and_committed_settings(self):
        stage = prepare_staging(self.job)
        (stage / "foreign.txt").write_bytes(b"keep")
        self.job.status = "Completed"
        with self.assertRaisesRegex(OSError, "History removed"):
            self.queue.set_history_clear(-1)
        self.assertEqual(self.store.settings.history_retention_seconds, -1)
        self.assertEqual(Store(self.store.directory).settings.history_retention_seconds, -1)
        self.assertTrue((stage / "owner.json").exists())
        self.assertEqual((stage / "foreign.txt").read_bytes(), b"keep")

    def test_copy_crash_never_exposes_partial_final_name(self):
        stage = prepare_staging(self.job)
        source, target = stage / "media.mp4", self.root / "Fixture.mp4"
        source.write_bytes(b"fixture media")
        def crash():
            with patch("pyrobolic.core.os.link", side_effect=OSError(errno.EOPNOTSUPP, "fixture")), \
                    patch("pyrobolic.core.os.fsync", side_effect=lambda *_: os._exit(23)):
                publish_file(source, target, threading.Event(), temporary_directory=stage)
        child = multiprocessing.get_context("fork").Process(target=crash)
        child.start()
        child.join(5)
        if child.is_alive():
            child.kill()
            child.join()
            self.fail("Crash fixture did not exit")
        self.assertEqual(child.exitcode, 23)
        self.assertFalse(target.exists())
        self.assertTrue(list(stage.glob("media.publish-*")))
        discard_staging(self.job)
        self.assertFalse(stage.exists())

    def test_copy_publication_preserves_concurrent_creator(self):
        stage = prepare_staging(self.job)
        source, target = stage / "media.mp4", self.root / "Fixture.mp4"
        source.write_bytes(b"fixture media")
        def race(temporary, destination):
            destination.write_bytes(b"concurrent creator")
            rename_no_replace(temporary, destination)
        with patch("pyrobolic.core.os.link", side_effect=OSError(errno.EOPNOTSUPP, "fixture")), \
                patch("pyrobolic.core.rename_no_replace", side_effect=race), self.assertRaises(FileExistsError):
            publish_file(source, target, threading.Event(), temporary_directory=stage)
        self.assertEqual(target.read_bytes(), b"concurrent creator")
        self.assertEqual(source.read_bytes(), b"fixture media")
        self.assertFalse(list(stage.glob("media.publish-*")))
