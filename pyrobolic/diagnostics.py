# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Read-only local dependency probes. Never inspect jobs, URLs, cookies or profiles."""
from dataclasses import dataclass
import shutil
import subprocess

from .core import engine_path

TOOLS = ("yt-dlp", "ffmpeg", "ffprobe", "deno")
GUIDANCE = {
    "yt-dlp": "Install or update the download engine in Preferences.",
    "ffmpeg": "Install the ffmpeg package using your system package manager.",
    "ffprobe": "Install the ffmpeg package using your system package manager.",
    "deno": "YouTube support is installed privately when first needed. Update Download Support in Advanced Settings to install or repair it.",
}


@dataclass(frozen=True)
class Dependency:
    name: str
    status: str
    path: str = ""
    version: str = ""
    detail: str = ""


def check_dependencies(directory, processes, timeout=5):
    results = []
    for name, process in zip(TOOLS, processes, strict=True):
        if process.cancelled.is_set():
            raise InterruptedError("Dependency check canceled")
        try:
            path = engine_path(directory) if name == "yt-dlp" else shutil.which(name)
            if name == "deno":
                from .runtime import installed_directory
                support = installed_directory(directory)
                if support:
                    path = str(support / "deno")
        except RuntimeError:
            path = None
        if not path:
            results.append(Dependency(name, "Not Found", detail=GUIDANCE[name]))
            continue
        args = (["--ignore-config", "--version"] if name == "yt-dlp" else
                ["-version"] if name in {"ffmpeg", "ffprobe"} else ["--version"])
        try:
            output = process.communicate([path, *args], timeout=timeout)
            version = next((line.strip() for line in output.splitlines() if line.strip()), "")
            version = "".join(c for c in version if c.isprintable())[:240]
            if not version:
                results.append(Dependency(name, "No Version Reported", path, detail=GUIDANCE[name]))
            else:
                results.append(Dependency(name, "Available", path, version))
        except subprocess.TimeoutExpired:
            results.append(Dependency(name, "Timed Out", path, detail=GUIDANCE[name]))
        except (OSError, subprocess.CalledProcessError):
            # Do not display arbitrary program output as installation instructions.
            results.append(Dependency(name, "Could Not Run", path, detail=GUIDANCE[name]))
    return results
