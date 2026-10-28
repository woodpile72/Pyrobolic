# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from pyrobolic.core import Job, Queue, Settings, Store, download_arguments, inspect_url
from pyrobolic.preferences import preference_values, subtitle_catalog
from pyrobolic.transfer import has_inspection_options, inspection_arguments, transfer_values, validate_transfer
from pyrobolic.batch import parse_batch, replace_import_line


class TransferOptionTests(unittest.TestCase):
    def test_direct_connection_arguments_validation_and_saved_job_policy(self):
        settings = Settings(direct_connection=True)
        self.assertEqual(inspection_arguments(settings), ["--proxy", ""])
        self.assertTrue(has_inspection_options(settings))
        self.assertEqual(inspection_arguments(Settings()), [])
        for invalid in ({"direct_connection": 1}, {"direct_connection": "yes"},
                        {"direct_connection": True, "proxy": "http://localhost:8080"}):
            with self.assertRaises(ValueError):
                Settings(**invalid)
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            store.paused = True
            job = Job("https://example.org/fixture", "Fixture", directory, direct_connection=True)
            args = download_arguments(job, "yt-dlp")
            self.assertEqual(args[args.index("--proxy") + 1], "")
            Queue(store).add([job], settings)
            restored = Store(store.directory)
            self.assertTrue(restored.settings.direct_connection)
            restored.settings.direct_connection = False
            queue = Queue(restored)
            queue.retry(job.id)
            self.assertTrue(restored.jobs[0].direct_connection)
            restored.jobs[0].status = "Completed"
            queue.redownload(job.id)
            self.assertTrue(restored.jobs[-1].direct_connection)

    def test_rumble_default_identity_is_site_scoped_and_explicit_choices_win(self):
        for url in ("https://rumble.com/video", "https://www.rumble.com/video",
                    "https://RUMBLE.COM./embed/video"):
            self.assertEqual(inspection_arguments(Settings(), url), ["--impersonate", "firefox"])
            args = download_arguments(Job(url, "Fixture", "/tmp"), "yt-dlp")
            self.assertEqual(args[args.index("--impersonate") + 1], "firefox")
            self.assertEqual(inspection_arguments(Settings(browser_identity="chrome"), url),
                             ["--impersonate", "chrome"])
        for url in ("https://notrumble.com/video", "https://rumble.com.example.org/video",
                    "https://example.org/?url=https://rumble.com/video"):
            self.assertEqual(inspection_arguments(Settings(), url), [])

    def test_inspection_applies_site_policy_without_settings_and_removes_null(self):
        process = Mock()
        process.cancelled.is_set.return_value = False
        child = process.start.return_value
        child.returncode = 1
        child.communicate.return_value = ("ERROR: metadata forbidden\nnull\n", None)
        with patch("pyrobolic.core.engine_path", return_value="yt-dlp"):
            with self.assertRaisesRegex(RuntimeError, "^ERROR: metadata forbidden$"):
                inspect_url("https://rumble.com/video", process)
        args = process.start.call_args.args[0]
        self.assertEqual(args[args.index("--impersonate") + 1], "firefox")
        process.close.assert_called_once()

    def test_browser_identity_inspection_download_validation_and_job_retry(self):
        settings = Settings(browser_identity="firefox")
        self.assertTrue(has_inspection_options(settings))
        self.assertEqual(inspection_arguments(settings), ["--impersonate", "firefox"])
        self.assertFalse(has_inspection_options(Settings()))
        job = Job("https://example.org/fixture", "Fixture", "/tmp", browser_identity="firefox")
        args = download_arguments(job, "yt-dlp")
        self.assertEqual(args[args.index("--impersonate") + 1], "firefox")
        self.assertNotIn("--cookies-from-browser", args)
        self.assertNotIn("--impersonate", download_arguments(replace(job, browser_identity="default"), "yt-dlp"))
        for identity in ("unknown", "firefox --exec bad", None):
            with self.assertRaises(ValueError):
                Settings(browser_identity=identity)
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            store.paused = True
            Queue(store).add([job])
            restored = Store(store.directory)
            self.assertEqual(restored.settings.browser_identity, "default")
            Queue(restored).retry(job.id)
            self.assertEqual(restored.jobs[0].browser_identity, "firefox")

    def test_inspection_uses_proxy_without_download_only_controls(self):
        settings = Settings(proxy="http://127.0.0.1:8080", rate_limit_kib=512, fragment_concurrency=8)
        self.assertEqual(inspection_arguments(settings), ["--proxy", settings.proxy])
        self.assertEqual(inspection_arguments(replace(settings, proxy="")), [])
        self.assertEqual(set(transfer_values(settings)), {"proxy", "direct_connection", "rate_limit_kib", "fragment_concurrency", "use_part_files", "browser_identity"})
        with self.assertRaises(ValueError):
            inspection_arguments(replace(settings, proxy="http://secret:password@localhost"))

    def test_explicit_parabolic_import_roundtrip_and_errors(self):
        text = "https://example.org/one|/tmp/output|First\nhttps://example.org/two||Second"
        entries = parse_batch(text, "parabolic")
        self.assertEqual([(e.name, e.destination) for e in entries], [("First", "/tmp/output"), ("Second", "")])
        self.assertEqual(replace_import_line(text, entries[0], "parabolic"), text)
        self.assertTrue(parse_batch("https://example.org|relative|Name", "parabolic")[0].error)
        self.assertTrue(parse_batch("https://example.org|/tmp|Name|extra", "parabolic")[0].error)
        entry = replace(entries[0], name="A|B")
        with self.assertRaises(ValueError):
            replace_import_line(text, entry, "parabolic")
        # No automatic syntax guessing: pipes in ordinary URLs are not separators.
        self.assertEqual(parse_batch("https://example.org/a|b")[0].url, "https://example.org/a|b")

    def test_validation_and_argument_boundaries(self):
        job = Job("https://example.org/fixture", "Fixture", "/tmp", rate_limit_kib=512,
                  fragment_concurrency=4, proxy="socks5://127.0.0.1:9000")
        args = download_arguments(job, "yt-dlp")
        self.assertEqual(args[args.index("--limit-rate") + 1], "512K")
        self.assertEqual(args[args.index("--concurrent-fragments") + 1], "4")
        self.assertEqual(args[args.index("--proxy") + 1], job.proxy)
        for values in ({"rate_limit_kib": True}, {"rate_limit_kib": -1}, {"fragment_concurrency": 0},
                       {"fragment_concurrency": 17}, {"proxy": "http://name:password@localhost"},
                       {"proxy": "http://localhost/?token=x"}, {"proxy": "file:///tmp/proxy"},
                       {"proxy": "http://localhost:999999"}, {"proxy": "http://localhost/a"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_transfer(replace(job, **values))

    def test_persistence_and_retry_do_not_take_new_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            store.paused = True
            defaults = Settings(rate_limit_kib=1024, fragment_concurrency=3, proxy="http://127.0.0.1:8080")
            job = Job("https://example.org/fixture", "Fixture", directory, **preference_values(defaults))
            Queue(store).add([job], defaults)
            restored = Store(store.directory)
            self.assertEqual(restored.settings, defaults)
            queue = Queue(restored)
            queue.configure(replace(defaults, rate_limit_kib=0, proxy=""))
            queue.retry(job.id)
            self.assertEqual(restored.jobs[0].rate_limit_kib, 1024)
            self.assertEqual(restored.jobs[0].proxy, defaults.proxy)

    def test_mixed_subtitle_catalog_deduplicates_and_tracks_unknowns(self):
        manual = {"code": "en", "name": "English", "source": "manual"}
        generated = dict(manual, source="generated")
        items = [{"formats_loaded": True, "subtitle_tracks": [manual, generated]},
                 {"subtitles_loaded": True, "subtitle_tracks": [generated]}, {"subtitle_tracks": []}]
        tracks, known = subtitle_catalog(items, "manual-auto")
        self.assertEqual(known, 2)
        self.assertEqual(tracks["en"]["count"], 2)
        self.assertEqual(tracks["en"]["sources"], {"manual", "generated"})
        self.assertEqual(subtitle_catalog(items, "manual")[0]["en"]["count"], 1)
        self.assertEqual(subtitle_catalog(items, "auto")[0]["en"]["count"], 2)
