# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Install official yt-dlp releases without overwriting running executables."""

import hashlib
import json
import os
from pathlib import Path
import platform
import tempfile
import threading
from urllib.request import Request, urlopen

from .core import Process, data_directory


API = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
MAX_BINARY = 128 * 1024 * 1024


class UpdateControl:
    def __init__(self):
        self.canceled = threading.Event()
        self.done = threading.Event()
        self.process = Process()
        self.commit_lock = threading.Lock()
        self.committed = False

    def cancel(self):
        with self.commit_lock:
            if not self.committed:
                self.canceled.set()
                self.process.cancel()

    def check(self):
        if self.canceled.is_set():
            raise InterruptedError("Engine update canceled; the installed engine was kept.")

    def close(self):
        self.done.wait()


def fetch(url):
    if not url.startswith("https://"):
        raise ValueError("Updates require HTTPS")
    return urlopen(Request(url, headers={"User-Agent": "Pyrobolic/0.1"}), timeout=30)


def install_verified(stream, target: Path, expected: str, progress=lambda fraction: None,
                     total=0, validate=True, control=None):
    control = control or UpdateControl()
    control.check()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(prefix=".yt-dlp-", dir=target.parent)
    temporary = Path(filename)
    try:
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "wb") as output:
            while chunk := stream.read(256 * 1024):
                control.check()
                size += len(chunk)
                if size > MAX_BINARY:
                    raise RuntimeError("Update exceeds the download size limit")
                digest.update(chunk)
                output.write(chunk)
                progress(min(size / total, 1) if total else 0)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest().lower() != expected.lower():
            raise RuntimeError("yt-dlp checksum mismatch. The existing version was kept.")
        temporary.chmod(0o755)
        if validate:
            control.process.communicate([str(temporary), "--version"], timeout=20)
        with control.commit_lock:
            control.check()
            os.replace(temporary, target)
            control.committed = True
    finally:
        temporary.unlink(missing_ok=True)


def update_engine(directory=None, progress=lambda fraction: None, control=None):
    control = control or UpdateControl()
    try:
        return _update_engine(directory, progress, control)
    finally:
        control.done.set()


def _update_engine(directory, progress, control):
    control.check()
    machine = platform.machine()
    assets = {"x86_64": "yt-dlp_linux", "aarch64": "yt-dlp_linux_aarch64"}
    if machine not in assets:
        raise RuntimeError(f"Automatic updates are not available for {machine}. Install yt-dlp with your package manager.")
    with fetch(API) as response:
        release = json.load(response)
    control.check()
    urls = {a["name"]: a for a in release["assets"]}
    name = assets[machine]
    with fetch(urls["SHA2-256SUMS"]["browser_download_url"]) as response:
        checksums = response.read(1024 * 1024).decode("utf-8")
    expected = next((line.split()[0] for line in checksums.splitlines()
                     if len(line.split()) == 2 and line.split()[1].lstrip("*") == name), None)
    if expected is None:
        raise RuntimeError("The release does not contain a checksum for yt-dlp")
    target = (directory or data_directory()) / "bin/yt-dlp"
    asset = urls[name]
    control.check()
    with fetch(asset["browser_download_url"]) as response:
        install_verified(response, target, expected, progress, asset.get("size", 0), control=control)
    return release["tag_name"]
