# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from pyrobolic.core import Job, Process, Queue, Store, download_arguments, inspect_url
from pyrobolic.formats import catalog, exact_selector, format_label, make_choice, source_choices


FORMATS = [
    {"format_id": "a-low", "vcodec": "none", "acodec": "aac", "abr": 96, "ext": "m4a", "language": "en"},
    {"format_id": "a-high", "vcodec": "none", "acodec": "opus", "abr": 160, "ext": "webm", "language": "fr"},
    {"format_id": "muxed", "vcodec": "avc1", "acodec": "aac", "width": 1280, "height": 720, "ext": "mp4"},
    {"format_id": "v-high", "vcodec": "av01", "acodec": "none", "width": 1080, "height": 1920,
     "fps": 60, "ext": "mp4", "dynamic_range": "HDR10", "format_note": "Enhanced", "filesize_approx": 1048576},
]


class FormatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.formats = catalog({"formats": FORMATS})

    def job(self, **kwargs):
        return Job("https://example.org/media", "Fixture", str(self.root), **kwargs)

    def test_catalog_preserves_order_and_stream_types(self):
        self.assertEqual([f["id"] for f in self.formats], [f["format_id"] for f in FORMATS])
        self.assertEqual([f["kind"] for f in self.formats], ["audio", "audio", "combined", "video"])
        self.assertEqual([f["id"] for f in source_choices(self.formats, "audio")], ["muxed", "a-high", "a-low"])

    def test_catalog_excludes_nonmedia_drm_and_untrusted_fields(self):
        entries = FORMATS + [{"format_id": "storyboard", "vcodec": "none", "acodec": "none"},
                            {"format_id": "drm", "has_drm": True}, {"format_id": "bad]/best"}, None]
        entries[0] = dict(entries[0], url="secret", http_headers={"Cookie": "secret"}, fragments=["secret"])
        result = catalog({"formats": entries})
        self.assertEqual(len(result), 4)
        self.assertNotIn("secret", json.dumps(result))

    def test_unknown_and_missing_metadata(self):
        self.assertEqual(catalog({}), [])
        self.assertEqual(catalog({"format_id": "mp4"})[0]["kind"], "unknown")
        self.assertEqual(catalog({"formats": [{"format_id": "x", "fps": float("nan"), "height": -1}]})[0],
                         {"id": "x", "kind": "unknown"})

    def test_labels_keep_portrait_dimensions_and_variant_information(self):
        text = format_label(self.formats[-1])
        for expected in ("1080x1920", "60 fps", "av01", "HDR10", "Enhanced", "~1.0 MiB", "ID v-high"):
            self.assertIn(expected, text)
        self.assertIn("fr", format_label(self.formats[1]))

    def test_exact_ids_are_not_engine_selector_keywords(self):
        self.assertEqual(exact_selector("best", "mp4"), 'best*[format_id="best"]+best*[format_id="mp4"]')
        self.assertEqual(exact_selector("0"), 'best*[format_id="0"]')
        for value in ("x/best", "x+y", "a,b", "x]", "a b", "'bad'", "", 1, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                exact_selector(value)
        with self.assertRaises(ValueError):
            exact_selector(None, "audio")

    def test_choice_validates_pair_and_does_not_silently_substitute(self):
        choice = make_choice(self.formats, "video", "v-high", "a-low")
        self.assertEqual(choice["audio"], "a-low")
        for primary, audio in (("gone", None), ("muxed", "a-low"), ("v-high", "muxed"), ("v-high", "v-high")):
            with self.subTest(primary=primary), self.assertRaises(ValueError):
                make_choice(self.formats, "video", primary, audio)
        self.assertIn("No audio", make_choice(self.formats, "video", "v-high")["summary"])
        self.assertIsNone(make_choice(self.formats, "video"))

    def test_explicit_source_ignores_resolution_ceiling_without_transcode(self):
        args = download_arguments(self.job(source_format="v-high", source_audio="a-low", quality=480), "yt-dlp")
        self.assertEqual(args[args.index("-f") + 1], 'best*[format_id="v-high"]+best*[format_id="a-low"]')
        self.assertNotIn("--recode-video", args)
        self.assertNotIn("height<=", " ".join(args))

    def test_audio_source_keeps_explicit_conversion_separate(self):
        args = download_arguments(self.job(mode="audio", source_format="muxed", audio_format="opus"), "yt-dlp")
        self.assertEqual(args[args.index("-f") + 1], 'best*[format_id="muxed"]')
        self.assertEqual(args[args.index("--audio-format") + 1], "opus")
        with self.assertRaises(ValueError):
            download_arguments(self.job(mode="audio", source_format="v", source_audio="a"), "yt-dlp")

    def test_choice_survives_queue_save_restart_and_retry(self):
        store = Store(self.root)
        store.paused = True
        queue = Queue(store)
        job = self.job(source_format="v-high", source_audio="a-low", source_summary="Selected streams")
        queue.add([job])
        reopened = Store(self.root)
        restored = reopened.jobs[0]
        self.assertEqual((restored.source_format, restored.source_audio, restored.source_summary),
                         ("v-high", "a-low", "Selected streams"))
        Queue(reopened).retry(restored.id)
        self.assertEqual(restored.source_format, "v-high")
        self.assertEqual(restored.status, "Queued")

    def test_invalid_saved_selection_requires_recovery(self):
        store = Store(self.root)
        store.jobs = [self.job(source_audio="orphan")]
        store.save()
        with self.assertRaises(ValueError):
            Store(self.root)
        queue = Queue(Store(self.root / "other"))
        with self.assertRaises(ValueError):
            queue.add([self.job(source_format="best/evil")])
        self.assertEqual(queue.store.jobs, [])

    def test_inspection_retains_catalog_and_flat_playlist_can_be_lazy(self):
        data = {"id": "movie", "title": "Movie", "webpage_url": "https://example.org/movie", "formats": FORMATS}
        script = "print(" + repr(json.dumps(data)) + ")"
        with patch("pyrobolic.core.engine_path", return_value="unused"), \
                patch("pyrobolic.core.common_arguments", return_value=[sys.executable, "-c", script]):
            result = inspect_url(data["webpage_url"], Process(), self.root, single=True)
        self.assertTrue(result[0]["formats_loaded"])
        self.assertEqual(result[0]["formats"], self.formats)

    def test_legacy_job_has_automatic_source(self):
        _settings, _paused, jobs = Store.decode(json.dumps({"jobs": [{"url": "https://example.org", "title": "Old", "output": "/tmp"}]}))
        self.assertIsNone(jobs[0].source_format)
        self.assertIsNone(jobs[0].source_audio)
