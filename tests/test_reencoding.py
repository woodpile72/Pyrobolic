# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Private-stage re-encoding failure boundaries, using controlled processing results."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from pyrobolic.core import Job, progress_update
from pyrobolic.encoding import finish_encoding


class ReencodingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.stage = Path(temporary.name)
        self.media = self.stage / "media.mp4"
        self.media.write_bytes(b"original")
        self.options = Job("https://example.org/fixture", "Fixture", str(self.stage),
                           container="mp4", video_processing="convert", reencode_video=True)
        self.calls = []
        self.failure = None
        self.codec = "h264"
        self.duration = "2"

    def run_phase(self, args, timeout=30):
        self.calls.append(args)
        if args[0] == "ffprobe":
            return json.dumps({"streams": [{"codec_type": "video", "codec_name": self.codec},
                                           {"codec_type": "audio", "codec_name": "aac"}],
                               "format": {"duration": self.duration}})
        self.assertEqual(Path(args[args.index("-i") + 1]).read_bytes(), b"original")
        if self.failure:
            raise self.failure
        Path(args[-1]).write_bytes(b"encoded")
        return ""

    def finish(self, source="mp4"):
        finish_encoding(self.options, self.stage, source, [str(self.media)], self.run_phase, self.preserve)

    def preserve(self, source, target):
        target.hardlink_to(source)

    def test_retry_uses_original_not_previously_encoded_output(self):
        self.finish()
        self.assertEqual(self.media.read_bytes(), b"encoded")
        self.finish()
        self.assertEqual(self.media.read_bytes(), b"encoded")
        self.assertEqual((self.stage / "media.encoding-source.mp4").read_bytes(), b"original")
        commands = [args for args in self.calls if args[0] == "ffmpeg"]
        self.assertEqual(len(commands), 2)
        self.assertIn("-c:0", commands[0])
        self.assertNotIn("-c:1", commands[0])

    def test_failure_and_cancellation_do_not_replace_input(self):
        for failure in (RuntimeError("Missing encoder"), InterruptedError("Canceled")):
            self.failure = failure
            with self.assertRaises(type(failure)):
                self.finish()
            self.assertEqual(self.media.read_bytes(), b"original")
        self.failure = None
        self.finish()
        self.assertEqual(self.media.read_bytes(), b"encoded")

    def test_wrong_codec_does_not_replace_input(self):
        self.codec = "hevc"
        with self.assertRaisesRegex(RuntimeError, "requested video codec"):
            self.finish()
        self.assertEqual(self.media.read_bytes(), b"original")

    def test_already_converted_container_is_verified_without_second_encode(self):
        self.finish(source="webm")
        self.assertTrue(all(args[0] == "ffprobe" for args in self.calls))
        self.assertEqual(self.media.read_bytes(), b"original")

    def test_invalid_duration_does_not_replace_input(self):
        for duration in ("0", "nan", "-1"):
            self.duration = duration
            with self.assertRaisesRegex(RuntimeError, "playable duration"):
                self.finish()
            self.assertEqual(self.media.read_bytes(), b"original")

    def test_missing_report_and_unowned_paths_fail_closed(self):
        with self.assertRaisesRegex(RuntimeError, "input container"):
            self.finish(source=None)
        with self.assertRaisesRegex(RuntimeError, "outside staging"):
            finish_encoding(self.options, self.stage, "mp4", [str(self.stage.parent / "media.mp4")], self.run_phase, self.preserve)
        temporary = self.stage / "media.encoding-output.mp4"
        temporary.symlink_to(self.media)
        with self.assertRaisesRegex(RuntimeError, "symbolic link"):
            self.finish()
        self.assertEqual(self.media.read_bytes(), b"original")

    def test_disabled_policy_does_not_probe_or_change_files(self):
        for values in ({"reencode_video": False}, {"mode": "audio"}, {"video_processing": "remux"}):
            finish_encoding(replace(self.options, **values), None, None, [], self.run_phase, self.preserve)
        self.assertEqual(self.calls, [])

    def test_input_container_reports_are_structured(self):
        self.assertEqual(progress_update('PYRO_INPUT_EXT:"mp4"'), {"input_ext": "mp4"})
        self.assertEqual(progress_update('PYRO_INPUT_EXT:null'), {"input_ext": None})
