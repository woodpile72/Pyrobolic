# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
from dataclasses import replace
import errno
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from pyrobolic.batch import (BatchInspection, MAX_BATCH_BYTES, edited_entry, export_m3u, inspect_batch_url,
                            numbered_name, parse_batch, read_batch, replace_import_line)
from pyrobolic.core import Job, Queue, Store


class BatchTests(unittest.TestCase):
    def test_plain_tsv_comments_bom_and_unicode(self):
        rows = parse_batch("\ufeff# Batch\n\nhttps://example.org/a\r\n"
                           "https://example.org/b\tConcert \u00e9\t~/Downloads\n")
        self.assertEqual([r.line for r in rows], [3, 4])
        self.assertEqual(rows[1].name, "Concert \u00e9")
        self.assertEqual(rows[1].destination, str(Path.home() / "Downloads"))
        self.assertFalse(any(r.error for r in rows))

    def test_per_line_errors_and_duplicate_policy(self):
        rows = parse_batch("bad\nhttps://example.org/a\nhttps://example.org/a\n"
                           "https://example.org/b\t../bad\nhttps://example.org/c\t\trelative\n"
                           "https://example.org/d\tname\t/tmp\textra\n"
                           "https://example.org/a\tDifferent name\n")
        self.assertTrue(all(rows[i].error for i in (0, 2, 3, 4, 5)))
        self.assertFalse(rows[1].error)
        self.assertFalse(rows[6].error)
        self.assertIn("Duplicate", rows[2].error)

    def test_empty_limits_and_bad_files(self):
        for text in ("# comment", "x" * (MAX_BATCH_BYTES + 1),
                     "\n".join(f"https://example.org/{i}" for i in range(1001))):
            with self.assertRaises(ValueError):
                parse_batch(text)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "batch.txt"
            path.write_bytes(b"\xef\xbb\xbfhttps://example.org/a")
            self.assertEqual(read_batch(path), "https://example.org/a")
            path.write_bytes(b"\xff")
            with self.assertRaisesRegex(ValueError, "UTF-8"):
                read_batch(path)
            path.write_bytes(b"x" * (MAX_BATCH_BYTES + 1))
            with self.assertRaisesRegex(ValueError, "too large"):
                read_batch(path)
            with self.assertRaises(ValueError):
                read_batch(directory)

    def test_thousand_records(self):
        rows = parse_batch("\n".join(f"https://example.org/{i}" for i in range(1000)))
        self.assertEqual(len(rows), 1000)
        self.assertFalse(any(r.error for r in rows))

    def test_inspection_requests_one_more_than_limit(self):
        with patch("pyrobolic.batch.inspect_url", return_value=[]) as inspect:
            inspect_batch_url("https://example.org/playlist", None, None)
        self.assertEqual(inspect.call_args.kwargs, {"playlist_limit": 1001})

    def test_invalid_url_retains_name_and_destination_for_correction(self):
        entry = parse_batch("invalid\tChosen name\t/tmp/media")[0]
        self.assertTrue(entry.error)
        self.assertEqual((entry.name, entry.destination), ("Chosen name", "/tmp/media"))

    def test_edit_is_a_draft_and_changed_url_invalidates_only_its_catalog(self):
        rows = parse_batch("https://example.org/a\nhttps://example.org/b")
        BatchInspection().run(rows, None, inspect=self.inspect)
        first_items, second_items = rows[0].items, rows[1].items
        edited = edited_entry(rows, rows[0], "https://example.org/new", "New", "/tmp/new")
        self.assertFalse(edited.inspected)
        self.assertEqual(edited.items, [])
        self.assertEqual(edited.error, "")
        self.assertEqual(rows[0].url, "https://example.org/a")
        self.assertIs(rows[0].items, first_items)
        self.assertIs(rows[1].items, second_items)
        rows[0] = edited
        calls = []
        def inspect(url, *_):
            calls.append(url)
            return [{"url": url, "title": "New source"}]
        BatchInspection().run(rows, None, inspect=inspect)
        self.assertEqual(calls, ["https://example.org/new"])
        self.assertEqual(rows[0].items[0]["import_name"], "New")
        self.assertEqual(rows[0].items[0]["destination"], "/tmp/new")
        self.assertIs(rows[1].items, second_items)

    def test_edit_name_and_folder_reuses_metadata_without_mutating_original(self):
        rows = parse_batch("https://example.org/a\tOld\t/tmp/old")
        BatchInspection().run(rows, None, inspect=self.inspect)
        original = rows[0]
        updated = edited_entry(rows, original, original.url, "New", "~/Downloads")
        self.assertTrue(updated.inspected)
        self.assertEqual(updated.items[0]["import_name"], "New")
        self.assertEqual(updated.items[0]["destination"], str(Path.home() / "Downloads"))
        self.assertEqual(original.items[0]["import_name"], "Old")
        self.assertEqual(original.items[0]["destination"], "/tmp/old")
        cleared = edited_entry(rows, original, original.url)
        self.assertNotIn("import_name", cleared.items[0])
        self.assertNotIn("destination", cleared.items[0])
        self.assertIs(edited_entry(rows, original, original.url, "Old", "/tmp/old"), original)

    def test_edit_rejects_invalid_fields_duplicates_and_detached_entries(self):
        rows = parse_batch("https://example.org/a\nhttps://example.org/b\nhttps://example.org/a")
        for url, name, folder in (("bad", "", ""), ("https://example.org/a", "", ""),
                                  (rows[1].url, "../name", ""), (rows[1].url, "", "relative"),
                                  (rows[1].url, "name\tfield", ""), (rows[1].url, "", "/tmp\nmore")):
            with self.subTest(url=url, name=name, folder=folder), self.assertRaises(ValueError):
                edited_entry(rows, rows[1], url, name, folder)
        self.assertIs(edited_entry(rows, rows[0], rows[0].url), rows[0])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            edited_entry(rows, rows[2], rows[0].url)
        with self.assertRaisesRegex(ValueError, "no longer"):
            edited_entry(rows, replace(rows[0]), rows[0].url)
        self.assertEqual(edited_entry(rows, rows[2], rows[0].url, "Alternate").name, "Alternate")

    def test_edit_known_playlist_rejects_name_and_updates_all_destinations(self):
        rows = parse_batch("https://example.org/list")
        rows[0].inspected = True
        rows[0].items = [{"url": f"https://example.org/{i}", "title": str(i)} for i in range(2)]
        with self.assertRaisesRegex(ValueError, "one video"):
            edited_entry(rows, rows[0], rows[0].url, "Single name")
        updated = edited_entry(rows, rows[0], rows[0].url, destination="/tmp/playlist")
        self.assertTrue(updated.inspected)
        self.assertEqual([i["destination"] for i in updated.items], ["/tmp/playlist"] * 2)
        self.assertTrue(all("destination" not in i for i in rows[0].items))

    def test_edit_failed_and_invalid_entries_become_retryable_without_network(self):
        rows = parse_batch("invalid\tName\nhttps://example.org/bad")
        BatchInspection().run(rows, None, inspect=self.inspect)
        corrected = edited_entry(rows, rows[0], "https://example.org/fixed", rows[0].name)
        self.assertFalse(corrected.error or corrected.inspected or corrected.items)
        self.assertEqual(corrected.name, "Name")
        self.assertIs(edited_entry(rows, rows[1], rows[1].url), rows[1])
        corrected = edited_entry(rows, rows[1], "https://example.org/recovered")
        self.assertFalse(corrected.error or corrected.inspected or corrected.items)

    def test_replace_line_preserves_comments_invalid_rows_and_import_budget(self):
        text = "# Header\n\ninvalid\tName\t/tmp\nhttps://example.org/a\tX\t/tmp\textra\n"
        rows = parse_batch(text)
        corrected = edited_entry(rows, rows[0], "https://example.org/fixed", "Name", "/tmp")
        changed = replace_import_line(text, corrected)
        self.assertTrue(changed.startswith("# Header\n\n"))
        self.assertTrue(changed.endswith("https://example.org/a\tX\t/tmp\textra"))
        parsed = parse_batch(changed)
        self.assertEqual(parsed[0].line, 3)
        self.assertFalse(parsed[0].error)
        self.assertTrue(parsed[1].error)
        with self.assertRaisesRegex(ValueError, "too large"):
            replace_import_line("#" * MAX_BATCH_BYTES + "\nx", replace(corrected, line=2))
        with self.assertRaisesRegex(ValueError, "changed"):
            replace_import_line("x", corrected)

    @staticmethod
    def inspect(url, process, directory):
        if url.endswith("bad"):
            raise RuntimeError("Unavailable")
        return [{"url": url, "title": "Title", "media_id": "1"}]

    def test_inspection_partial_success_custom_fields_and_retry(self):
        rows = parse_batch("https://example.org/ok\tChosen\t/tmp\nhttps://example.org/bad\ninvalid")
        calls = []
        runner = BatchInspection()
        runner.run(rows, None, lambda *value: calls.append(value), inspect=self.inspect)
        self.assertTrue(runner.done.is_set())
        self.assertEqual(rows[0].items[0]["import_name"], "Chosen")
        self.assertEqual(rows[0].items[0]["destination"], "/tmp")
        self.assertEqual(rows[1].error, "Unavailable")
        self.assertEqual(calls, [(1, 3), (2, 3)])
        rows[1].error, rows[1].inspected = "", False
        inspected = []
        def fixed(url, *_):
            inspected.append(url)
            return [{"url": url, "title": "Recovered"}]
        BatchInspection().run(rows, None, inspect=fixed)
        self.assertEqual(inspected, [rows[1].url])
        self.assertEqual(rows[0].items[0]["import_name"], "Chosen")

    def test_named_playlist_and_expansion_limits(self):
        def playlist(url, *_):
            return [{"url": f"{url}/{i}", "title": str(i)} for i in range(600)]
        rows = parse_batch("https://example.org/a\tCustom\nhttps://example.org/b\nhttps://example.org/c")
        BatchInspection().run(rows, None, inspect=playlist)
        self.assertIn("custom name", rows[0].error)
        self.assertEqual(len(rows[1].items), 600)
        self.assertIn("1,000", rows[2].error)
        self.assertEqual(rows[2].items, [])

    def test_cancel_before_and_between_entries(self):
        rows = parse_batch("https://example.org/a\nhttps://example.org/b")
        runner = BatchInspection()
        runner.cancel()
        runner.run(rows, None, inspect=lambda *_: self.fail("Must not inspect"))
        self.assertFalse(any(r.inspected for r in rows))
        runner = BatchInspection()
        runner.run(rows, None, lambda *_: runner.cancel(), inspect=self.inspect)
        self.assertTrue(rows[0].inspected)
        self.assertFalse(rows[1].inspected)
        self.assertIsNone(runner.process)

    def test_cancel_running_process_and_join(self):
        runner = BatchInspection()
        rows = parse_batch("https://example.org/a\nhttps://example.org/b")
        started = threading.Event()
        def inspect(url, process, directory):
            child = process.start([sys.executable, "-c", "import time; time.sleep(30)"])
            started.set()
            child.communicate()
            if process.cancelled.is_set():
                raise InterruptedError()
            return []
        thread = threading.Thread(target=lambda: runner.run(rows, None, inspect=inspect))
        thread.start()
        self.assertTrue(started.wait(5))
        runner.cancel()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        runner.close()
        self.assertFalse(any(r.inspected for r in rows))
        self.assertIsNone(runner.process)

    def test_numbering_and_no_silent_truncation(self):
        self.assertEqual(numbered_name("Concert", 3, 12), "03 - Concert")
        self.assertEqual(numbered_name("Concert", 3, 1000), "0003 - Concert")
        with self.assertRaises(ValueError):
            numbered_name("x" * 140, 1, 10)

    def test_order_names_destinations_preferences_survive_restart_and_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "state")
            store.paused = True
            queue = Queue(store)
            jobs = [Job(f"https://example.org/{i}", str(i), str(root / str(i)),
                        filename=numbered_name(str(i), n, 3), include_id=False,
                        source_format="chosen" if i == 2 else None, preferred_video_codec="h264",
                        subtitle_mode="sidecar", subtitle_languages="en,es")
                    for n, i in enumerate((3, 2, 1), 1)]
            queue.add(jobs)
            loaded = Store(store.directory)
            self.assertEqual([j.title for j in loaded.jobs], ["3", "2", "1"])
            self.assertEqual([j.filename for j in loaded.jobs], ["01 - 3", "02 - 2", "03 - 1"])
            self.assertEqual(len({j.batch_id for j in loaded.jobs}), 1)
            Queue(loaded).retry(jobs[1].id)
            retried = loaded.jobs[1]
            self.assertEqual(retried.source_format, "chosen")
            self.assertEqual(retried.output, str(root / "2"))
            self.assertEqual(retried.preferred_video_codec, "h264")
            self.assertEqual(retried.subtitle_languages, "en,es")
            self.assertEqual(retried.output_stem, "02 - 2")

    def test_export_relative_unicode_and_completed_only_in_job_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "media"
            media.mkdir()
            first, second = media / "#Concert \u00e9.mp4", root / "Second.mp3"
            first.write_bytes(b"media")
            second.write_bytes(b"audio")
            sub = media / "captions.srt"
            sub.write_text("caption")
            job = Job("https://example.org/a", "Source", str(media), status="Completed", filename="Chosen",
                      files=[str(first), str(sub)])
            other = replace(job, files=[str(second)], filename="Second")
            target = root / "Playlist.m3u8"
            self.assertEqual(export_m3u([other, replace(job, status="Failed"), job], target), 2)
            lines = target.read_text().splitlines()
            self.assertEqual(lines, ["#EXTM3U", "#EXTINF:-1,Second", "./Second.mp3",
                                     "#EXTINF:-1,Chosen", "./media/#Concert \u00e9.mp4"])
            paths = [root / line for line in lines if not line.startswith("#")]
            self.assertTrue(all(path.is_file() for path in paths))

    def test_export_nonoverwrite_missing_empty_and_unsafe_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "video.mp4"
            media.write_bytes(b"media")
            job = Job("https://example.org/a", "A", directory, status="Completed", files=[str(media)])
            target = root / "existing.m3u8"
            target.write_text("keep")
            with self.assertRaises(FileExistsError):
                export_m3u([job], target)
            self.assertEqual(target.read_text(), "keep")
            self.assertEqual(list(root.glob(".playlist-*")), [])
            media.unlink()
            with self.assertRaises(OSError):
                export_m3u([job], root / "missing.m3u8")
            media.touch()
            with self.assertRaises(ValueError):
                export_m3u([job], root / "empty.m3u8")
            unsafe = root / "line\nbreak.mp4"
            unsafe.write_bytes(b"media")
            with self.assertRaises(ValueError):
                export_m3u([replace(job, files=[str(unsafe)])], root / "unsafe.m3u8")
            with self.assertRaises(ValueError):
                export_m3u([replace(job, status="Failed")], root / "none.m3u8")

    def test_export_without_hard_link_support(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "video.mp4"
            media.write_bytes(b"media")
            job = Job("https://example.org/a", "A", directory, status="Completed", files=[str(media)])
            target = root / "playlist.m3u8"
            with patch("pyrobolic.core.os.link", side_effect=OSError(errno.EOPNOTSUPP, "no hard links")):
                self.assertEqual(export_m3u([job], target), 1)
                before = target.read_bytes()
                with self.assertRaises(FileExistsError):
                    export_m3u([job], target)
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(list(root.glob(".playlist-*")), [])


if __name__ == "__main__":
    unittest.main()
