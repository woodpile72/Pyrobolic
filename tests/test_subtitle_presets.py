# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pyrobolic.core import Job, Queue, Settings, Store, download_arguments
from pyrobolic.preferences import preference_values, source_variant_filter
from pyrobolic.subtitle_presets import (SUBTITLE_FIELDS, load_preset, save_preset,
                                      subtitle_values, validate_presets)


class SubtitlePresetTests(unittest.TestCase):
    def test_presets_capture_only_subtitles_and_load_without_aliasing(self):
        settings = Settings(subtitle_mode="sidecar", subtitle_languages="en,es", subtitle_format="srt",
                            preferred_video_codec="h264", proxy="http://localhost:8000")
        library = save_preset({}, "Two Languages", settings)
        self.assertEqual(set(library["Two Languages"]), set(SUBTITLE_FIELDS))
        loaded = load_preset(Settings(), library, "Two Languages")
        self.assertEqual(subtitle_values(loaded), subtitle_values(settings))
        self.assertEqual(loaded.proxy, "")
        self.assertEqual(loaded.preferred_video_codec, "any")
        loaded.subtitle_languages = "de"
        self.assertEqual(library["Two Languages"]["subtitle_languages"], "en,es")

    def test_name_validation_limits_and_explicit_replacement(self):
        library = save_preset({}, " Example ", Settings(subtitle_mode="off"))
        self.assertIn("Example", library)
        with self.assertRaises(ValueError):
            save_preset(library, "example", Settings())
        replaced = save_preset(library, "example", Settings(subtitle_mode="sidecar"), overwrite=True)
        self.assertEqual(list(replaced), ["example"])
        self.assertEqual(library["Example"]["subtitle_mode"], "off")
        for name in ("", " ", "x" * 65, "line\nbreak", "hidden\u202e"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                save_preset({}, name, Settings())
        full = {str(i): subtitle_values(Settings()) for i in range(32)}
        with self.assertRaises(ValueError):
            save_preset(full, "overflow", Settings())

    def test_corrupt_libraries_trigger_recovery_not_silent_reset(self):
        valid = subtitle_values(Settings())
        for library in ([], {"bad": {}}, {"bad": dict(valid, url="https://example.org")},
                        {"bad": dict(valid, subtitle_mode="legacy")},
                        {"bad": dict(valid, subtitle_languages=".*")},
                        {"Name": valid, "name": valid}):
            with self.subTest(library=library), self.assertRaises(ValueError):
                Store.decode(json.dumps({"settings": {"subtitle_presets": library}}))
        validate_presets({})

    def test_roundtrip_retry_is_snapshot_and_save_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            store.paused = True
            queue = Queue(store)
            policy = Settings(subtitle_mode="sidecar", subtitle_languages="en,es", subtitle_missing="fail")
            settings = replace(policy, subtitle_presets=save_preset({}, "Travel", policy))
            job = Job("https://example.org/video", "Fixture", directory, **preference_values(settings))
            queue.add([job], settings)
            restored = Store(Path(directory))
            self.assertEqual(restored.settings.subtitle_presets, settings.subtitle_presets)
            restored.settings.subtitle_presets.clear()
            Queue(restored).retry(job.id)
            self.assertEqual(restored.jobs[0].subtitle_languages, "en,es")
            self.assertFalse(hasattr(restored.jobs[0], "subtitle_presets"))
            with patch.object(store, "save", side_effect=OSError("full")), self.assertRaises(OSError):
                queue.add([Job("https://example.org/next", "Next", directory)],
                          replace(settings, subtitle_presets={}))
            self.assertEqual(store.settings, settings)
            self.assertEqual(len(store.jobs), 1)

    def test_variant_exclusion_is_strict_but_never_replaces_exact_choice(self):
        job = Job("https://example.org/video", "Fixture", "/tmp", upscaled_sources="exclude", quality=720)
        args = download_arguments(job, "yt-dlp")
        selector = args[args.index("-f") + 1]
        self.assertEqual(selector.count(source_variant_filter(job)), 2)
        self.assertIn("[height<=720]", selector)
        self.assertEqual(source_variant_filter(replace(job, mode="audio")), "")
        self.assertEqual(source_variant_filter(replace(job, source_format="137-sr")), "")
        exact = download_arguments(replace(job, source_format="137-sr"), "yt-dlp")
        self.assertEqual(exact[exact.index("-f") + 1], 'best*[format_id="137-sr"]')
        with self.assertRaises(ValueError):
            Settings(upscaled_sources="prefer")
