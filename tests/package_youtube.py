# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Opt-in public YouTube test against an installed package in a disposable root."""
from pathlib import Path
import shutil
import tempfile
import time
from unittest.mock import patch

from pyrobolic.core import Job, Process, Queue, Store, engine_path, inspect_url
from pyrobolic.runtime import ensure_support, installed_directory

with tempfile.TemporaryDirectory(prefix="pyro-package-youtube-") as temporary:
    root = Path(temporary)
    assert shutil.which("deno") is None, "Run in a clean disposable root, not the personal desktop"
    store = Store(root / "state")
    assert installed_directory(store.directory) is None
    url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    entries = inspect_url(url, Process(), store.directory, single=True)
    assert len(entries) == 1
    support = installed_directory(store.directory)
    assert support and engine_path(store.directory) == str(support / "yt-dlp")
    with patch("pyrobolic.runtime.update_support", side_effect=AssertionError("Unexpected reinstall")):
        ensure_support(url, Process(), store.directory)
    queue = Queue(store)
    job = Job(url, "Public YouTube package test", str(root / "media"), filename="public-test",
              quality=480, include_id=False, metadata=False)
    queue.add([job])
    deadline = time.monotonic() + 180
    while job.status not in {"Completed", "Failed"} and time.monotonic() < deadline:
        time.sleep(.1)
    queue.shutdown()
    assert job.status == "Completed", job.detail
    assert all(Path(name).is_file() and Path(name).stat().st_size for name in job.files)
    print("Installed-package clean Deno/EJS installation, subsequent reuse and public YouTube download passed")
