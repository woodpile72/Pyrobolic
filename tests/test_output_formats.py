# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
import json
from pathlib import Path
import tempfile
import unittest

from pyrobolic.core import AUDIO_FORMATS, CONTAINERS, Job, Queue, Settings, Store, download_arguments
from pyrobolic.formats import catalog, make_choice, output_extension, remux_problem
from test_formats import FORMATS


class OutputFormatTests(unittest.TestCase):
    def job(self, **kwargs):
        return Job("https://example.org/video", "Fixture", "/tmp", **kwargs)

    def test_all_video_targets_default_to_copy(self):
        for container in CONTAINERS[1:]:
            with self.subTest(container=container):
                args = download_arguments(self.job(container=container), "yt-dlp")
                self.assertEqual(args[args.index("--remux-video") + 1], container)
                self.assertNotIn("--recode-video", args)

    def test_conversion_is_explicit_and_does_not_force_incompatible_merge(self):
        for container in CONTAINERS[1:]:
            with self.subTest(container=container):
                args = download_arguments(self.job(container=container, video_processing="convert"), "yt-dlp")
                self.assertEqual(args[args.index("--recode-video") + 1], container)
                self.assertNotIn("--remux-video", args)
                self.assertNotIn("--merge-output-format", args)

    def test_new_audio_targets_and_actual_extension(self):
        for target, extension in (("wav", "wav"), ("vorbis", "ogg")):
            args = download_arguments(self.job(mode="audio", audio_format=target), "yt-dlp")
            self.assertEqual(args[args.index("--audio-format") + 1], target)
            self.assertEqual(output_extension("audio", target, "auto"), extension)
        self.assertIsNone(output_extension("audio", "best", "mkv"))
        self.assertIsNone(output_extension("video", "mp3", "auto"))

    def test_audio_never_invokes_video_conversion(self):
        args = download_arguments(self.job(mode="audio", audio_format="vorbis", container="webm",
                                          video_processing="convert"), "yt-dlp")
        self.assertNotIn("--recode-video", args)
        self.assertNotIn("--remux-video", args)

    def test_webm_preflight_uses_known_selected_codecs_only(self):
        formats = catalog({"formats": FORMATS})
        av1_opus = make_choice(formats, "video", "v-high", "a-high")
        av1_aac = make_choice(formats, "video", "v-high", "a-low")
        h264_aac = make_choice(formats, "video", "muxed")
        self.assertEqual(remux_problem(formats, av1_opus, "webm"), "")
        self.assertIn("Convert", remux_problem(formats, av1_aac, "webm"))
        self.assertIn("WebM", remux_problem(formats, h264_aac, "webm"))
        self.assertEqual(remux_problem(formats, av1_aac, "mkv"), "")
        self.assertEqual(remux_problem(formats, None, "webm"), "")
        self.assertEqual(remux_problem([], h264_aac, "webm"), "")
        self.assertEqual(remux_problem(formats, make_choice(formats, "video", "v-high"), "webm"), "")

    def test_invalid_processing_and_unsupported_subtitle_embedding(self):
        for kwargs in ({"video_processing": "invalid"}, {"video_processing": "convert"},
                       {"container": "avi", "subtitles": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                download_arguments(self.job(**kwargs), "yt-dlp")

    def test_legacy_default_and_remembered_policy(self):
        _settings, _, jobs = Store.decode(json.dumps({"jobs": [{"url": "https://example.org", "title": "Old", "output": "/tmp"}]}))
        self.assertEqual(jobs[0].video_processing, "remux")
        self.assertEqual(Settings(video_processing="convert").video_processing, "remux")
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            store.paused = True
            settings = Settings(container="webm", video_processing="convert", audio_format="vorbis")
            queue = Queue(store)
            job = self.job(container="webm", video_processing="convert")
            queue.add([job], settings)
            restored = Store(Path(directory))
            self.assertEqual(restored.settings, settings)
            self.assertEqual(restored.jobs[0].video_processing, "convert")
            Queue(restored).retry(restored.jobs[0].id)
            self.assertEqual(restored.jobs[0].video_processing, "convert")

    def test_invalid_saved_processing_requires_recovery(self):
        data = {"jobs": [{"url": "https://example.org", "title": "Invalid", "output": "/tmp", "video_processing": "invalid"}]}
        with self.assertRaises(ValueError):
            Store.decode(json.dumps(data))
