# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Private-display quick downloads of generated media over loopback only."""
from functools import partial
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import traceback

sys.path.insert(0, os.environ.get("PYROBOLIC_TEST_ROOT", str(Path(__file__).resolve().parents[1])))
from pyrobolic.app import Application, GLib
from pyrobolic.core import Store
from test_integration import QuietHandler

temporary = tempfile.TemporaryDirectory()
root = Path(temporary.name)
source = root / "concert.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24",
                "-f", "lavfi", "-i", "sine=frequency=440", "-t", "1", "-c:v", "libx264",
                "-c:a", "aac", str(source)], check=True)
server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(root)))
threading.Thread(target=server.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{server.server_port}/concert.mp4"
store = Store(root / "state")
store.settings.output = str(root / "downloads")
store.settings.include_id = False
store.settings.mode, store.settings.audio_format = "audio", "opus"
store.settings.metadata, store.settings.notifications = False, False
app = Application(store)
state, failures = {}, []
deadline = time.monotonic() + 60


def video_hash(path):
    return subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v",
                                    "-c", "copy", "-f", "hash", "-"], text=True)


def finish():
    app.window.queue.shutdown()
    for process in list(app.window.processes):
        process.cancel()
        process.close()
    app.window.actions.shutdown(wait=True)
    app.quit()


def advance():
    try:
        assert time.monotonic() < deadline, "Quick-download media test timed out"
        if "dialog" not in state:
            state["dialog"] = app.window.new_download(url)
            state["dialog"].quick_download()
            return True
        if state["dialog"].process or state["dialog"].saving:
            return True
        assert state["dialog"].closed, state["dialog"].message.get_text()
        jobs = app.window.queue.snapshot()
        assert jobs
        job = jobs[-1]
        assert job.status not in {"Failed", "Canceled"}, job.detail
        if job.status != "Completed":
            return True
        assert len(job.files) == 1 and Path(job.files[0]).stat().st_size > 0
        streams = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams",
                                                      "-of", "json", job.files[0]]))["streams"]
        if len(jobs) == 1:
            assert [s["codec_name"] for s in streams] == ["opus"]
            assert job.source_format is None and job.source_audio is None
            state["audio_path"] = Path(job.files[0])
            state["audio_bytes"] = state["audio_path"].read_bytes()
            store.settings.mode, store.settings.container = "video", "mp4"
            state["dialog"] = app.window.new_download(url)
            state["dialog"].quick_download()
            return True
        assert len(jobs) == 2
        assert {s["codec_type"] for s in streams} == {"video", "audio"}
        assert video_hash(source) == video_hash(job.files[0])
        assert state["audio_path"].read_bytes() == state["audio_bytes"]
        saved = Store(store.directory)
        assert [j.status for j in saved.jobs] == ["Completed", "Completed"]
        assert [j.files for j in saved.jobs] == [j.files for j in jobs]
        assert saved.settings.mode == "video" and saved.settings.container == "mp4"
        finish()
        return False
    except Exception:
        failures.append(traceback.format_exc())
        finish()
        return False


GLib.timeout_add(500, advance)
app.run([])
server.shutdown()
server.server_close()
temporary.cleanup()
if failures:
    print("\n".join(failures))
    raise SystemExit(1)
print("Quick download real-media checks passed: Opus conversion, MP4 stream preservation, durable output")
