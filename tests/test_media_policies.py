# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Real engines with generated streams and controlled extraction metadata."""

from functools import partial
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from pyrobolic.core import Job, Queue, Store, download_arguments
from test_core import await_idle
from test_integration import QuietHandler


def probe(path):
    return json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)]))["streams"]


@unittest.skipUnless(os.environ.get("PYROBOLIC_INTEGRATION") == "1", "Set PYROBOLIC_INTEGRATION=1")
class MediaPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name, size, fps, vcodec, acodec in (("low.mp4", "160x90", "24", "libx264", "aac"),
                                                ("high.webm", "320x180", "60", "libvpx-vp9", "libopus")):
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}",
                            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "1.2",
                            "-c:v", vcodec, "-threads", "1", "-c:a", acodec, "-b:a",
                            "64k" if name == "low.mp4" else "96k", str(self.root / name)], check=True)
        for code, text in (("en", "Manual English"), ("es", "Subtítulos españoles"), ("de", "Automatisch"),
                           ("ar", "مرحبا")):
            (self.root / f"{code}.srt").write_text(f"1\n00:00:00,000 --> 00:00:01,000\n{text}\n", encoding="utf-8")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=self.temporary.name))
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.info = {"id": "fixture", "title": "Policy fixture", "webpage_url": self.base + "/low.mp4",
                     "extractor": "fixture", "extractor_key": "Fixture",
                     "formats": [
                         {"format_id": "low", "url": self.base + "/low.mp4", "ext": "mp4", "vcodec": "avc1.640",
                          "acodec": "mp4a.40.2", "height": 90, "width": 160, "fps": 24, "abr": 64},
                         {"format_id": "high", "url": self.base + "/high.webm", "ext": "webm", "vcodec": "vp9",
                          "acodec": "opus", "height": 180, "width": 320, "fps": 60, "abr": 96}],
                     "subtitles": {code: [{"url": self.base + f"/{code}.srt", "ext": "srt", "name": code}]
                                   for code in ("en", "es", "ar")},
                     "automatic_captions": {code: [{"url": self.base + "/de.srt", "ext": "srt", "name": code}]
                                            for code in ("en", "de")}}
        self.info_path = self.root / "fixture.json"
        self.store = Store(self.root / "state")
        self.queue = Queue(self.store)

    def job(self, name, **kwargs):
        return Job(self.base + "/low.mp4", name, str(self.root / "media"), filename=name,
                   include_id=False, metadata=False, **kwargs)

    def run_jobs(self, jobs):
        self.info_path.write_text(json.dumps(self.info), encoding="utf-8")
        def arguments(job, executable):
            return download_arguments(job, executable)[:-2] + ["--load-info-json", str(self.info_path)]
        with patch("pyrobolic.core.download_arguments", side_effect=arguments):
            self.queue.add(jobs)
            await_idle(self.queue, 120)

    def test_source_preferences_and_exact_selection(self):
        jobs = [self.job("Best"), self.job("Codec", preferred_video_codec="h264"),
                self.job("Frame rate", preferred_fps=30), self.job("Audio rate", preferred_abr=64),
                self.job("Smaller", source_quality="smallest"), self.job("Fallback", preferred_video_codec="av1"),
                self.job("Exact", source_format="high", preferred_video_codec="h264", preferred_fps=30),
                self.job("Missing exact", source_format="missing", preferred_video_codec="h264")]
        self.run_jobs(jobs)
        for job, codec, height in zip(jobs[:-1], ["vp9", "h264", "h264", "h264", "h264", "vp9", "vp9"],
                                       [180, 90, 90, 90, 90, 180, 180]):
            self.assertEqual(job.status, "Completed", "\n".join(job.log))
            streams = probe(job.files[0])
            self.assertEqual(streams[0]["codec_name"], codec)
            self.assertEqual(streams[0]["height"], height)
            self.assertIn("format_id:", job.selected_source)
            if job.title == "Fallback":
                self.assertIn("AV1", job.warnings[0])
            else:
                self.assertEqual(job.warnings, [])
            original = self.root / ("low.mp4" if codec == "h264" else "high.webm")
            def stream_hash(path):
                return subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0",
                                                "-c", "copy", "-f", "hash", "-"], text=True)
            self.assertEqual(stream_hash(original), stream_hash(job.files[0]))
        self.assertEqual(jobs[-1].status, "Failed")
        self.assertEqual(jobs[-1].files, [])
        restored = Store(self.store.directory)
        self.assertEqual(restored.jobs[5].warnings, jobs[5].warnings)
        self.assertEqual(restored.jobs[6].source_format, "high")
        def arguments(job, executable):
            return download_arguments(job, executable)[:-2] + ["--load-info-json", str(self.info_path)]
        with patch("pyrobolic.core.download_arguments", side_effect=arguments):
            self.queue.retry(jobs[-1].id)
            await_idle(self.queue, 30)
        self.assertEqual(jobs[-1].status, "Failed")

    def test_variant_filter_and_loaded_subtitle_preset(self):
        from pyrobolic.core import Settings
        from pyrobolic.preferences import preference_values
        from pyrobolic.subtitle_presets import save_preset, load_preset
        self.info["formats"][1]["format_note"] = "180p, AI-upscaled"
        library = save_preset({}, "Bilingual", Settings(subtitle_mode="sidecar",
                              subtitle_languages="en,es", subtitle_format="srt", subtitle_missing="fail"))
        policy = load_preset(Settings(upscaled_sources="exclude"), library, "Bilingual")
        excluded = self.job("Filtered with preset", **preference_values(policy))
        exact = self.job("Exact upscale", source_format="high", upscaled_sources="exclude")
        self.run_jobs([excluded, exact])
        for job, height in ((excluded, 90), (exact, 180)):
            self.assertEqual(job.status, "Completed", "\n".join(job.log))
            self.assertEqual(probe(job.files[0])[0]["height"], height)
        self.assertIn("Manual English", next(Path(p).read_text() for p in excluded.files if p.endswith(".en.srt")))
        self.assertIn("españoles", next(Path(p).read_text() for p in excluded.files if p.endswith(".es.srt")))
        self.info["formats"] = [self.info["formats"][1]]
        unavailable = self.job("Only upscale", upscaled_sources="exclude")
        self.run_jobs([unavailable])
        self.assertEqual(unavailable.status, "Failed")
        self.assertEqual(unavailable.files, [])

    def test_subtitle_output_content_embedding_and_failure(self):
        jobs = [self.job("Sidecars", subtitle_mode="sidecar", subtitle_languages="EN,es,ar", subtitle_format="srt"),
                self.job("Both", container="mkv", subtitle_mode="both", subtitle_languages="en,es", subtitle_format="ass"),
                self.job("Embedded", source_format="low", container="mp4", subtitle_mode="embed", subtitle_format="srt"),
                self.job("Generated", subtitle_mode="sidecar", subtitle_languages="en,de", subtitle_source="manual-auto", subtitle_format="vtt"),
                self.job("Lyrics", mode="audio", subtitle_mode="sidecar", subtitle_format="lrc"),
                self.job("Missing required", subtitle_mode="sidecar", subtitle_languages="ja", subtitle_missing="fail"),
                self.job("Missing optional", subtitle_mode="sidecar", subtitle_languages="ja"),
                self.job("WebM subtitles", container="webm", subtitle_mode="both")]
        self.run_jobs(jobs)
        for job in jobs:
            if job.title == "Missing required":
                self.assertEqual(job.status, "Failed", "\n".join(job.log))
                self.assertFalse(any(p.is_file() for p in Path(job.output).glob("Missing required*")))
                continue
            self.assertEqual(job.status, "Completed", "\n".join(job.log))
            self.assertTrue(all(Path(name).stat().st_size for name in job.files))
            self.assertFalse(any("PYRO_SUBTITLES" in line for line in job.log))
        self.assertEqual(len(jobs[0].files), 4)
        self.assertIn("españoles", next(Path(p).read_text() for p in jobs[0].files if p.endswith(".es.srt")))
        self.assertIn("مرحبا", next(Path(p).read_text() for p in jobs[0].files if p.endswith(".ar.srt")))
        self.assertEqual(len([s for s in probe(jobs[1].files[0]) if s["codec_type"] == "subtitle"]), 2)
        self.assertEqual({s["tags"]["language"] for s in probe(jobs[1].files[0]) if s["codec_type"] == "subtitle"}, {"eng", "spa"})
        self.assertTrue(any(p.endswith(".es.ass") for p in jobs[1].files))
        self.assertEqual(len(jobs[2].files), 1)
        self.assertEqual([s["codec_name"] for s in probe(jobs[2].files[0]) if s["codec_type"] == "subtitle"], ["mov_text"])
        self.assertIn("Manual English", next(Path(p).read_text() for p in jobs[3].files if p.endswith(".en.vtt")))
        self.assertIn("00:00.000 --> 00:01.000", next(Path(p).read_text() for p in jobs[3].files if p.endswith(".en.vtt")))
        self.assertIn("Automatisch", next(Path(p).read_text() for p in jobs[3].files if p.endswith(".de.vtt")))
        self.assertTrue(any(p.endswith(".en.lrc") for p in jobs[4].files))
        self.assertIn("ja", jobs[6].warnings[0])
        self.assertEqual([s["codec_name"] for s in probe(jobs[7].files[0]) if s["codec_type"] == "subtitle"], ["webvtt"])
        self.assertEqual(sum(len(e.jobs) for e in self.queue.take_completions()), 7)
        # A network failure is a failed download, not an optional-language fallback.
        self.info["subtitles"]["en"][0]["url"] = self.base + "/nonexistent.srt"
        failed = self.job("Broken subtitles", subtitle_mode="sidecar", subtitle_missing="warn")
        self.run_jobs([failed])
        self.assertEqual(failed.status, "Failed")
        self.assertEqual(self.queue.take_completions(), [])

    def test_cancellation_during_subtitle_verification(self):
        from pyrobolic.core import Process
        entered = threading.Event()
        original = Process.communicate
        def communicate(process, args, timeout=20):
            if args[0] == "ffprobe":
                entered.set()
                if not process.cancelled.wait(15):
                    raise RuntimeError("Cancellation did not reach verification")
                raise InterruptedError("Canceled")
            return original(process, args, timeout)
        self.info_path.write_text(json.dumps(self.info), encoding="utf-8")
        def arguments(job, executable):
            return download_arguments(job, executable)[:-2] + ["--load-info-json", str(self.info_path)]
        job = self.job("Cancel verification", container="mkv", subtitle_mode="embed")
        with patch("pyrobolic.core.download_arguments", side_effect=arguments), patch.object(Process, "communicate", communicate):
            self.queue.add([job])
            self.assertTrue(entered.wait(30))
            self.queue.cancel(job.id)
            await_idle(self.queue, 20)
        self.assertEqual(job.status, "Canceled")
        self.assertEqual(self.queue.take_completions(), [])
        self.assertFalse(any(Path(job.output).glob("Cancel verification*")))

    def test_unsupported_automatic_embedding_and_conversion_failure(self):
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(self.root / "low.mp4"), "-c", "copy",
                        str(self.root / "fixture.avi")], check=True)
        self.info["formats"] = [dict(self.info["formats"][0], url=self.base + "/fixture.avi", ext="avi")]
        incompatible = self.job("Unsupported embedding", subtitle_mode="embed")
        self.run_jobs([incompatible])
        self.assertEqual(incompatible.status, "Failed", "\n".join(incompatible.log))
        self.assertIn("not embedded", incompatible.detail)
        (self.root / "bad.srt").write_text("This is not a timed subtitle", encoding="utf-8")
        self.info["subtitles"]["en"][0]["url"] = self.base + "/bad.srt"
        invalid = self.job("Subtitle conversion failure", subtitle_mode="sidecar", subtitle_format="ass")
        self.run_jobs([invalid])
        self.assertEqual(invalid.status, "Failed", "\n".join(invalid.log))
        self.assertEqual(self.queue.take_completions(), [])

    def test_automatic_only_all_languages_and_verified_embedding(self):
        self.info["subtitles"]["live_chat"] = [{"url": self.base + "/must-not-fetch.json", "ext": "json"}]
        self.info["automatic_captions"]["live_chat"] = self.info["subtitles"]["live_chat"]
        jobs = [self.job("All manual", subtitle_mode="sidecar", subtitle_language_scope="all"),
                self.job("All combined", subtitle_mode="sidecar", subtitle_language_scope="all", subtitle_source="manual-auto"),
                self.job("All generated", container="mkv", subtitle_mode="both", subtitle_language_scope="all",
                         subtitle_source="auto", subtitle_format="srt"),
                self.job("Generated embedded", container="mp4", source_format="low", subtitle_mode="embed",
                         subtitle_source="auto", subtitle_languages="en", subtitle_format="srt"),
                self.job("Generated converted", source_format="low", container="webm", video_processing="convert",
                         subtitle_mode="both", subtitle_source="auto", subtitle_languages="de"),
                self.job("No generated Spanish", subtitle_mode="sidecar", subtitle_source="auto",
                         subtitle_languages="es", subtitle_missing="fail")]
        self.run_jobs(jobs)
        for job in jobs[:-1]:
            self.assertEqual(job.status, "Completed", "\n".join(job.log))
            self.assertFalse(any("live_chat" in path for path in job.files))
        self.assertEqual(len(jobs[0].files), 4)
        self.assertEqual(len(jobs[1].files), 5)
        self.assertIn("Manual English", next(Path(p).read_text() for p in jobs[1].files if p.endswith(".en.srt")))
        self.assertEqual(len(jobs[2].files), 3)
        self.assertIn("Automatisch", next(Path(p).read_text() for p in jobs[2].files if p.endswith(".en.srt")))
        self.assertEqual({s["tags"]["language"] for s in probe(jobs[2].files[0]) if s["codec_type"] == "subtitle"}, {"eng", "deu"})
        self.assertEqual(len(jobs[3].files), 1)
        self.assertEqual([s["codec_name"] for s in probe(jobs[3].files[0]) if s["codec_type"] == "subtitle"], ["mov_text"])
        self.assertEqual([s["codec_name"] for s in probe(jobs[4].files[0]) if s["codec_type"] == "subtitle"], ["webvtt"])
        self.assertTrue(any(p.endswith(".de.vtt") for p in jobs[4].files))
        self.assertEqual(jobs[-1].status, "Failed")
        self.assertIn("es", jobs[-1].detail)
        self.assertFalse(any(Path(jobs[-1].output).glob("No generated Spanish*")))
        self.assertEqual(sum(len(event.jobs) for event in self.queue.take_completions()), 5)

    def test_all_languages_empty_source_and_failed_generated_fetch(self):
        self.info["automatic_captions"] = {}
        warning = self.job("No generated tracks", subtitle_mode="sidecar", subtitle_source="auto",
                           subtitle_language_scope="all")
        strict = self.job("Require generated tracks", subtitle_mode="sidecar", subtitle_source="auto",
                          subtitle_language_scope="all", subtitle_missing="fail")
        self.run_jobs([warning, strict])
        self.assertEqual(warning.status, "Completed", "\n".join(warning.log))
        self.assertIn("No subtitles", warning.warnings[0])
        self.assertEqual(strict.status, "Failed")
        self.assertEqual(sum(len(event.jobs) for event in self.queue.take_completions()), 1)
        self.info["automatic_captions"] = {"en": [{"url": self.base + "/missing.srt", "ext": "srt"}]}
        broken = self.job("Broken generated", subtitle_mode="both", subtitle_source="auto", container="mkv",
                          subtitle_language_scope="all")
        self.run_jobs([broken])
        self.assertEqual(broken.status, "Failed")
        self.assertEqual(self.queue.take_completions(), [])
