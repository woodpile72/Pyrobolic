# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Private, transactional yt-dlp/EJS and Deno installations."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
import threading
from urllib.parse import urlparse
import zipfile

from .core import Process, data_directory
from .updater import UpdateControl, fetch, _update_engine

API = "https://api.github.com/repos/denoland/deno/releases/latest"
_lock = threading.Lock()


def youtube_url(url):
    host = (urlparse(url).hostname or "").lower()
    return any(host == domain or host.endswith("." + domain)
               for domain in ("youtube.com", "youtu.be", "youtube-nocookie.com"))


def installed_directory(directory=None):
    current = (directory or data_directory()) / "support/current"
    if all((current / name).is_file() and os.access(current / name, os.X_OK)
           for name in ("yt-dlp", "deno")):
        # Resolve once so an in-flight command keeps a matching engine/runtime pair.
        return current.resolve()
    return None


def _validate(args, control, *, empty_batch=False):
    process = Process()
    control.process = process
    control.check()
    try:
        return process.communicate(args, timeout=30)
    except subprocess.CalledProcessError as error:
        if empty_batch and error.returncode == 2 and "You must provide at least one URL" in error.output:
            return error.output
        raise


def install_deno(target, control, progress=lambda value: None):
    machine = platform.machine()
    if platform.system() != "Linux" or machine not in {"x86_64", "aarch64"}:
        raise RuntimeError("Managed download support requires Linux amd64 or arm64.")
    with fetch(API) as response:
        release = json.loads(response.read(2 * 1024 * 1024))
    name = f"deno-{machine}-unknown-linux-gnu.zip"
    asset = next(asset for asset in release["assets"] if asset["name"] == name)
    expected = asset.get("digest", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected):
        raise RuntimeError("Official Deno release has no SHA-256 digest; existing support was kept.")
    url = asset["browser_download_url"]
    if not url.startswith("https://github.com/denoland/deno/releases/download/"):
        raise RuntimeError("Unexpected Deno distribution source")
    with tempfile.TemporaryFile() as archive:
        digest, size = hashlib.sha256(), 0
        with fetch(url) as response:
            while chunk := response.read(256 * 1024):
                control.check()
                size += len(chunk)
                if size > 128 * 1024 * 1024:
                    raise RuntimeError("Deno archive exceeds the size limit")
                archive.write(chunk)
                digest.update(chunk)
                progress(min(size / max(asset.get("size", 0), 1), 1))
        if digest.hexdigest() != expected.removeprefix("sha256:"):
            raise RuntimeError("Deno checksum mismatch; existing support was kept.")
        archive.seek(0)
        with zipfile.ZipFile(archive) as zipped:
            if zipped.namelist() != ["deno"] or zipped.getinfo("deno").file_size > 256 * 1024 * 1024:
                raise RuntimeError("Unexpected Deno archive contents")
            with zipped.open("deno") as source, target.open("xb") as output:
                while chunk := source.read(256 * 1024):
                    control.check()
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
    target.chmod(0o755)
    version = _validate([str(target), "--version"], control).splitlines()[0]
    match = re.match(r"deno (\d+)\.(\d+)\.(\d+)", version)
    if not match or tuple(map(int, match.groups())) < (2, 3, 0):
        raise RuntimeError("The downloaded Deno runtime is not supported")
    if version.split()[1] != release["tag_name"].removeprefix("v"):
        raise RuntimeError("Deno executable version does not match the release")
    return version


def update_support(directory=None, progress=lambda value: None, control=None, *, only_missing=False):
    directory = directory or data_directory()
    control = control or UpdateControl()
    locked = False
    stage = None
    link = None
    try:
        while not locked:
            control.check()
            locked = _lock.acquire(timeout=0.1)
        if only_missing and installed_directory(directory):
            return "Download support already installed"
        root = directory / "support"
        root.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix="release-", dir=root))
        deno = install_deno(stage / "deno", control, lambda value: progress(value * .5))
        control.check()
        # _update_engine commits only into this unpublished staging directory.
        engine_control = UpdateControl()
        # Share cancellation events while retaining the outer transaction's commit lock.
        engine_control.canceled = control.canceled
        control.process = engine_control.process
        engine = _update_engine(stage, lambda value: progress(.5 + value * .5), engine_control)
        (stage / "bin/yt-dlp").rename(stage / "yt-dlp")
        (stage / "bin").rmdir()
        diagnostic = _validate([str(stage / "yt-dlp"), "--ignore-config", "--verbose",
                                "--js-runtimes", f"deno:{stage / 'deno'}",
                                "--simulate", "--batch-file", "/dev/null"], control, empty_batch=True)
        if "yt_dlp_ejs" not in diagnostic:
            raise RuntimeError("Downloaded engine is missing its YouTube JavaScript components")
        (stage / "versions.json").write_text(json.dumps({"deno": deno, "yt-dlp": engine}) + "\n")
        link = root / (stage.name + ".link")
        link.symlink_to(stage.name, target_is_directory=True)
        with control.commit_lock:
            control.check()
            os.replace(link, root / "current")
            control.committed = True
        # Keep older generations: running jobs may still use their absolute paths.
        stage = None
        return f"Download support installed: yt-dlp {engine}, {deno}"
    finally:
        if link:
            link.unlink(missing_ok=True)
        if stage:
            shutil.rmtree(stage)
        if locked:
            _lock.release()
        control.done.set()


def ensure_support(url, process, directory=None):
    if not youtube_url(url) or installed_directory(directory):
        return
    control = UpdateControl()
    process.attach_cancellable(control)
    try:
        update_support(directory, control=control, only_missing=True)
    finally:
        process.detach_cancellable(control)
