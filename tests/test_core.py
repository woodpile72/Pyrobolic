# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
import hashlib
import io
import json
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from pyrobolic.core import (Job, Process, Queue, Settings, Store, download_arguments,
                            inspect_url, progress_update, validate_url)
from pyrobolic.updater import install_verified


def await_idle(queue, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with queue.lock:
            if not queue.running and not any(j.status == "Queued" for j in queue.store.jobs):
                return
        time.sleep(0.02)
    raise AssertionError("Queue did not finish")


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def job(self, **kwargs):
        return Job("https://example.org/media", "A title", str(self.root), **kwargs)

    def success_script(self):
        output = self.root / "test.mp4"
        output.write_bytes(b"fixture output")
        return "print(" + repr("PYRO_FILE:" + json.dumps(str(output))) + ")"

    def test_url_validation(self):
        self.assertEqual(validate_url(" https://example.org/a?b=2 "), "https://example.org/a?b=2")
        for value in ("file:///etc/passwd", "--exec=bad", "https://", "ftp://host/file",
                      "https://user:password@host/a", "https://example.org/a b"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_url(value)

    def test_video_arguments_preserve_best_quality_without_transcoding(self):
        args = download_arguments(self.job(), "/usr/bin/yt-dlp")
        self.assertEqual(args[args.index("-f") + 1], "bestvideo+bestaudio/best")
        self.assertNotIn("--recode-video", args)
        self.assertIn("--ignore-config", args)
        self.assertEqual(args[-2:], ["--", "https://example.org/media"])

    def test_resolution_limit_applies_to_combined_fallback(self):
        args = download_arguments(self.job(quality=720), "yt-dlp")
        self.assertEqual(args[args.index("-f") + 1], "bestvideo[height<=720]+bestaudio/best[height<=720]")

    def test_audio_profile(self):
        args = download_arguments(self.job(mode="audio", audio_format="mp3"), "yt-dlp")
        self.assertIn("--extract-audio", args)
        self.assertEqual(args[args.index("--audio-format") + 1], "mp3")
        self.assertNotIn("--merge-output-format", args)

    def test_unknown_format_rejected(self):
        with self.assertRaises(ValueError):
            download_arguments(self.job(audio_format="--exec"), "yt-dlp")

    def test_audio_and_video_use_different_output_names(self):
        video = download_arguments(self.job(), "yt-dlp")
        audio = download_arguments(self.job(mode="audio"), "yt-dlp")
        self.assertNotEqual(video[video.index("--output") + 1], audio[audio.index("--output") + 1])

    def test_requested_container_only_remuxes(self):
        args = download_arguments(self.job(container="mp4"), "yt-dlp")
        self.assertEqual(args[args.index("--remux-video") + 1], "mp4")
        self.assertNotIn("--recode-video", args)

    def test_progress_handles_unknown_size(self):
        result = progress_update('PYRO_PROGRESS:{"downloaded_bytes":500,"total_bytes":null}')
        self.assertEqual(result["progress"], 0)
        result = progress_update('PYRO_PROGRESS:{"downloaded_bytes":500,"total_bytes":1000,"eta":65}')
        self.assertEqual(result["progress"], 0.5)
        self.assertIn("1:05", result["detail"])

    def test_quoted_unicode_file_path(self):
        value = '/tmp/a "quote" and \u00e9.mp4'
        self.assertEqual(progress_update("PYRO_FILE:" + json.dumps(value)), {"file": value})

    def test_save_reload_recovers_interrupted_jobs(self):
        store = Store(self.root)
        store.jobs = [self.job(status="Downloading"), self.job(status="Queued"), self.job(status="Completed")]
        store.save()
        restored = Store(self.root)
        self.assertEqual([j.status for j in restored.jobs], ["Interrupted", "Interrupted", "Completed"])
        self.assertEqual(list(self.root.glob(".state-*")), [])

    def test_corrupted_state_not_silently_overwritten(self):
        (self.root / "state.json").write_text("corrupt")
        with self.assertRaises(json.JSONDecodeError):
            Store(self.root)
        self.assertEqual((self.root / "state.json").read_text(), "corrupt")

    def test_old_settings_load_with_new_defaults(self):
        (self.root / "state.json").write_text(json.dumps({"settings": {"concurrency": 3}, "jobs": []}))
        store = Store(self.root)
        self.assertEqual(store.settings.mode, "video")
        self.assertEqual(store.settings.quality, 0)
        self.assertEqual(store.settings.concurrency, 3)
        self.assertFalse(store.paused)

    def test_invalid_remembered_options_fall_back_to_valid_defaults(self):
        settings = Settings(mode="invalid", quality=5, audio_format="bad", container="bad")
        self.assertEqual((settings.mode, settings.quality, settings.audio_format, settings.container),
                         ("video", 0, "best", "auto"))

    def test_remember_options_with_successful_enqueue(self):
        store = Store(self.root)
        queue = Queue(store)
        queue.set_paused(True)
        options = Settings(output=str(self.root / "new-output"), mode="audio", quality=720,
                           audio_format="opus", container="mkv")
        queue.add([self.job()], options)
        restored = Store(self.root)
        self.assertEqual(restored.settings, options)
        self.assertTrue(restored.paused)

    def test_failed_save_rolls_back_added_jobs_and_options(self):
        store = Store(self.root)
        queue = Queue(store)
        old = store.settings
        with patch.object(store, "save", side_effect=OSError("Disk full")), self.assertRaises(OSError):
            queue.add([self.job()], Settings(mode="audio"))
        self.assertEqual(queue.snapshot(), [])
        self.assertIs(store.settings, old)
        self.assertFalse(queue.running)

    def test_inspection_playlist_ignores_unavailable_entries(self):
        data = {"_type": "playlist", "entries": [None, {"title": "One", "url": "https://example.org/1"},
                                                     {"title": "Invalid", "url": "javascript:bad"}]}
        script = "import json; print('WARNING: fixture'); print(" + repr(json.dumps(data)) + ")"
        with patch("pyrobolic.core.engine_path", return_value="unused"), \
                patch("pyrobolic.core.common_arguments", return_value=[sys.executable, "-c", script]):
            result = inspect_url("https://example.org/list", Process(), self.root)
        self.assertEqual([r["title"] for r in result], ["One"])
        self.assertTrue(result[0]["from_collection"])

    def test_inspection_preserves_collection_identity_for_single_item(self):
        for kind in ("video", "playlist", "multi_video"):
            item = {"title": "Fixture", "webpage_url": "https://example.org/one"}
            data = dict(item, _type=kind) if kind == "video" else {"_type": kind, "entries": [item]}
            script = "print(" + repr(json.dumps(data)) + ")"
            with self.subTest(kind=kind), patch("pyrobolic.core.engine_path", return_value="unused"), \
                    patch("pyrobolic.core.common_arguments", return_value=[sys.executable, "-c", script]):
                result = inspect_url("https://example.org/one", Process(), self.root)
                self.assertEqual(result[0]["from_collection"], kind != "video")

    def test_cancel_before_start(self):
        process = Process()
        process.cancel()
        with self.assertRaises(InterruptedError):
            process.start(["does-not-exist"])

    def test_queue_completion_failure_retry_and_history(self):
        store = Store(self.root)
        queue = Queue(store)
        code = self.success_script()
        with patch("pyrobolic.core.engine_path", return_value="unused"), \
                patch("pyrobolic.core.shutil.which", return_value="present"), \
                patch("pyrobolic.core.download_arguments", return_value=[sys.executable, "-c", code]):
            queue.add([self.job(), self.job()])
            await_idle(queue)
        self.assertEqual([j.status for j in queue.snapshot()], ["Completed", "Completed"])
        with patch("pyrobolic.core.engine_path", side_effect=RuntimeError("fixture failure")):
            job = self.job()
            queue.add([job])
            await_idle(queue)
        self.assertEqual(job.status, "Failed")
        with patch("pyrobolic.core.engine_path", return_value="unused"), \
                patch("pyrobolic.core.shutil.which", return_value="present"), \
                patch("pyrobolic.core.download_arguments", return_value=[sys.executable, "-c", code]):
            queue.retry(job.id)
            await_idle(queue)
        self.assertEqual(job.status, "Completed")
        queue.clear_history()
        self.assertEqual(queue.snapshot(), [])
        self.assertEqual(Store(self.root).jobs, [])

    def test_concurrency_limit_and_cancellation(self):
        store = Store(self.root)
        store.settings = Settings(concurrency=1)
        queue = Queue(store)
        code = "import time; print('started',flush=True); time.sleep(30)"
        with patch("pyrobolic.core.engine_path", return_value="unused"), \
                patch("pyrobolic.core.shutil.which", return_value="present"), \
                patch("pyrobolic.core.download_arguments", return_value=[sys.executable, "-c", code]):
            first, second = self.job(), self.job()
            queue.add([first, second])
            time.sleep(0.1)
            self.assertEqual(len(queue.running), 1)
            self.assertEqual(second.status, "Queued")
            queue.cancel(second.id)
            queue.cancel(first.id)
            await_idle(queue)
        self.assertEqual([j.status for j in queue.snapshot()], ["Canceled", "Canceled"])

    def test_no_output_does_not_report_success(self):
        queue = Queue(Store(self.root))
        with patch("pyrobolic.core.engine_path", return_value="unused"), \
                patch("pyrobolic.core.shutil.which", return_value="present"), \
                patch("pyrobolic.core.download_arguments", return_value=[sys.executable, "-c", "pass"]):
            job = self.job()
            queue.add([job])
            await_idle(queue)
        self.assertEqual(job.status, "Failed")

    def test_same_url_and_directory_are_not_written_concurrently(self):
        store = Store(self.root)
        store.settings.concurrency = 6
        queue = Queue(store)
        code = "import time; time.sleep(30)"
        with patch("pyrobolic.core.engine_path", return_value="unused"), \
                patch("pyrobolic.core.shutil.which", return_value="present"), \
                patch("pyrobolic.core.download_arguments", return_value=[sys.executable, "-c", code]):
            queue.add([self.job(), self.job(mode="audio")])
            self.assertEqual(len(queue.running), 1)
            queue.shutdown()
            await_idle(queue)
        self.assertEqual(queue.snapshot()[1].status, "Interrupted")

    def test_paused_queue_does_not_start_until_resumed(self):
        queue = Queue(Store(self.root))
        queue.set_paused(True)
        code = self.success_script()
        with patch("pyrobolic.core.engine_path", return_value="unused"), \
                patch("pyrobolic.core.shutil.which", return_value="present"), \
                patch("pyrobolic.core.download_arguments", return_value=[sys.executable, "-c", code]) as arguments:
            job = self.job()
            queue.add([job])
            self.assertEqual(job.status, "Queued")
            arguments.assert_not_called()
            queue.set_paused(False)
            await_idle(queue)
        self.assertEqual(job.status, "Completed")
        self.assertFalse(Store(self.root).paused)

    def test_pausing_allows_active_download_to_finish_but_holds_next(self):
        store = Store(self.root)
        store.settings.concurrency = 1
        queue = Queue(store)
        code = 'import time; time.sleep(.2); ' + self.success_script()
        with patch("pyrobolic.core.engine_path", return_value="unused"), \
                patch("pyrobolic.core.shutil.which", return_value="present"), \
                patch("pyrobolic.core.download_arguments", return_value=[sys.executable, "-c", code]):
            first, second = self.job(), self.job()
            queue.add([first, second])
            queue.set_paused(True)
            deadline = time.monotonic() + 5
            while first.status != "Completed" and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual(first.status, "Completed")
            self.assertEqual(second.status, "Queued")
            queue.set_paused(False)
            await_idle(queue)
        self.assertEqual(second.status, "Completed")

    def test_cancel_all_includes_queued_and_running(self):
        store = Store(self.root)
        store.settings.concurrency = 1
        queue = Queue(store)
        with patch("pyrobolic.core.engine_path", return_value="unused"), \
                patch("pyrobolic.core.shutil.which", return_value="present"), \
                patch("pyrobolic.core.download_arguments", return_value=[sys.executable, "-c", "import time; time.sleep(30)"]):
            queue.add([self.job(), self.job(), self.job()])
            queue.cancel_all()
            await_idle(queue)
        self.assertEqual([j.status for j in queue.snapshot()], ["Canceled"] * 3)

    def test_retry_failed_preserves_completed_and_intentionally_canceled(self):
        store = Store(self.root)
        store.jobs = [self.job(status=status) for status in ("Failed", "Interrupted", "Canceled", "Completed")]
        queue = Queue(store)
        queue.set_paused(True)
        queue.retry_failed()
        self.assertEqual([j.status for j in queue.snapshot()], ["Queued", "Queued", "Canceled", "Completed"])
        self.assertFalse(queue.running)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.target = Path(self.temp.name) / "yt-dlp"
        self.target.write_bytes(b"old")

    def test_success_is_atomic_and_executable(self):
        content = b"new version"
        with self.target.open("rb") as old_reader:
            install_verified(io.BytesIO(content), self.target, hashlib.sha256(content).hexdigest(), validate=False)
            self.assertEqual(old_reader.read(), b"old")
        self.assertEqual(self.target.read_bytes(), content)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o755)
        self.assertEqual(list(self.target.parent.glob(".yt-dlp-*")), [])

    def test_update_succeeds_while_old_executable_is_running(self):
        shutil.copyfile("/bin/sleep", self.target)
        self.target.chmod(0o755)
        process = subprocess.Popen([str(self.target), "30"])
        try:
            content = Path("/bin/true").read_bytes()
            install_verified(io.BytesIO(content), self.target, hashlib.sha256(content).hexdigest())
            self.assertIsNone(process.poll())
            self.assertEqual(self.target.read_bytes(), content)
        finally:
            process.terminate()
            process.wait(timeout=3)

    def test_checksum_failure_preserves_old_version(self):
        with self.assertRaises(RuntimeError):
            install_verified(io.BytesIO(b"bad"), self.target, "0" * 64, validate=False)
        self.assertEqual(self.target.read_bytes(), b"old")
        self.assertEqual(list(self.target.parent.glob(".yt-dlp-*")), [])

    def test_size_limit_preserves_old_version(self):
        with patch("pyrobolic.updater.MAX_BINARY", 2), self.assertRaises(RuntimeError):
            install_verified(io.BytesIO(b"large"), self.target, "unused", validate=False)
        self.assertEqual(self.target.read_bytes(), b"old")

    def test_unusable_binary_preserves_old_version(self):
        content = b"not an executable"
        with self.assertRaises(OSError):
            install_verified(io.BytesIO(content), self.target, hashlib.sha256(content).hexdigest())
        self.assertEqual(self.target.read_bytes(), b"old")


if __name__ == "__main__":
    unittest.main()
