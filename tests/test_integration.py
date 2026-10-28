# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Opt-in integration tests use generated media served only over loopback."""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from pyrobolic.core import Job, Process, Queue, Store, inspect_url, staging_directory
from test_core import await_idle


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


@unittest.skipUnless(os.environ.get("PYROBOLIC_INTEGRATION") == "1", "Set PYROBOLIC_INTEGRATION=1")
class IntegrationTests(unittest.TestCase):
    def test_ten_real_workers_complete_twelve_collision_safe_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fixture.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                            "testsrc2=size=160x90:rate=12", "-t", "1", "-c:v", "libx264",
                            "-threads", "1", str(source)], check=True)
            for number in range(12):
                (root / f"fixture-{number}.mp4").hardlink_to(source)
            release = threading.Event()
            requests = set()
            request_lock = threading.Lock()
            all_ten = threading.Event()

            class GatedHandler(QuietHandler):
                def do_GET(self):
                    with request_lock:
                        requests.add(self.path)
                        if len(requests) >= 10:
                            all_ten.set()
                    if not release.wait(45):
                        self.send_error(503)
                        return
                    super().do_GET()

            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(GatedHandler, directory=directory))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            store = Store(root / "state")
            store.settings.concurrency = 10
            queue = Queue(store)
            jobs = [Job(f"http://127.0.0.1:{server.server_port}/fixture-{n}.mp4",
                        f"Fixture {n}", str(root / "downloads"), filename="Same title",
                        include_id=False, metadata=False) for n in range(12)]
            try:
                queue.add(jobs)
                self.assertTrue(all_ten.wait(35), "Ten engine workers did not reach the loopback server")
                with queue.lock:
                    self.assertEqual(len(queue.running), 10)
                    self.assertEqual([job.status for job in jobs[10:]], ["Queued"] * 2)
                release.set()
                await_idle(queue, 90)
                self.assertTrue(all(job.status == "Completed" for job in jobs),
                                [(job.status, job.detail) for job in jobs])
                self.assertEqual(len({job.files[0] for job in jobs}), 12)
                for job in jobs:
                    self.assertEqual(Path(job.files[0]).read_bytes(), source.read_bytes())
                    self.assertFalse(staging_directory(job).exists())
                self.assertTrue(all(job.status == "Completed" for job in Store(store.directory).jobs))
                self.assertEqual(sum(len(event.jobs) for event in queue.take_completions()), 12)
                self.assertEqual(queue.take_completions(), [])
            finally:
                release.set()
                try:
                    queue.shutdown()
                    await_idle(queue, 15)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)

    def test_exact_dash_streams_and_unavailable_format(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "fixture.mpd"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24",
                            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "2",
                            "-map", "0:v", "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-g", "24",
                            "-b:v:0", "180k", "-b:v:1", "90k", "-s:v:1", "160x90", "-c:a", "aac",
                            "-f", "dash", "-adaptation_sets", "id=0,streams=v id=1,streams=a", str(manifest)], check=True)
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=directory))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/fixture.mpd"
                formats = inspect_url(url, Process(), root / "state", single=True)[0]["formats"]
                low = next(f["id"] for f in formats if f.get("height") == 90)
                high = next(f["id"] for f in formats if f.get("height") == 180)
                audio = next(f["id"] for f in formats if f["kind"] == "audio")
                store = Store(root / "state")
                queue = Queue(store)
                def job(name, **kwargs):
                    return Job(url, name, str(root / "downloads"), filename=name, include_id=False, metadata=False, **kwargs)
                combined = job("Low with audio", source_format=low, source_audio=audio, container="mkv")
                silent = job("High without audio", source_format=high)
                sound = job("Audio", source_format=audio, mode="audio")
                converted = job("Converted streams", source_format=low, source_audio=audio,
                                container="webm", video_processing="convert")
                missing = job("Unavailable", source_format="nonexistent")
                queue.add([combined, silent, sound, converted, missing])
                await_idle(queue, 90)
                for downloaded in (combined, silent, sound, converted):
                    self.assertEqual(downloaded.status, "Completed", "\n".join(downloaded.log))
                self.assertEqual(missing.status, "Failed")
                self.assertFalse(missing.files)
                def streams(path):
                    return json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)]))["streams"]
                self.assertEqual([s["codec_type"] for s in streams(combined.files[0])], ["video", "audio"])
                self.assertEqual(streams(combined.files[0])[0]["height"], 90)
                self.assertEqual([s["codec_type"] for s in streams(silent.files[0])], ["video"])
                self.assertEqual([s["codec_type"] for s in streams(sound.files[0])], ["audio"])
                converted_streams = streams(converted.files[0])
                self.assertEqual(converted_streams[0]["height"], 90)
                self.assertIn(converted_streams[0]["codec_name"], {"vp8", "vp9", "av1"})
                self.assertIn(converted_streams[1]["codec_name"], {"opus", "vorbis"})
                def stream_hash(path, stream):
                    return subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path), "-map", stream,
                                                    "-c", "copy", "-f", "hash", "-"], text=True)
                self.assertEqual(stream_hash(manifest, "0:v:1"), stream_hash(combined.files[0], "0:v:0"))
                self.assertEqual(stream_hash(manifest, "0:v:0"), stream_hash(silent.files[0], "0:v:0"))
                self.assertEqual(stream_hash(manifest, "0:a:0"), stream_hash(sound.files[0], "0:a:0"))
                queue.retry(missing.id)
                await_idle(queue, 30)
                self.assertEqual(missing.status, "Failed")
                self.assertFalse(missing.files)
            finally:
                server.shutdown()
                server.server_close()

    def test_output_containers_and_audio_conversion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fixture.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24",
                            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "2",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)], check=True)
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=directory))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/fixture.mp4"
                store = Store(root / "state")
                queue = Queue(store)
                jobs = [Job(url, target, str(root / "media"), filename=target, include_id=False,
                            container=target, metadata=False) for target in ("mov", "avi", "mkv", "webm")]
                jobs += [Job(url, target, str(root / "media"), filename="convert-" + target, include_id=False,
                             container=target, video_processing="convert", metadata=False) for target in ("webm", "avi", "mp4")]
                jobs += [Job(url, target, str(root / "media"), filename=target, include_id=False,
                             mode="audio", audio_format=target, metadata=False) for target in ("wav", "vorbis")]
                queue.add(jobs)
                await_idle(queue, 120)
                for job in jobs:
                    if job.container == "webm" and job.video_processing == "remux":
                        self.assertEqual(job.status, "Failed", "\n".join(job.log))
                        self.assertFalse(job.files)
                        continue
                    self.assertEqual(job.status, "Completed", "\n".join(job.log))
                    streams = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json", job.files[0]]))["streams"]
                    if job.mode == "audio":
                        self.assertEqual([s["codec_type"] for s in streams], ["audio"])
                        self.assertEqual(Path(job.files[0]).suffix, ".ogg" if job.audio_format == "vorbis" else ".wav")
                        self.assertEqual(streams[0]["codec_name"], "vorbis" if job.audio_format == "vorbis" else "pcm_s16le")
                    else:
                        self.assertEqual(Path(job.files[0]).suffix, "." + job.container)
                        self.assertEqual(streams[0]["height"], 180)
                        self.assertEqual([s["codec_type"] for s in streams], ["video", "audio"])
                        if job.video_processing == "remux" or job.container == "mp4":
                            def stream_hash(path):
                                return subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path),
                                                                "-map", "0:v:0", "-c", "copy", "-f", "hash", "-"], text=True)
                            self.assertEqual(stream_hash(source), stream_hash(job.files[0]))
                # A failed incompatible remux must not emit a success event.
                events = queue.take_completions()
                self.assertEqual(sum(len(event.jobs) for event in events), len(jobs) - 1)
                self.assertEqual(events[0].failed, 1)
            finally:
                server.shutdown()
                server.server_close()

    def test_inspect_download_video_and_extract_original_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fixture.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24",
                            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "2",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)], check=True)
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=directory))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/fixture.mp4"
                items = inspect_url(url, Process(), root / "state")
                self.assertEqual(len(items), 1)
                store = Store(root / "state")
                queue = Queue(store)
                video = Job(url, items[0]["title"], str(root / "video"))
                audio = Job(url, "Audio test", str(root / "audio"), mode="audio",
                            filename="100% literal audio", include_id=False)
                named = Job(url, "Raw site title", str(root / "named"),
                            filename="Concert 2026", media_id="123", include_id=True,
                            source_format=items[0]["formats"][0]["id"])
                queue.add([video, audio, named])
                await_idle(queue, 60)
                for job in (video, audio, named):
                    self.assertEqual(job.status, "Completed", "\n".join(job.log))
                    self.assertTrue(Path(job.files[0]).is_file())
                def stream_hash(path, stream):
                    return subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path),
                                                    "-map", f"0:{stream}", "-c", "copy", "-f", "hash", "-"], text=True)
                self.assertEqual(stream_hash(source, "v"), stream_hash(video.files[0], "v"))
                self.assertEqual(stream_hash(source, "a"), stream_hash(audio.files[0], "a"))
                self.assertEqual(stream_hash(source, "v"), stream_hash(named.files[0], "v"))
                self.assertEqual(Path(named.files[0]).name, "Concert 2026 [123].mp4")
                self.assertEqual(Path(audio.files[0]).stem, "100% literal audio")
                self.assertFalse(staging_directory(named).exists())
                self.assertFalse(staging_directory(audio).exists())
                duplicate = Job(url, "Same video", str(root / "named"),
                                filename="Concert 2026", media_id="123")
                queue.add([duplicate])
                await_idle(queue, 60)
                self.assertEqual(duplicate.status, "Completed", "\n".join(duplicate.log))
                self.assertEqual(Path(duplicate.files[0]).name, "Concert 2026 [123] (2).mp4")
                self.assertEqual(stream_hash(source, "v"), stream_hash(duplicate.files[0], "v"))
                self.assertEqual(len(Store(root / "state").jobs), 4)
                old_files = named.files[:]
                new_id = queue.redownload(named.id)
                await_idle(queue, 60)
                again = next(job for job in store.jobs if job.id == new_id)
                self.assertEqual(again.status, "Completed", "\n".join(again.log))
                self.assertEqual(again.source_format, named.source_format)
                self.assertEqual(Path(again.files[0]).name, "Concert 2026 [123] (3).mp4")
                self.assertEqual(named.files, old_files)
                self.assertEqual(stream_hash(source, "v"), stream_hash(again.files[0], "v"))
                self.assertEqual(stream_hash(source, "v"), stream_hash(old_files[0], "v"))
                self.assertEqual(len(Store(root / "state").jobs), 5)
            finally:
                server.shutdown()
                server.server_close()
