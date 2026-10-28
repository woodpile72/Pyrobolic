# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pyrobolic.core import Job, Queue, Settings, Store, download_arguments, progress_update
from pyrobolic.preferences import (preference_sort, preference_values, source_feedback, subtitle_arguments,
                                  subtitle_feedback, subtitle_mode, validate_preferences)


class PreferenceTests(unittest.TestCase):
    def job(self, **options):
        return Job("https://example.org/video", "Video", "/tmp", **options)

    def test_defaults_preserve_quick_path(self):
        job = self.job()
        self.assertEqual(preference_sort(job), [])
        self.assertEqual(subtitle_arguments(job), [])
        self.assertNotIn("--format-sort", download_arguments(job, "yt-dlp"))

    def test_general_preferences_sort_without_strict_filters(self):
        job = self.job(preferred_video_codec="h264", preferred_audio_codec="opus", preferred_fps=30,
                       preferred_abr=128, quality=720)
        args = download_arguments(job, "yt-dlp")
        self.assertEqual(args[args.index("--format-sort") + 1], "vcodec:h264,acodec:opus,fps:30,abr:128")
        self.assertEqual(args[args.index("-f") + 1], "bestvideo[height<=720]+bestaudio/best[height<=720]")

    def test_exact_ids_bypass_preferences_and_remain_strict(self):
        job = self.job(source_format="123", preferred_video_codec="av1", preferred_fps=30, source_quality="smallest")
        args = download_arguments(job, "yt-dlp")
        self.assertNotIn("--format-sort", args)
        self.assertEqual(args[args.index("-f") + 1], 'best*[format_id="123"]')
        self.assertEqual(source_feedback(job, {"vcodec": "h264"}), [])

    def test_audio_ignores_video_preferences(self):
        job = self.job(mode="audio", preferred_video_codec="h264", preferred_fps=30, preferred_abr=64)
        self.assertEqual(preference_sort(job), ["--format-sort", "abr:64"])
        self.assertEqual(source_feedback(job, {"abr": 64}), [])

    def test_feedback_reports_fallback_and_unknown_values(self):
        job = self.job(preferred_video_codec="h264", preferred_audio_codec="aac", preferred_fps=30, preferred_abr=128)
        self.assertEqual(source_feedback(job, {"vcodec": "avc1.640", "acodec": "mp4a.40.2", "fps": 29.97, "abr": 96}), [])
        self.assertEqual(len(source_feedback(job, {"vcodec": "vp9", "acodec": "opus", "fps": 60})), 4)

    def test_validated_values_cannot_inject_engine_grammar(self):
        for options in ({"preferred_video_codec": "h264,ext"}, {"preferred_fps": True},
                        {"subtitle_languages": "en.*"}, {"subtitle_languages": "all,-live_chat"},
                        {"subtitle_mode": "invalid"}, {"subtitle_missing": "ignore"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                validate_preferences(self.job(**options))

    def test_subtitle_sources_formats_modes_and_missing_policy(self):
        job = self.job(subtitle_mode="both", subtitle_languages="en, es", subtitle_source="manual-auto", subtitle_format="srt")
        args = subtitle_arguments(job)
        self.assertIn("--write-subs", args)
        self.assertIn("--write-auto-subs", args)
        self.assertIn("--embed-subs", args)
        self.assertEqual(args[args.index("--sub-langs") + 1], "(?i:en(?:-.*)?),(?i:es(?:-.*)?)")
        self.assertEqual(subtitle_feedback(job, ["en-US", "es"]), "")
        self.assertIn("es", subtitle_feedback(job, ["en"]))
        self.assertNotIn("--embed-subs", subtitle_arguments(replace(job, subtitle_mode="sidecar")))
        for options in ({"container": "avi"}, {"subtitle_format": "lrc"}, {"mode": "audio"},
                        {"container": "webm", "subtitle_format": "ass"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                download_arguments(replace(job, **options), "yt-dlp")
        download_arguments(replace(job, mode="audio", subtitle_mode="sidecar"), "yt-dlp")

    def test_legacy_subtitles_keep_previous_intent(self):
        self.assertEqual(subtitle_mode(self.job(subtitles=True)), "both")
        self.assertEqual(subtitle_mode(self.job(subtitles=True, mode="audio")), "off")
        self.assertEqual(subtitle_mode(self.job(subtitles=True, subtitle_mode="off")), "off")

    def test_state_roundtrip_retry_and_failed_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            store.paused = True
            queue = Queue(store)
            settings = Settings(preferred_video_codec="h264", preferred_fps=30, subtitle_mode="sidecar",
                                subtitle_languages="en,es", subtitle_format="srt", subtitle_missing="fail")
            job = self.job(source_format="42", **preference_values(settings))
            queue.add([job], settings)
            restored = Store(store.directory)
            self.assertEqual(restored.settings, settings)
            self.assertEqual(preference_values(restored.jobs[0]), preference_values(settings))
            restored.jobs[0].warnings = ["Previous attempt"]
            restored.jobs[0].selected_source = "Previous source"
            Queue(restored).retry(job.id)
            self.assertEqual(restored.jobs[0].source_format, "42")
            self.assertEqual(restored.jobs[0].warnings, [])
            self.assertEqual(restored.jobs[0].selected_source, "")
            with patch.object(store, "save", side_effect=OSError("disk full")), self.assertRaises(OSError):
                queue.add([self.job()], replace(settings, preferred_video_codec="av1"))
            self.assertEqual(store.settings, settings)
            self.assertEqual(len(store.jobs), 1)

    def test_reports_do_not_store_subtitle_urls(self):
        report = progress_update('PYRO_SUBTITLES:{"en":{"url":"https://secret.example/token"}}')
        self.assertEqual(report, {"subtitle_languages": ["en"]})
        self.assertEqual(progress_update('PYRO_SUBFILES:["/tmp/media.en.srt"]'), {"subtitle_files": ["/tmp/media.en.srt"]})

    def test_invalid_saved_policy_triggers_recovery(self):
        with self.assertRaises(ValueError):
            Store.decode(json.dumps({"settings": {"subtitle_languages": "["}}))
        with self.assertRaises(ValueError):
            Store.decode(json.dumps({"jobs": [{"url": "https://example.org", "title": "x", "output": "/tmp", "preferred_abr": -1}]}))

    def test_automatic_only_keeps_subtitles_until_verification(self):
        for mode in ("sidecar", "embed", "both"):
            args = subtitle_arguments(self.job(subtitle_mode=mode, subtitle_source="auto"))
            self.assertIn("--write-auto-subs", args)
            self.assertIn("--no-write-subs", args)
            self.assertNotIn("--write-subs", args)
            self.assertNotIn("--embed-subs", args)
            if mode != "sidecar":
                self.assertEqual(args[args.index("--use-postprocessor") + 1],
                                 "FFmpegEmbedSubtitle:already_have_subtitle=True;when=after_move")

    def test_all_languages_is_explicit_and_excludes_live_chat(self):
        job = self.job(subtitle_mode="sidecar", subtitle_language_scope="all", subtitle_languages="")
        validate_preferences(job)
        args = subtitle_arguments(job)
        self.assertEqual(args[args.index("--sub-langs") + 1], "all,-live_chat")
        self.assertEqual(subtitle_feedback(job, ["en", "es"]), "")
        self.assertIn("No subtitles", subtitle_feedback(job, []))
        with self.assertRaises(ValueError):
            validate_preferences(replace(job, subtitle_language_scope="selected"))
        with self.assertRaises(ValueError):
            validate_preferences(replace(job, subtitle_language_scope="invalid"))

    def test_all_and_auto_defaults_persist_without_exact_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            store.paused = True
            settings = Settings(subtitle_mode="both", subtitle_source="auto", subtitle_language_scope="all",
                                subtitle_languages="en,es", subtitle_missing="fail")
            job = self.job(**preference_values(settings))
            Queue(store).add([job], settings)
            restored = Store(store.directory)
            self.assertEqual(restored.settings, settings)
            self.assertEqual(preference_values(restored.jobs[0]), preference_values(settings))
            Queue(restored).retry(job.id)
            self.assertEqual(restored.jobs[0].subtitle_language_scope, "all")
            self.assertEqual(restored.jobs[0].subtitle_source, "auto")
            self.assertIsNone(restored.jobs[0].source_format)
