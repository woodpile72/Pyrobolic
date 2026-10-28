# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Literal filenames, per-job staging, and publication without replacing files."""

from dataclasses import asdict
import errno
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from pyrobolic.core import (Job, Process, Queue, Store, download_arguments,
                            prepare_staging, publish_file, staging_directory, discard_staging)
from pyrobolic.naming import (MAX_NAME_BYTES, available_stem, identifier,
                              output_stem, suggest_name, validate_name)


FACEBOOK_TITLE = ("1.5M views \u00b7 46K reactions \uff5c CONCERT!\U0001f1e8\U0001f1ed"
                  "We\u2019re bringing live music to you! \U0001f525\U0001f3b9 On October 30, 2026, "
                  "join our fictional band at the example music hall for an evening of songs")
FACEBOOK_NAME = "CONCERT!\U0001f1e8\U0001f1edWe\u2019re bringing live music to you!"


class SuggestionTests(unittest.TestCase):
    def test_reported_facebook_caption(self):
        self.assertEqual(suggest_name(FACEBOOK_TITLE, "https://www.facebook.com/1"), FACEBOOK_NAME)

    def test_counters_removed_only_from_facebook_prefix(self):
        for title in ("1984 - Live in 2026", "A million views on life", "Dr. Smith at 3.14", "What happened! The answer"):
            self.assertEqual(suggest_name(title, "https://facebook.com/1"), title)
        title = "12 views | The concert"
        self.assertEqual(suggest_name(title, "https://example.org/1"), "12 views The concert")
        self.assertEqual(suggest_name(title, source="Facebook"), "The concert")

    def test_localized_counters_and_meaningful_numbers(self):
        self.assertEqual(suggest_name("12 mil vistas \u00b7 230 reacciones | Concierto 2026", source="Facebook"), "Concierto 2026")
        self.assertEqual(suggest_name("1,5M Aufrufe | Konzert 2026", source="Facebook"), "Konzert 2026")

    def test_unicode_and_byte_limit(self):
        for title in ("\u97f3\u697d" * 100, "a " * 100, "\U0001f3b9" * 100, "CON." + "a" * 200):
            name = suggest_name(title)
            validate_name(name)
            self.assertLessEqual(len(name.encode()), MAX_NAME_BYTES)
        self.assertEqual(suggest_name("Cafe\u0301 2026"), "Caf\u00e9 2026")

    def test_sanitization_and_invalid_edits(self):
        self.assertEqual(suggest_name("../bad/name:\x00 \u202etest"), "bad name test")
        self.assertEqual(suggest_name("..."), "Video")
        for name in ("", "../outside", "bad\\name", "\x00", " name", "name.", "CON", "x" * 141):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_name(name)
        self.assertEqual(validate_name("100% $(literal) [safe]"), "100% $(literal) [safe]")

    def test_optional_id_and_stable_fallback(self):
        self.assertEqual(output_stem("Concert", "123", include_id=True), "Concert [123]")
        self.assertEqual(output_stem("Concert", "123", include_id=False), "Concert")
        self.assertEqual(identifier("../unsafe", "https://example.org/a"), identifier("", "https://example.org/a"))
        self.assertNotEqual(identifier("", "https://example.org/a"), identifier("", "https://example.org/b"))

    def test_numbered_case_insensitive_collisions(self):
        self.assertEqual(available_stem("Concert", ["concert.mp4", "Concert (2).en.vtt"], ["Concert (3)"]), "Concert (4)")
        self.assertEqual(available_stem("Caf\u00e9", ["Cafe\u0301.mp4"]), "Caf\u00e9 (2)")


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "output"
        self.output.mkdir()
        self.store = Store(self.root / "state")
        self.store.paused = True
        self.queue = Queue(self.store)

    def job(self, **kwargs):
        return Job(url="https://example.org/video", title="Raw title", output=str(self.output),
                   filename="Concert", include_id=False, **kwargs)

    def staged(self):
        job = self.job()
        self.queue.add([job])
        stage = prepare_staging(job)
        source = stage / "media.mp4"
        source.write_bytes(b"downloaded media")
        job.files = [str(source)]
        return job, stage, source

    def test_existing_files_and_batch_reservations(self):
        (self.output / "Concert.mp4").write_bytes(b"original")
        jobs = [self.job(), self.job()]
        self.queue.add(jobs)
        self.assertEqual([j.output_stem for j in jobs], ["Concert (2)", "Concert (3)"])
        self.assertEqual((self.output / "Concert.mp4").read_bytes(), b"original")
        restored = Store(self.root / "state")
        self.assertEqual(restored.jobs[0].output_stem, "Concert (2)")
        self.assertEqual(restored.jobs[0].filename, "Concert")

    def test_explicit_different_extensions_share_basename(self):
        existing = self.output / "Concert.webm"
        existing.write_bytes(b"keep webm")
        old = self.job(container="webm", status="Completed", output_stem="Concert")
        self.store.jobs.append(old)
        job = self.job(container="mp4")
        self.queue.add([job])
        self.assertEqual(job.output_stem, "Concert")
        stage = prepare_staging(job)
        source = stage / "media.mp4"
        source.write_bytes(b"new mp4")
        job.files = [str(source)]
        self.assertEqual(Path(self.queue._publish(job, stage, Process())[0]).name, "Concert.mp4")
        self.assertEqual(existing.read_bytes(), b"keep webm")

    def test_active_different_formats_reserve_only_overlapping_outputs(self):
        jobs = [self.job(container="mp4"), self.job(container="webm"), self.job(container="mp4")]
        self.queue.add(jobs)
        self.assertEqual([j.output_stem for j in jobs], ["Concert", "Concert", "Concert (2)"])

    def test_shared_description_still_reserves_whole_group(self):
        jobs = [self.job(container="mp4", save_description=True),
                self.job(container="webm", save_description=True)]
        self.queue.add(jobs)
        self.assertEqual([j.output_stem for j in jobs], ["Concert", "Concert (2)"])

    def test_automatic_format_rechecks_actual_extension_at_publication(self):
        (self.output / "Concert.webm").write_bytes(b"keep")
        job, stage, _ = self.staged()
        self.assertEqual(job.output_stem, "Concert (2)")
        target = self.queue._publish(job, stage, Process())[0]
        self.assertEqual(Path(target).name, "Concert.mp4")
        self.assertEqual(Store(self.root / "state").jobs[0].output_stem, "Concert")

    def test_actual_sidecar_collision_keeps_outputs_together(self):
        (self.output / "Concert.webm").write_bytes(b"keep video")
        (self.output / "Concert.en.vtt").write_bytes(b"keep captions")
        job = self.job(container="mp4", subtitle_mode="sidecar")
        self.queue.add([job])
        stage = prepare_staging(job)
        job.files = []
        for name in ("media.mp4", "media.en.vtt"):
            source = stage / name
            source.write_bytes(b"new output")
            job.files.append(str(source))
        result = self.queue._publish(job, stage, Process())
        self.assertEqual({Path(p).name for p in result}, {"Concert (2).mp4", "Concert (2).en.vtt"})
        self.assertEqual((self.output / "Concert.en.vtt").read_bytes(), b"keep captions")

    def test_extension_matching_preserves_unicode_and_case_collision_safety(self):
        self.assertEqual(available_stem("Caf\u00e9", ["Cafe\u0301.MP4"], suffixes={".mp4"}), "Caf\u00e9 (2)")
        self.assertEqual(available_stem("Concert", ["Concert.webm", "Concert.mp4.part"], suffixes={".mp4"}), "Concert")

    def test_late_race_uses_actual_suffixes_not_unrelated_formats(self):
        job = self.job(container="mp4")
        self.queue.add([job])
        stage = prepare_staging(job)
        source = stage / "media.mp4"
        source.write_bytes(b"ours")
        job.files = [str(source)]
        (self.output / "Concert (2).webm").write_bytes(b"unrelated")
        injected = False
        def race(source, target, canceled, **kwargs):
            nonlocal injected
            if not injected:
                injected = True
                target.write_bytes(b"concurrent writer")
            return publish_file(source, target, canceled, **kwargs)
        with patch("pyrobolic.core.publish_file", side_effect=race):
            result = self.queue._publish(job, stage, Process())
        self.assertEqual(Path(result[0]).name, "Concert (2).mp4")
        self.assertEqual((self.output / "Concert.mp4").read_bytes(), b"concurrent writer")

    def test_literal_template_and_unique_staging(self):
        first, second = self.job(), self.job()
        first.filename = "100% %(title)s $(literal)"
        self.queue.add([first, second])
        args = download_arguments(first, "yt-dlp")
        self.assertEqual(args[args.index("--output") + 1], "media.%(ext)s")
        self.assertNotIn(first.filename, args)
        self.assertNotEqual(staging_directory(first), staging_directory(second))

    def test_legacy_jobs_keep_legacy_template(self):
        old = asdict(self.job())
        for key in ("filename", "media_id", "include_id", "output_stem"):
            old.pop(key)
        _settings, _paused, jobs = Store.decode(json.dumps({"jobs": [old]}))
        args = download_arguments(jobs[0], "yt-dlp")
        self.assertIn("%(title)", args[args.index("--output") + 1])

    def test_invalid_saved_filename_pair_rejected(self):
        with self.assertRaises(ValueError):
            Store.decode(json.dumps({"jobs": [asdict(self.job())]}))

    def test_staging_rejects_foreign_owner(self):
        job, stage, _ = self.staged()
        (stage / "owner.json").write_text('{}')
        with self.assertRaises(RuntimeError):
            prepare_staging(job)

    def test_staging_rejects_symlink_without_writing_outside(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.output / ".pyrobolic").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            prepare_staging(self.job())
        self.assertEqual(list(outside.iterdir()), [])

    def test_publish_and_reuse_owned_output(self):
        job, stage, source = self.staged()
        target = self.queue._publish(job, stage, Process())[0]
        self.assertEqual(Path(target).name, "Concert.mp4")
        self.assertEqual(source.read_bytes(), Path(target).read_bytes())
        self.assertEqual(self.queue._publish(job, stage, Process()), [target])

    def test_late_collision_never_replaces_original(self):
        job, stage, _ = self.staged()
        original = self.output / "Concert.mp4"
        original.write_bytes(b"keep me")
        target = self.queue._publish(job, stage, Process())[0]
        self.assertEqual(Path(target).name, "Concert (2).mp4")
        self.assertEqual(original.read_bytes(), b"keep me")
        self.assertEqual(Store(self.root / "state").jobs[0].output_stem, "Concert (2)")

    def test_collision_after_first_file_rolls_back_only_own_files(self):
        job, stage, _ = self.staged()
        (stage / "media.en.vtt").write_bytes(b"WEBVTT")
        original = self.output / "Concert.en.vtt"
        original.write_bytes(b"keep subtitle")
        result = self.queue._publish(job, stage, Process())
        self.assertEqual({Path(p).name for p in result}, {"Concert (2).mp4", "Concert (2).en.vtt"})
        self.assertFalse((self.output / "Concert.mp4").exists())
        self.assertEqual(original.read_bytes(), b"keep subtitle")

    def test_collision_save_failure_stops_publication(self):
        job, stage, _ = self.staged()
        (self.output / "Concert.mp4").write_bytes(b"keep me")
        with patch.object(self.store, "save", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.queue._publish(job, stage, Process())
        self.assertEqual(job.output_stem, "Concert")
        self.assertFalse((self.output / "Concert (2).mp4").exists())

    def test_retry_detaches_hardlinks_before_postprocessing(self):
        job, stage, source = self.staged()
        target = Path(self.queue._publish(job, stage, Process())[0])
        self.assertTrue(source.samefile(target))
        prepare_staging(job)
        self.assertFalse(source.samefile(target))
        source.write_bytes(b"reprocessed")
        self.assertEqual(target.read_bytes(), b"downloaded media")

    def test_cancel_during_retry_preparation_preserves_outputs(self):
        job, stage, source = self.staged()
        target = Path(self.queue._publish(job, stage, Process())[0])
        canceled = threading.Event()
        canceled.set()
        with self.assertRaises(InterruptedError):
            prepare_staging(job, canceled)
        self.assertEqual(target.read_bytes(), b"downloaded media")
        self.assertTrue(source.samefile(target))
        self.assertEqual(list(stage.glob("detach-*")), [])

    def test_different_urls_with_same_name_do_not_reuse_media(self):
        first, second = self.job(), self.job()
        second.url = "https://example.org/different-video"
        self.queue.add([first, second])
        for job, content in ((first, b"first video"), (second, b"second video")):
            stage = prepare_staging(job)
            source = stage / "media.mp4"
            source.write_bytes(content)
            job.files = [str(source)]
            result = self.queue._publish(job, stage, Process())
            self.assertEqual(Path(result[0]).read_bytes(), content)
        self.assertEqual((self.output / "Concert.mp4").read_bytes(), b"first video")
        self.assertEqual((self.output / "Concert (2).mp4").read_bytes(), b"second video")

    def test_copy_fallback_reuses_journaled_files_but_not_changed_files(self):
        job, stage, _ = self.staged()
        with patch("pyrobolic.core.os.link", side_effect=OSError(errno.EOPNOTSUPP, "unsupported")):
            result = self.queue._publish(job, stage, Process())
            self.assertEqual(self.queue._publish(job, stage, Process()), result)
            Path(result[0]).write_bytes(b"changed by user")
            renamed = self.queue._publish(job, stage, Process())
        self.assertNotEqual(renamed, result)
        self.assertEqual(Path(result[0]).read_bytes(), b"changed by user")

    def test_copy_cancellation_removes_only_new_partial(self):
        _job, _stage, source = self.staged()
        target = self.output / "canceled.mp4"
        event = threading.Event()
        event.set()
        with patch("pyrobolic.core.os.link", side_effect=OSError(errno.EOPNOTSUPP, "unsupported")):
            with self.assertRaises(InterruptedError):
                publish_file(source, target, event)
            self.assertFalse(target.exists())
            target.write_bytes(b"existing")
            with self.assertRaises(FileExistsError):
                publish_file(source, target, event)
        self.assertEqual(target.read_bytes(), b"existing")

    def test_external_outputs_and_symlink_journal_rejected(self):
        job, stage, source = self.staged()
        job.files = [str(self.output / "outside.mp4")]
        with self.assertRaises(RuntimeError):
            self.queue._publish(job, stage, Process())
        job.files = [str(source)]
        (stage / "published.json").symlink_to(stage / "owner.json")
        with self.assertRaises(RuntimeError):
            self.queue._publish(job, stage, Process())

    def test_completed_cleanup_leaves_final_media(self):
        job, stage, _ = self.staged()
        target = Path(self.queue._publish(job, stage, Process())[0])
        discard_staging(job)
        self.assertFalse(stage.exists())
        self.assertEqual(target.read_bytes(), b"downloaded media")
