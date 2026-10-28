# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Download queue and persistence, independent of GTK."""

from __future__ import annotations

import copy
import errno
import hashlib
from dataclasses import asdict, dataclass, field, replace
import json
import math
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit
import uuid

from .naming import available_stem, output_stem, validate_name, validate_stem
from .formats import catalog, exact_selector
from .transfer import transfer_arguments
from .linux_files import rename_no_replace
from .ranges import validate_range, range_arguments
from .preferences import (MediaPreferences, validate_preferences, preference_sort, source_feedback, source_variant_filter,
                          subtitle_mode, subtitle_arguments, subtitle_feedback)


TERMINAL = {"Completed", "Failed", "Canceled", "Interrupted"}
ACTIVE = {"Queued", "Downloading", "Processing", "Canceling"}
QUALITIES = [0, 2160, 1440, 1080, 720, 480]
AUDIO_FORMATS = ["best", "mp3", "m4a", "opus", "flac", "wav", "vorbis"]
CONTAINERS = ["auto", "mp4", "mkv", "webm", "mov", "avi"]
VIDEO_PROCESSING = ["remux", "convert"]
STARTUP_FEEDBACK_INTERVAL = 5


def startup_detail(selected: bool, elapsed: float) -> str:
    phase = "Starting transfer" if selected else "Inspecting source"
    seconds = max(0, int(elapsed))
    detail = f"{phase}  \u00b7  {seconds // 60}:{seconds % 60:02d} elapsed"
    if seconds >= 60:
        detail += "  \u00b7  No transfer progress reported yet"
    return detail


def data_directory() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "pyrobolic"


def validate_url(value: str) -> str:
    value = value.strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("Enter an HTTP or HTTPS URL.")
    if parsed.username or parsed.password or any(c.isspace() for c in value):
        raise ValueError("The URL must not contain spaces or login credentials.")
    return value


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@dataclass
class Settings(MediaPreferences):
    auth_profiles: dict = field(default_factory=dict)
    subtitle_presets: dict = field(default_factory=dict)
    appearance: str = "system"
    history_sort: str = "newest"
    history_retention_days: int = 0
    history_retention_seconds: int | None = None
    settings_expanded: bool = False
    download_expanded: bool = False
    background_mode: bool = False
    tray_appearance: str = "symbolic"
    remember_window: bool = True
    window_width: int = 880
    window_height: int = 650
    window_maximized: bool = False
    output: str = field(default_factory=lambda: str(Path.home() / "Downloads"))
    concurrency: int = 2
    metadata: bool = True
    subtitles: bool = False
    mode: str = "video"
    quality: int = 0
    audio_format: str = "best"
    container: str = "auto"
    include_id: bool = True
    notifications: bool = True
    open_folder_on_completion: bool = False
    video_processing: str = "remux"
    prevent_suspend: bool = False

    def __post_init__(self):
        if type(self.background_mode) is not bool or self.tray_appearance not in {"symbolic", "color"}:
            raise ValueError("Invalid background preference")
        from .history import validate_retention, validate_clear_interval
        validate_retention(self.history_retention_days)
        validate_clear_interval(self.history_retention_seconds)
        if any(type(value) is not bool for value in (self.settings_expanded, self.download_expanded)):
            raise ValueError("Invalid disclosure state")
        from .auth import validate_profiles
        validate_profiles(self.auth_profiles)
        from .subtitle_presets import validate_presets
        validate_presets(self.subtitle_presets)
        validate_preferences(self)
        if self.appearance not in {"system", "light", "dark"}:
            raise ValueError("Invalid appearance preference")
        if self.history_sort not in {"newest", "oldest", "title"}:
            raise ValueError("Invalid History sort order")
        if type(self.remember_window) is not bool or type(self.window_maximized) is not bool:
            raise ValueError("Invalid window state")
        for size in (self.window_width, self.window_height):
            if type(size) is not int or not 360 <= size <= 16384:
                raise ValueError("Invalid saved window size")
        self.concurrency = max(1, min(10, self.concurrency))
        if self.mode not in ("video", "audio"):
            self.mode = "video"
        if self.quality not in QUALITIES:
            self.quality = 0
        if self.audio_format not in AUDIO_FORMATS:
            self.audio_format = "best"
        if self.container not in CONTAINERS:
            self.container = "auto"
        if self.video_processing not in VIDEO_PROCESSING or self.container == "auto":
            self.video_processing = "remux"


@dataclass
class Job(MediaPreferences):
    url: str
    title: str
    output: str
    mode: str = "video"
    quality: int = 0
    audio_format: str = "best"
    container: str = "auto"
    metadata: bool = True
    subtitles: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = "Queued"
    progress: float = 0
    detail: str = "Waiting"
    files: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    filename: str | None = None
    media_id: str = ""
    include_id: bool = True
    output_stem: str | None = None
    batch_id: str = ""
    completed_at: float | None = None
    total_bytes: int | None = None
    source_format: str | None = None
    source_audio: str | None = None
    source_summary: str = ""
    video_processing: str = "remux"
    warnings: list[str] = field(default_factory=list)
    selected_source: str = ""
    transfer_paused: bool = False
    clip_start: float = 0
    clip_end: float | None = None
    precise_cuts: bool = False
    auth_ref: str = ""


@dataclass
class Completion:
    jobs: list[Job]
    failed: int = 0
    canceled: int = 0


class Store:
    def __init__(self, directory: Path | None = None):
        self.directory = directory or data_directory()
        self.path = self.directory / "state.json"
        self.settings = Settings()
        self.paused = False
        self.jobs: list[Job] = []
        if self.path.exists():
            self.settings, self.paused, self.jobs = self.decode(self.path.read_text(encoding="utf-8"))

    @staticmethod
    def decode(text):
        data = json.loads(text)
        if not isinstance(data, dict) or not isinstance(data.get("settings", {}), dict):
            raise ValueError("Saved settings must be an object")
        settings = Settings(**data.get("settings", {}))
        if (not isinstance(settings.output, str) or not settings.output or
                type(settings.concurrency) is not int or
                any(type(getattr(settings, key)) is not bool for key in
                    ("metadata", "subtitles", "include_id", "notifications", "open_folder_on_completion", "prevent_suspend"))):
            raise ValueError("Saved settings contain invalid values")
        paused = data.get("paused", False)
        if type(paused) is not bool or not isinstance(data.get("jobs", []), list):
            raise ValueError("Saved queue has an invalid structure")
        jobs, identifiers = [], set()
        for item in data.get("jobs", []):
            job = Job(**item)
            from .auth import validate_ref, public_url
            validate_ref(job.auth_ref)
            if job.auth_ref and (job.url != public_url(job.url) or job.metadata or job.filename is None):
                raise ValueError("Authenticated jobs require redacted source addresses and named staging without source metadata.")
            for key in ("id", "title", "url", "output", "detail", "batch_id"):
                if not isinstance(getattr(job, key), str):
                    raise ValueError(f"Saved download has an invalid {key}")
            if not job.id or job.id in identifiers or not job.output:
                raise ValueError("Saved downloads have empty or duplicate identifiers or paths")
            validate_url(job.url)
            validate_source_choice(job)
            validate_output_options(job)
            if type(job.transfer_paused) is not bool:
                raise ValueError("Invalid transfer pause state")
            if job.filename is not None:
                validate_name(job.filename)
            if job.output_stem is not None:
                validate_stem(job.output_stem)
            if (job.filename is None) != (job.output_stem is None):
                raise ValueError("Saved filename and output name must be present together")
            if not isinstance(job.media_id, str) or type(job.include_id) is not bool:
                raise ValueError("Invalid saved filename options")
            if (job.status not in ACTIVE | TERMINAL or job.mode not in {"audio", "video"} or
                    job.quality not in QUALITIES or job.container not in CONTAINERS or
                    job.audio_format not in AUDIO_FORMATS or
                    any(type(getattr(job, key)) is not bool for key in ("metadata", "subtitles"))):
                raise ValueError("Saved download contains invalid options")
            for value in (job.progress, job.created):
                if type(value) not in (int, float) or not math.isfinite(value):
                    raise ValueError("Saved download contains an invalid time or progress")
            if not 0 <= job.progress <= 1:
                raise ValueError("Saved download progress is out of range")
            if job.completed_at is not None and (type(job.completed_at) not in (int, float) or
                                                 not math.isfinite(job.completed_at) or not 0 <= job.completed_at <= 253402300799):
                raise ValueError("Invalid completion time")
            if job.total_bytes is not None and (type(job.total_bytes) is not int or job.total_bytes < 0):
                raise ValueError("Invalid output size")
            if not isinstance(job.selected_source, str) or len(job.selected_source) > 3000:
                raise ValueError("Invalid saved source description")
            for value in (job.files, job.log, job.warnings):
                if not isinstance(value, list) or any(not isinstance(line, str) for line in value):
                    raise ValueError("Saved download contains invalid file paths or logs")
            identifiers.add(job.id)
            if job.status in ACTIVE:
                job.status, job.detail = "Interrupted", "Application closed before completion"
            job.transfer_paused = False
            jobs.append(job)
        return settings, paused, jobs

    @classmethod
    def recover(cls, directory, source=None):
        """Validate a replacement, then preserve the original bytes before any replacement."""
        path = Path(directory) / "state.json"
        if source is None:
            settings, paused, jobs = Settings(), False, []
        else:
            settings, paused, jobs = cls.decode(Path(source).read_text(encoding="utf-8"))
        backup = None
        try:
            original = path.read_bytes()
        except FileNotFoundError:
            pass
        else:
            fd, name = tempfile.mkstemp(prefix="state-damaged-", suffix=".json", dir=directory)
            backup = Path(name)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(original)
                    stream.flush()
                    os.fsync(stream.fileno())
            except Exception:
                backup.unlink(missing_ok=True)
                raise
        atomic_json(path, {"settings": asdict(settings), "paused": paused,
                           "jobs": [asdict(job) for job in jobs]})
        return cls(directory), backup

    def save(self):
        atomic_json(self.path, {"settings": asdict(self.settings),
                                "paused": self.paused,
                                "jobs": [asdict(j) for j in self.jobs]})


def engine_path(directory: Path | None = None) -> str:
    from .runtime import installed_directory
    support = installed_directory(directory)
    if support:
        return str(support / "yt-dlp")
    managed = (directory or data_directory()) / "bin/yt-dlp"
    if managed.is_file() and os.access(managed, os.X_OK):
        return str(managed)
    found = shutil.which("yt-dlp")
    if not found:
        raise RuntimeError("yt-dlp is not installed. Install it from Preferences.")
    return found


def common_arguments(executable: str) -> list[str]:
    args = [executable, "--ignore-config", "--no-color",
            "--socket-timeout", "20", "--retries", "3"]
    private_deno = Path(executable).parent / "deno"
    deno = str(private_deno) if private_deno.is_file() else shutil.which("deno")
    if deno:
        args += ["--js-runtimes", f"deno:{deno}"]
    return args


def validate_source_choice(job):
    exact_selector(job.source_format, job.source_audio)
    if job.mode == "audio" and job.source_audio is not None:
        raise ValueError("Audio extraction cannot combine source streams")
    if not isinstance(job.source_summary, str) or len(job.source_summary) > 3000:
        raise ValueError("Invalid source format description")


def validate_output_options(job):
    validate_preferences(job)
    from .encoding import validate_encoding
    validate_encoding(job)
    from .artwork import validate_artwork
    validate_artwork(job)
    from .metadata import validate_chapter_output
    validate_chapter_output(job)
    if isinstance(job, Job):
        validate_range(job.clip_start, job.clip_end, job.precise_cuts)
    if job.video_processing not in VIDEO_PROCESSING:
        raise ValueError("Invalid video processing option")
    if job.mode == "video":
        if job.container == "auto" and job.video_processing != "remux":
            raise ValueError("Choose a target container before allowing video conversion")
    submode = subtitle_mode(job)
    if submode in {"embed", "both"}:
        if job.mode == "audio":
            raise ValueError("Choose subtitle sidecar files for audio downloads.")
        if job.container == "avi":
            raise ValueError("AVI cannot embed subtitles. Choose sidecar files or another container.")
        if job.subtitle_format == "lrc":
            raise ValueError("LRC subtitles require sidecar output.")
        if job.container == "webm" and job.subtitle_format not in {"best", "vtt"}:
            raise ValueError("WebM embedding requires WebVTT subtitles. Choose VTT or Original.")


def download_arguments(job: Job, executable: str) -> list[str]:
    validate_url(job.url)
    validate_source_choice(job)
    validate_output_options(job)
    if not job.use_part_files and job.output_stem is None:
        raise ValueError("Disabling .part files requires a new named download with private staging.")
    selected = exact_selector(job.source_format, job.source_audio)
    if job.mode not in {"video", "audio"} or job.audio_format not in AUDIO_FORMATS:
        raise ValueError("Invalid download format")
    if job.quality not in QUALITIES:
        raise ValueError("Invalid video quality")
    if job.container not in CONTAINERS:
        raise ValueError("Invalid video container")
    suffix = f" (audio-{job.audio_format})" if job.mode == "audio" else (
        f" ({job.quality}p max)" if job.quality else "")
    destination = staging_directory(job) if job.output_stem is not None else Path(job.output).expanduser().resolve()
    template = "media.%(ext)s" if job.output_stem is not None else f"%(title).180B [%(id)s]{suffix}.%(ext)s"
    args = common_arguments(executable) + [
        "--no-playlist", "--newline", "--no-simulate", "--no-overwrites",
        "--progress", "--progress-delta", "0.3",
        "--progress-template", 'download:PYRO_PROGRESS:%(progress.{status,filename,downloaded_bytes,total_bytes,total_bytes_estimate,speed,eta})j',
        "--progress-template", 'postprocess:PYRO_PROCESS:processing',
        "--print", "after_move:PYRO_FILE:%(filepath)j",
        "--print", "before_dl:PYRO_SELECTION:%(.{format_id,format_note,vcodec,acodec,fps,abr,height})j",
        "--paths", str(destination),
        "--output", template,
    ]
    if job.mode == "audio":
        args += ["-f", selected or "bestaudio/best", "--extract-audio", "--audio-format", job.audio_format]
        if job.audio_format != "best":
            args += ["--audio-quality", job.audio_encoding_quality if job.audio_format in {"mp3", "m4a", "opus", "vorbis"} else "0"]
    else:
        limit = (f"[height<={job.quality}]" if job.quality else "") + source_variant_filter(job)
        args += ["-f", selected or f"bestvideo{limit}+bestaudio/best{limit}"]
        if job.container != "auto":
            if job.video_processing == "convert":
                # Merge in an engine-selected container before any requested conversion.
                args += ["--recode-video", job.container]
            else:
                args += ["--merge-output-format", job.container, "--remux-video", job.container]
    args += preference_sort(job)
    from .encoding import encoding_arguments
    args += encoding_arguments(job)
    args += transfer_arguments(job)
    args += range_arguments(job)
    from .metadata import metadata_arguments
    args += metadata_arguments(job)
    from .artwork import artwork_arguments
    args += artwork_arguments(job)
    args += subtitle_arguments(job)
    from .sponsorblock import arguments as sponsor_arguments
    args += sponsor_arguments(job)
    if subtitle_mode(job) in {"embed", "both"} and job.container == "webm" and job.subtitle_format == "best":
        args += ["--convert-subs", "vtt"]
    return args + ["--", job.url]


class Process:
    """Cancellation owns only this subprocess group, including its FFmpeg child."""

    def __init__(self):
        self.cancelled = threading.Event()
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.process = None
        self.done = threading.Event()
        self.grace = 3
        self.resumed = threading.Event()
        self.resumed.set()
        self.cancellables = []

    def attach_cancellable(self, cancellable):
        with self.lock:
            self.cancellables.append(cancellable)
            if self.cancelled.is_set():
                cancellable.cancel()

    def detach_cancellable(self, cancellable):
        with self.lock:
            self.cancellables.remove(cancellable)

    def start(self, args, *, pass_fds=()):
        with self.lock:
            if self.cancelled.is_set():
                raise InterruptedError("Canceled")
            self.process = subprocess.Popen(args, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, text=True,
                                            encoding="utf-8", errors="replace",
                                            start_new_session=True, pass_fds=pass_fds)
            threading.Thread(target=self._supervise, daemon=True).start()
            return self.process

    def _signal_group(self, sig):
        try:
            os.killpg(self.process.pid, sig)
            return True
        except ProcessLookupError:
            return False

    def _supervise(self):
        try:
            while self.process.poll() is None and not self.stopping.wait(0.05):
                pass
            # The parent may exit first while a child still holds the output pipe open.
            self._signal_group(signal.SIGCONT)
            if self._signal_group(signal.SIGTERM):
                deadline = time.monotonic() + self.grace
                while time.monotonic() < deadline:
                    self.process.poll()
                    if not self._signal_group(0):
                        break
                    time.sleep(0.05)
                else:
                    self._signal_group(signal.SIGKILL)
            self.process.wait()
        finally:
            self.resumed.set()
            self.done.set()

    def set_paused(self, paused):
        with self.lock:
            if self.cancelled.is_set() or self.process is None or self.process.poll() is not None or self.done.is_set():
                raise RuntimeError("This transfer is no longer running.")
            if not self._signal_group(signal.SIGSTOP if paused else signal.SIGCONT):
                raise RuntimeError("This transfer is no longer running.")
            if paused:
                self.resumed.clear()
            else:
                self.resumed.set()

    def wait_resumed(self):
        self.resumed.wait()
        if self.cancelled.is_set():
            raise InterruptedError("Canceled")

    def cancel(self):
        with self.lock:
            self.cancelled.set()
            self.stopping.set()
            self.resumed.set()
            if self.process is not None and not self.done.is_set():
                self._signal_group(signal.SIGCONT)
                self._signal_group(signal.SIGTERM)
            cancellables = self.cancellables[:]
        for cancellable in cancellables:
            cancellable.cancel()

    def close(self):
        if self.process is not None:
            if not self.done.is_set():
                self.resumed.set()
                self.stopping.set()
            self.done.wait()
            if self.process.stdout:
                self.process.stdout.close()

    def communicate(self, args, timeout=20):
        child = self.start(args)
        try:
            output, _ = child.communicate(timeout=timeout)
            if self.cancelled.is_set():
                raise InterruptedError("Canceled")
            if child.returncode:
                raise subprocess.CalledProcessError(child.returncode, args, output)
            return output
        finally:
            self.close()


def inspect_url(url: str, process: Process, directory: Path | None = None, *, single=False, playlist_limit=None,
                authentication=None, transfer=None) -> list[dict]:
    url = validate_url(url)
    from .runtime import ensure_support
    ensure_support(url, process, directory)
    args = common_arguments(engine_path(directory))
    from .transfer import TransferPreferences, inspection_arguments
    args += inspection_arguments(transfer if transfer is not None else TransferPreferences(), url)
    if playlist_limit is not None:
        if type(playlist_limit) is not int or playlist_limit < 1:
            raise ValueError("Invalid playlist inspection limit")
        args += ["--playlist-end", str(playlist_limit)]
    args += [
        "--no-playlist" if single else "--flat-playlist", "--dump-single-json", "--skip-download", "--", url]
    private = None
    try:
        if authentication is not None and authentication.active:
            from .auth import EngineAuthentication
            private = EngineAuthentication(url, authentication)
            child = process.start(private.arguments(args), pass_fds=private.fds)
        else:
            child = process.start(args)
        output, _ = child.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        process.cancel()
        raise RuntimeError("Inspecting this URL timed out. Try again.") from None
    finally:
        process.close()
        if private:
            private.close()
    if process.cancelled.is_set():
        raise InterruptedError("Canceled")
    if child.returncode:
        if private:
            from .auth import safe_auth_error
            raise RuntimeError(safe_auth_error(RuntimeError(output)))
        message = "\n".join(line for line in output.splitlines() if line.strip() != "null")
        raise RuntimeError(message[-4000:].strip() or "Unable to inspect this URL")
    data = None
    for line in output.splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                data = value
        except json.JSONDecodeError:
            continue
    if data is None:
        raise RuntimeError("yt-dlp did not return media information")
    items = data.get("entries") if data.get("_type") in {"playlist", "multi_video"} else [data]
    result = []
    for item in items or []:
        if not item:
            continue
        target = item.get("webpage_url") or item.get("url") or url
        try:
            target = validate_url(target)
        except ValueError:
            continue
        result.append({"url": target, "title": item.get("title") or item.get("id") or target,
                       "from_collection": data.get("_type") in {"playlist", "multi_video"},
                       "media_id": str(item.get("id") or ""),
                       "duration": item.get("duration"),
                       "thumbnail": item.get("thumbnail") if not private and isinstance(item.get("thumbnail"), str) else None,
                       "source": item.get("extractor_key") or data.get("extractor_key", ""),
                       "formats": catalog(item),
                       "subtitles_loaded": data.get("_type") not in {"playlist", "multi_video"} or "subtitles" in item,
                       "subtitle_tracks": [{"code": str(code)[:80], "source": kind,
                                            "name": str((tracks[0] if tracks else {}).get("name") or code)[:120]}
                                           for key, kind in (("subtitles", "manual"), ("automatic_captions", "generated"))
                                           for code, tracks in (item.get(key) or {}).items()
                                           if isinstance(tracks, list) and (not tracks or isinstance(tracks[0], dict))],
                       "formats_loaded": isinstance(item.get("formats"), list) or "format_id" in item})
    if not result:
        raise RuntimeError("No downloadable items were found")
    if private:
        # URLs stay only in the review draft until submission moves them into the keyring.
        for item in result:
            for key in ("title", "media_id", "source"):
                item[key] = authentication.scrub(item[key], url)
            for entry in item["formats"] + item["subtitle_tracks"]:
                for key, value in entry.items():
                    if isinstance(value, str):
                        entry[key] = authentication.scrub(value, url)
    return result


class ProgressDisplay:
    """Keep intermittent engine estimates from collapsing the progress label."""
    def __init__(self):
        self.reset()

    def reset(self):
        self.estimates = {}
        self.filename = None
        self.downloaded = 0
        self.updated = None
        self.text = ""

    def detail(self, data, fraction, total):
        now = time.monotonic()
        filename = data.get("filename")
        downloaded = data.get("downloaded_bytes") or 0
        if (filename != self.filename or downloaded < self.downloaded):
            self.reset()
        self.filename, self.downloaded = filename, downloaded
        for key in ("speed", "eta"):
            value = data.get(key)
            if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                self.estimates[key] = (value, now)
        if self.updated is not None and now - self.updated < 1:
            return self.text
        values = {key: value for key, (value, seen) in self.estimates.items() if now - seen <= 5}
        speed = f"{values['speed'] / 1024 / 1024:.1f}" if "speed" in values else "--"
        eta = f"{int(values['eta']) // 60}:{int(values['eta']) % 60:02d}" if "eta" in values else "--:--"
        percent = f"{fraction:.0%}" if total else "Downloading"
        self.text = f"{percent}  ·  {speed} MiB/s  ·  {eta} remaining"
        self.updated = now
        return self.text


def progress_update(line: str, display: ProgressDisplay | None = None) -> dict:
    if line.startswith("PYRO_DESCRIPTION:"):
        value = line.removeprefix("PYRO_DESCRIPTION:")
        if value not in {"0", "1"}:
            raise ValueError("Invalid description availability")
        return {"description_available": value == "1"}
    if line.startswith("PYRO_SELECTION:"):
        value = json.loads(line[len("PYRO_SELECTION:"):])
        if not isinstance(value, dict):
            raise ValueError("Invalid source selection report")
        return {"selection": value}
    if line.startswith("PYRO_SUBTITLES:"):
        value = json.loads(line[len("PYRO_SUBTITLES:"):])
        return {"subtitle_languages": list(value) if isinstance(value, dict) else []}
    if line.startswith("PYRO_SUBFILES:"):
        value = json.loads(line[len("PYRO_SUBFILES:"):])
        return {"subtitle_files": value if isinstance(value, list) else []}
    if line.startswith("PYRO_CHAPTERS:"):
        value = json.loads(line[len("PYRO_CHAPTERS:"):])
        return {"source_chapters": value if isinstance(value, list) else []}
    if line.startswith("PYRO_CHAPTERFILES:"):
        value = json.loads(line[len("PYRO_CHAPTERFILES:"):])
        return {"chapter_files": value if isinstance(value, list) else []}
    if line.startswith("PYRO_ART_AVAILABLE:"):
        value = json.loads(line[len("PYRO_ART_AVAILABLE:"):])
        return {"artwork_available": bool(value)}
    if line.startswith("PYRO_ARTFILES:"):
        value = json.loads(line[len("PYRO_ARTFILES:"):])
        return {"artwork_files": value if isinstance(value, list) else []}
    if line.startswith("PYRO_INPUT_EXT:"):
        value = json.loads(line[len("PYRO_INPUT_EXT:"):])
        return {"input_ext": value if isinstance(value, str) else None}
    if line.startswith("PYRO_FILE:"):
        return {"file": json.loads(line[len("PYRO_FILE:"):])}
    if line.startswith("PYRO_PROCESS:"):
        if display is not None:
            display.reset()
        return {"status": "Processing", "detail": "Processing media"}
    if not line.startswith("PYRO_PROGRESS:"):
        return {}
    data = json.loads(line[len("PYRO_PROGRESS:"):])
    total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
    downloaded = data.get("downloaded_bytes") or 0
    fraction = min(1.0, max(0.0, downloaded / total)) if total else 0
    detail = f"{fraction:.0%}" if total else "Downloading"
    speed = data.get("speed")
    eta = data.get("eta")
    if type(speed) in (int, float) and math.isfinite(speed) and speed >= 0:
        detail += f"  ·  {speed / 1024 / 1024:.1f} MiB/s"
    if type(eta) in (int, float) and math.isfinite(eta) and eta >= 0:
        detail += f"  ·  {int(eta) // 60}:{int(eta) % 60:02d} remaining"
    if data.get("status") == "finished":
        detail = "Preparing media"
        if display is not None:
            display.reset()
    elif display is not None:
        detail = display.detail(data, fraction, total)
    return {"progress": fraction, "detail": detail, "status": "Downloading"}


def verify_outputs(files):
    if not files:
        raise RuntimeError("yt-dlp exited without reporting a downloaded file")
    total = 0
    for filename in files:
        if not isinstance(filename, str) or not filename:
            raise RuntimeError("yt-dlp reported an invalid output path")
        try:
            info = Path(filename).stat()
        except OSError as error:
            raise RuntimeError(f"Unable to verify downloaded file: {filename}: {error}") from error
        if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
            raise RuntimeError(f"Downloaded output is not a nonempty file: {filename}")
        total += info.st_size
    return total


def staging_directory(job):
    token = hashlib.sha256(job.id.encode("utf-8")).hexdigest()[:32]
    return Path(job.output).expanduser().resolve() / ".pyrobolic" / token


def prepare_staging(job, canceled=None):
    stage = staging_directory(job)
    if stage.is_symlink() or stage.parent.is_symlink():
        raise RuntimeError("The download staging directory must not be a symbolic link")
    stage.mkdir(parents=True, exist_ok=True)
    marker = stage / "owner.json"
    owner = {"job": job.id, "url": job.url}
    if marker.exists() or marker.is_symlink():
        if marker.is_symlink() or json.loads(marker.read_text()) != owner:
            raise RuntimeError("The staging directory belongs to another download")
    else:
        if any(stage.iterdir()):
            raise RuntimeError("Refusing to reuse an unrecognized staging directory")
        atomic_json(marker, owner)
    # A previous publish may have succeeded before saving state failed. Never let
    # a retried postprocessor modify a final file through a retained hard link.
    for path in stage.glob("media.*"):
        if path.is_symlink():
            raise RuntimeError("A staged output must not be a symbolic link")
        if path.is_file() and path.stat().st_nlink > 1:
            descriptor, temporary = tempfile.mkstemp(dir=stage, prefix="detach-")
            try:
                with os.fdopen(descriptor, "wb") as target, path.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        if canceled is not None and canceled.is_set():
                            raise InterruptedError("Canceled")
                        target.write(chunk)
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
    return stage


def file_identity(path):
    info = path.stat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def discard_staging(job):
    """Delete only this job's recognized staging files, never its published outputs."""
    if job.output_stem is None:
        return
    stage = staging_directory(job)
    if stage.is_symlink() or stage.parent.is_symlink():
        raise RuntimeError("Refusing to remove symbolic-link staging.")
    if not stage.exists():
        return
    marker = stage / "owner.json"
    if marker.is_symlink() or json.loads(marker.read_text()) != {"job": job.id, "url": job.url}:
        raise RuntimeError("The staging directory belongs to another download.")
    paths = list(stage.iterdir())
    for path in paths:
        if (path.is_symlink() or not path.is_file() or
                not (path.name.startswith("media.") or path.name in {"owner.json", "published.json"})):
            raise RuntimeError("Staging contains unrecognized files; no files were removed.")
    # Leave the ownership marker until last, so interrupted cleanup remains retryable.
    for path in paths:
        if path != marker:
            path.unlink()
    marker.unlink()
    stage.rmdir()
    try:
        stage.parent.rmdir()
    except OSError:
        pass


def publish_file(source, target, canceled, temporary_directory=None):
    """Never replace an existing path. Ordinary local filesystems need no media copy."""
    try:
        os.link(source, target)
        return
    except OSError as error:
        if error.errno not in {errno.EPERM, errno.EXDEV, errno.EOPNOTSUPP, errno.ENOSYS}:
            raise
    # Copy to a hidden temporary file, then atomically publish without replacing
    # a concurrent creator. A crash cannot expose a partial final-named file.
    if target.exists() or target.is_symlink():
        raise FileExistsError(errno.EEXIST, "Output already exists", str(target))
    directory = temporary_directory or target.parent
    prefix = "media.publish-" if temporary_directory is not None else ".pyro-publish-"
    fd, temporary = tempfile.mkstemp(prefix=prefix, dir=directory)
    try:
        with os.fdopen(fd, "wb") as output:
            with source.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    if canceled.is_set():
                        raise InterruptedError("Canceled")
                    output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if canceled.is_set():
            raise InterruptedError("Canceled")
        rename_no_replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


class StagingCleanupError(OSError):
    """History was saved, but owned temporary files could not be removed."""


def planned_suffixes(job):
    """None means the extractor must first resolve the complete output set."""
    from .formats import output_extension
    extension = output_extension(job.mode, job.audio_format, job.container)
    if (extension is None or subtitle_mode(job) in {"sidecar", "both"} or
            job.artwork_mode in {"save", "both"} or job.split_chapters):
        return None
    return {"." + extension} | ({".description"} if job.save_description else set())


def reserved_stems(jobs, job, suffixes):
    return [other.output_stem for other in jobs
            if other is not job and other.status in ACTIVE and other.output == job.output and other.output_stem
            and (suffixes is None or planned_suffixes(other) is None or
                 not suffixes.isdisjoint(planned_suffixes(other)))]


class Queue:
    def __init__(self, store: Store, on_change=lambda: None):
        self.store = store
        self.on_change = on_change
        self.lock = threading.RLock()
        self.running: dict[str, Process] = {}
        self.stopped = False
        self.persistence_error = ""
        self.dirty = {}
        self.full_refresh = True
        self.completed = {}
        self.batches = {}
        for job in store.jobs:
            self.batches.setdefault(job.batch_id or job.id, []).append(job)

    def take_completions(self):
        """Drain verified, saved completions once a submission's remaining work settles."""
        if not self.lock.acquire(blocking=False):
            return None
        try:
            if self.persistence_error:
                return []
            result = []
            groups = {}
            for job in self.completed.values():
                groups.setdefault(job.batch_id or job.id, []).append(job)
            for key, jobs in groups.items():
                batch = self.batches.get(key, jobs)
                if any(job.status in ACTIVE for job in batch):
                    continue
                result.append(Completion(jobs, sum(j.status == "Failed" for j in batch),
                                         sum(j.status in {"Canceled", "Interrupted"} for j in batch)))
                for job in jobs:
                    del self.completed[job.id]
                self.batches.pop(key, None)
            return result
        finally:
            self.lock.release()

    def changes(self):
        """Nonblocking UI handoff; progress copies only changed jobs, never all history."""
        if not self.lock.acquire(blocking=False):
            return None
        try:
            full = self.full_refresh
            jobs = self.store.jobs if full else self.dirty.values()
            result = (full, [replace(job, files=job.files[:], log=job.log[:]) for job in jobs],
                      self.store.paused, self.persistence_error)
            self.dirty.clear()
            self.full_refresh = False
            return result
        finally:
            self.lock.release()

    def snapshot(self) -> list[Job]:
        with self.lock:
            return copy.deepcopy(self.store.jobs)

    def _notify(self, persist=True, job=None):
        if persist:
            try:
                self.store.save()
                self.persistence_error = ""
            except Exception as error:
                self.persistence_error = str(error)
        if job is None:
            self.full_refresh = True
        else:
            self.dirty[job.id] = job
        self.on_change()

    def retry_save(self):
        with self.lock:
            self._notify()
            from .history import retention_seconds
            if not self.persistence_error and retention_seconds(self.store.settings) == -1:
                self.prune_history()

    def _require_writable_state(self):
        if self.persistence_error:
            raise OSError("Saved data could not be updated. Retry saving before adding or retrying downloads.")

    def add(self, jobs: list[Job], settings: Settings | None = None):
        with self.lock:
            self._require_writable_state()
            pending = list(self.store.jobs)
            existing_by_folder = {}
            for job in jobs:
                from .auth import public_url, validate_ref
                validate_ref(job.auth_ref)
                if job.auth_ref and (job.url != public_url(job.url) or job.metadata or job.filename is None):
                    raise ValueError("Authenticated jobs require a redacted source address and named staging without source metadata.")
                validate_url(job.url)
                validate_source_choice(job)
                validate_output_options(job)
                if job.filename is not None:
                    validate_name(job.filename)
                    job.output = str(Path(job.output).expanduser().resolve())
                    suffixes = planned_suffixes(job)
                    reserved = reserved_stems(pending, job, suffixes)
                    folder = Path(job.output)
                    if job.output not in existing_by_folder:
                        existing_by_folder[job.output] = [p.name for p in folder.iterdir()] if folder.exists() else []
                    existing = existing_by_folder[job.output]
                    job.output_stem = available_stem(output_stem(job.filename, job.media_id, job.url, job.include_id),
                                                     existing, reserved, suffixes=suffixes)
                    pending.append(job)
            previous_settings = self.store.settings
            previous_count = len(self.store.jobs)
            batch_id = uuid.uuid4().hex
            for job in jobs:
                job.batch_id = batch_id
            self.store.jobs.extend(jobs)
            if settings is not None:
                self.store.settings = settings
            try:
                self.store.save()
            except Exception as error:
                del self.store.jobs[previous_count:]
                self.store.settings = previous_settings
                self.persistence_error = str(error)
                self.full_refresh = True
                self.on_change()
                raise
            self.batches[batch_id] = jobs[:]
            self._schedule()
            self._notify(persist=False)

    def configure(self, settings: Settings):
        with self.lock:
            settings.concurrency = max(1, min(10, settings.concurrency))
            previous = self.store.settings
            self.store.settings = settings
            try:
                self.store.save()
            except Exception as error:
                self.store.settings = previous
                self.persistence_error = str(error)
                self._notify(persist=False)
                raise
            self.persistence_error = ""
            self._notify(persist=False)
            self._schedule()

    def _schedule(self):
        if self.stopped or self.store.paused:
            return
        # Protected URLs share an origin in state, but distinct keyring jobs can run concurrently.
        busy = {(j.auth_ref or j.url, str(Path(j.output).expanduser().resolve())) for j in self.store.jobs if j.id in self.running}
        for job in self.store.jobs:
            if len(self.running) >= self.store.settings.concurrency:
                break
            if job.status == "Queued":
                destination = (job.auth_ref or job.url, str(Path(job.output).expanduser().resolve()))
                if destination in busy:
                    continue
                busy.add(destination)
                process = Process()
                self.running[job.id] = process
                job.status, job.detail = "Downloading", "Preparing download"
                threading.Thread(target=self._download, args=(job, process), daemon=True).start()

    def set_paused(self, paused: bool):
        with self.lock:
            self.store.paused = paused
            self._notify()
            self._schedule()

    def pause_transfer(self, identifier, paused):
        with self.lock:
            job = next(j for j in self.store.jobs if j.id == identifier)
            process = self.running.get(identifier)
            if process is None or (not job.transfer_paused and job.status != "Downloading"):
                raise RuntimeError("Only a running transfer can be paused.")
            process.set_paused(paused)
            job.transfer_paused = paused
            self._notify(job=job)

    def cancel_all(self):
        with self.lock:
            for job in self.store.jobs:
                if job.id in self.running:
                    job.status, job.detail = "Canceling", "Canceling"
                    self.running[job.id].cancel()
                elif job.status == "Queued":
                    job.status, job.detail = "Canceled", "Canceled before starting"
            self._notify()

    def retry_failed(self):
        with self.lock:
            self._require_writable_state()
            for job in self.store.jobs:
                if job.status in {"Failed", "Interrupted"}:
                    job.status, job.detail, job.progress = "Queued", "Waiting", 0
                    job.files, job.log = [], []
                    job.warnings, job.selected_source = [], ""
            self._notify()
            self._schedule()

    def _download(self, job: Job, process: Process):
        stage = None
        private = None
        authentication = None
        actual_url = ""
        private_error = "Authenticated download failed. Refresh authentication or check the source and output options."
        selected_subtitles, subtitle_files = [], []
        source_chapters = None
        chapter_files = []
        artwork_available, artwork_files = None, []
        description_available = None
        input_ext = None
        selection_seen = False
        startup_finished = threading.Event()
        startup_thread = None
        progress_display = ProgressDisplay()

        def run_phase(args, timeout=30):
            nonlocal process
            process.close()
            with self.lock:
                if process.cancelled.is_set():
                    raise InterruptedError("Canceled")
                process = Process()
                self.running[job.id] = process
            return process.communicate(args, timeout=timeout)

        try:
            with self.lock:
                job.warnings, job.selected_source = [], ""
            if subtitle_mode(job) == "embed" and job.output_stem is None:
                raise RuntimeError("Embedded-only subtitles require a new named download with owned staging.")
            if not shutil.which("ffmpeg"):
                raise RuntimeError("FFmpeg is missing. Install the ffmpeg package.")
            Path(job.output).expanduser().mkdir(parents=True, exist_ok=True)
            if job.output_stem is not None:
                stage = prepare_staging(job, process.cancelled)
            from .runtime import ensure_support
            ensure_support(job.url, process, self.store.directory)
            args = download_arguments(job, engine_path(self.store.directory))
            if job.auth_ref:
                from .auth import CredentialVault, EngineAuthentication, public_url
                actual_url, authentication = CredentialVault().get(job.auth_ref, process)
                if public_url(actual_url) != job.url:
                    raise RuntimeError("The stored authentication does not match this source.")
                private = EngineAuthentication(actual_url, authentication)
                child = process.start(private.arguments(args), pass_fds=private.fds)
            else:
                child = process.start(args)
            startup_started = time.monotonic()
            sponsor_report = None
            sponsor_error = False

            def waiting_feedback():
                while not startup_finished.wait(STARTUP_FEEDBACK_INTERVAL):
                    with self.lock:
                        if startup_finished.is_set() or process.cancelled.is_set() or job.status != "Downloading":
                            return
                        job.detail = startup_detail(selection_seen, time.monotonic() - startup_started)
                        self._notify(persist=False, job=job)

            with self.lock:
                if job.status != "Canceling":
                    job.detail = startup_detail(False, 0)
                    self._notify(persist=False, job=job)
            startup_thread = threading.Thread(target=waiting_feedback, daemon=True, name="pyro-startup-feedback")
            startup_thread.start()
            with child.stdout:
                for line in child.stdout:
                    process.wait_resumed()
                    line = line.strip()
                    if not line:
                        continue
                    if job.sponsorblock != "off":
                        if line.startswith("PYRO_SPONSOR:"):
                            sponsor_report = json.loads(line[len("PYRO_SPONSOR:"):])
                        if "SponsorBlock" in line and ("WARNING:" in line or "ERROR:" in line):
                            sponsor_error = True
                    try:
                        update = progress_update(line, progress_display)
                    except (ValueError, TypeError, KeyError):
                        update = {}
                    with self.lock:
                        if "progress" in update or update.get("status") == "Processing" or update.get("file"):
                            startup_finished.set()
                        if "selection" in update:
                            selection_seen = True
                            selected = update.pop("selection")
                            job.selected_source = ", ".join(f"{key}: {str(value)[:160]}" for key, value in selected.items()
                                                            if key in {"format_id", "format_note", "vcodec", "acodec", "fps", "abr", "height"})
                            job.warnings = source_feedback(job, selected)
                            if authentication:
                                job.selected_source = authentication.scrub(job.selected_source, actual_url)
                                job.warnings = [authentication.scrub(value, actual_url) for value in job.warnings]
                            if not startup_finished.is_set() and job.status != "Canceling":
                                job.detail = startup_detail(True, time.monotonic() - startup_started)
                        if "input_ext" in update:
                            input_ext = update.pop("input_ext")
                        if "subtitle_languages" in update:
                            selected_subtitles = update.pop("subtitle_languages")
                            missing = subtitle_feedback(job, selected_subtitles)
                            if missing and job.subtitle_missing == "fail":
                                raise RuntimeError(missing)
                        if "subtitle_files" in update:
                            subtitle_files = update.pop("subtitle_files")
                        if "source_chapters" in update:
                            source_chapters = update.pop("source_chapters")
                        if "chapter_files" in update:
                            chapter_files = update.pop("chapter_files")
                        if "artwork_available" in update:
                            artwork_available = update.pop("artwork_available")
                        if "description_available" in update:
                            description_available = update.pop("description_available")
                        if "artwork_files" in update:
                            artwork_files = update.pop("artwork_files")
                        if update.get("file"):
                            job.files.append(update.pop("file"))
                        if job.status != "Canceling":
                            for key, value in update.items():
                                setattr(job, key, value)
                        if not line.startswith("PYRO_"):
                            if job.auth_ref:
                                if "ERROR:" in line:
                                    from .auth import safe_auth_error
                                    private_error = safe_auth_error(RuntimeError(line))
                            else:
                                job.log = (job.log + [line])[-200:]
                        self._notify(persist=False, job=job)
            code = child.wait()
            process.wait_resumed()
            with self.lock:
                job.status = "Processing"
            if process.cancelled.is_set():
                raise InterruptedError("Canceled")
            if code:
                from .artwork import artwork_mode, discard_failed_images
                if artwork_mode(job) != "off":
                    discard_failed_images(stage)
                if job.auth_ref:
                    from .auth import AuthenticationError
                    raise AuthenticationError(private_error)
                raise RuntimeError(next((line for line in reversed(job.log) if "ERROR:" in line),
                                        f"yt-dlp exited with status {code}"))
            if not selection_seen and preference_sort(job):
                job.warnings = job.warnings + ["The engine did not report source details; preferences could not be verified."]
            total_bytes = verify_outputs(job.files)
            if job.sponsorblock != "off":
                if sponsor_error:
                    raise RuntimeError("SponsorBlock reported a service or processing problem. The file was not published; retry or turn SponsorBlock off.")
            from .encoding import reencoding_enabled, finish_encoding
            if reencoding_enabled(job):
                with self.lock:
                    job.detail = "Re-encoding and verifying video"
                    self._notify(persist=False, job=job)
                finish_encoding(job, stage, input_ext, job.files + chapter_files, run_phase,
                                lambda source, target: publish_file(source, target, process.cancelled,
                                                                   temporary_directory=stage))
                total_bytes = verify_outputs(job.files)
            from .metadata import effective_policy, expected_chapters, clipped_metadata, verify_chapters
            chapter_requested = verify_chapters(job)
            info_requested = effective_policy(job, "source_info_policy") == "embed"
            if chapter_requested and source_chapters is None:
                raise RuntimeError("The engine did not report source chapters; chapter output could not be verified.")
            chapters_expected = chapter_requested and expected_chapters(job, source_chapters or [])
            if effective_policy(job, "chapter_policy") == "embed" and not chapters_expected:
                job.warnings += ["No source chapters available in the selected time range; no chapter markers were added."]
            if chapters_expected or info_requested:
                for media in job.files:
                    info_expected = info_requested and Path(media).suffix.lower() in {".mkv", ".mka"}
                    if info_requested and not info_expected:
                        job.warnings += ["Source data attachments require MKV or MKA output; no source data was attached."]
                    if not chapters_expected and not info_expected:
                        continue
                    process.close()
                    with self.lock:
                        if process.cancelled.is_set():
                            raise InterruptedError("Canceled")
                        process = Process()
                        self.running[job.id] = process
                    result = json.loads(process.communicate([
                        "ffprobe", "-v", "error", "-show_chapters", "-show_streams", "-of", "json", media], timeout=30))
                    if chapters_expected and not result.get("chapters"):
                        raise RuntimeError("Source chapters were available but were not embedded in the output.")
                    if chapters_expected and (job.clip_start or job.clip_end is not None):
                        if stage is None:
                            raise RuntimeError("Clipped chapter output requires owned staging.")
                        chapter_file = stage / "media.chapters.txt"
                        adjusted = stage / ("media.chapters" + Path(media).suffix)
                        chapter_file.write_text(clipped_metadata(job, source_chapters), encoding="utf-8")
                        process.close()
                        with self.lock:
                            if process.cancelled.is_set():
                                raise InterruptedError("Canceled")
                            process = Process()
                            self.running[job.id] = process
                        process.communicate(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", media,
                                             "-f", "ffmetadata", "-i", str(chapter_file), "-map", "0",
                                             "-map_metadata", "0", "-map_chapters", "1", "-c", "copy",
                                             str(adjusted)], timeout=120)
                        verify_outputs([str(adjusted)])
                        adjusted.replace(media)
                        chapter_file.unlink()
                        process.close()
                        with self.lock:
                            if process.cancelled.is_set():
                                raise InterruptedError("Canceled")
                            process = Process()
                            self.running[job.id] = process
                        result = json.loads(process.communicate([
                            "ffprobe", "-v", "error", "-show_chapters", "-show_streams", "-of", "json", media], timeout=30))
                        wanted = sum(expected_chapters(job, [chapter]) for chapter in source_chapters)
                        if len(result.get("chapters", [])) != wanted:
                            raise RuntimeError("The clipped output does not contain all requested chapter markers.")
                    if info_expected and not any(stream.get("codec_type") == "attachment" and
                                                 stream.get("tags", {}).get("filename") == "info.json" and
                                                 stream.get("tags", {}).get("mimetype") == "application/json"
                                                 for stream in result.get("streams", [])):
                        raise RuntimeError("Requested source data was not attached to the output.")
            from .artwork import finish_artwork
            saved_artwork = finish_artwork(job, stage, artwork_files, artwork_available, run_phase)
            submode = subtitle_mode(job)
            if submode != "off":
                missing = subtitle_feedback(job, selected_subtitles)
                if missing:
                    if job.subtitle_missing == "fail":
                        raise RuntimeError(missing)
                    job.warnings = job.warnings + [missing]
                if selected_subtitles:
                    if len(subtitle_files) != len(selected_subtitles):
                        raise RuntimeError("The engine did not report all requested subtitle files")
                    verify_outputs(subtitle_files)
                    for name in subtitle_files:
                        path = Path(name)
                        expected = stage or Path(job.output).expanduser().resolve()
                        if path.parent != expected or path.is_symlink():
                            raise RuntimeError("A subtitle output is outside this download's destination")
                    if submode in {"embed", "both"}:
                        for media in job.files:
                            process.close()
                            # Hand cancellation ownership to the verification subprocess.
                            with self.lock:
                                if process.cancelled.is_set():
                                    raise InterruptedError("Canceled")
                                process = Process()
                                self.running[job.id] = process
                            streams = json.loads(process.communicate([
                                "ffprobe", "-v", "error", "-select_streams", "s", "-show_entries", "stream=index",
                                "-of", "json", media], timeout=30)).get("streams", [])
                            if len(streams) < len(selected_subtitles):
                                raise RuntimeError("Requested subtitles were not embedded in the output")
                    if submode == "embed":
                        for name in subtitle_files:
                            Path(name).unlink()
                    else:
                        job.files = list(dict.fromkeys(job.files + subtitle_files))
                    total_bytes = verify_outputs(job.files)
            from .metadata import chapter_outputs, description_outputs
            if job.sponsorblock != "off":
                from .sponsorblock import verify as verify_sponsorblock
                job.warnings += verify_sponsorblock(job, sponsor_report,
                                                    [name for name in job.files if name not in subtitle_files], run_phase)
            saved_chapters = chapter_outputs(job, stage, source_chapters, chapter_files, run_phase)
            saved_description = description_outputs(job, stage, description_available)
            if saved_artwork or saved_chapters or saved_description:
                job.files = list(dict.fromkeys(job.files + saved_artwork + saved_chapters + saved_description))
                total_bytes = verify_outputs(job.files)
            if stage is not None:
                with self.lock:
                    if process.cancelled.is_set():
                        raise InterruptedError("Canceled")
                    job.status, job.detail = "Processing", "Saving files"
                    self._notify(persist=False, job=job)
                final_files = self._publish(job, stage, process)
                total_bytes = verify_outputs(final_files)
                with self.lock:
                    job.files = final_files
            with self.lock:
                if process.cancelled.is_set():
                    raise InterruptedError("Canceled")
                job.status, job.detail, job.progress = "Completed", "Download complete", 1
                job.completed_at, job.total_bytes = time.time(), total_bytes
        except Exception as error:
            with self.lock:
                if job.auth_ref:
                    from .auth import safe_auth_error
                    message = safe_auth_error(error)
                else:
                    message = str(error)
                job.status = "Canceled" if process.cancelled.is_set() else "Failed"
                job.detail = "Canceled" if process.cancelled.is_set() else message
                job.log = (job.log + [message])[-200:]
        finally:
            startup_finished.set()
            if startup_thread is not None and startup_thread.ident is not None:
                startup_thread.join()
            process.close()
            if private:
                private.close()
            with self.lock:
                job.transfer_paused = False
                self.running.pop(job.id, None)
                if job.status == "Completed":
                    self.completed[job.id] = replace(job, files=job.files[:], log=[])
                self._notify()
                if job.status == "Completed" and not self.persistence_error and stage is not None:
                    try:
                        discard_staging(job)
                    except (OSError, ValueError, RuntimeError):
                        pass  # Ownership remains intact for the periodic retry.
                from .history import retention_seconds
                if job.status == "Completed" and not self.persistence_error and retention_seconds(self.store.settings) == -1:
                    try:
                        self.prune_history()
                    except OSError:
                        pass  # The saved completion remains; the normal persistence error offers Retry.
                self._schedule()

    def _publish(self, job, stage, process):
        sources = list(dict.fromkeys(Path(path) for path in job.files))
        for path in sources:
            if path.parent != stage or not path.name.startswith("media.") or path.is_symlink():
                raise RuntimeError("yt-dlp reported an output outside this download's staging directory")
        for path in stage.iterdir():
            if (job.subtitle_mode == "legacy" and path.name.startswith("media.") and
                    path.suffix in {".vtt", ".srt", ".ass", ".lrc"} and path not in sources):
                if path.is_symlink():
                    raise RuntimeError("A subtitle output is a symbolic link")
                sources.append(path)
        verify_outputs([str(path) for path in sources])
        journal = stage / "published.json"
        if journal.is_symlink():
            raise RuntimeError("The publication journal must not be a symbolic link")
        records = json.loads(journal.read_text()) if journal.exists() else {}
        if not isinstance(records, dict):
            raise RuntimeError("Invalid publication journal")
        folder = Path(job.output)
        suffixes = {source.name[len("media"):] for source in sources}

        def choose_name():
            with self.lock:
                reserved = reserved_stems(self.store.jobs, job, suffixes)
                previous = job.output_stem
                chosen = available_stem(output_stem(job.filename, job.media_id, job.url, job.include_id),
                                        [path.name for path in folder.iterdir()], reserved, suffixes=suffixes)
                if chosen == previous:
                    return
                job.output_stem = chosen
                self._notify()
                if self.persistence_error:
                    job.output_stem = previous
                    raise OSError("Unable to save the new filename after a filename conflict")

        # Automatic formats and sidecars are now known. Do not rename a partially
        # published group on retry: its journal identifies the already-owned files.
        if not records:
            choose_name()

        def ours(source, target):
            try:
                return (not target.is_symlink() and
                        (source.samefile(target) or records.get(target.name) == [file_identity(source), file_identity(target)]))
            except FileNotFoundError:
                return False

        for _attempt in range(100):
            validate_stem(job.output_stem)
            targets = [folder / (job.output_stem + source.name[len("media"):]) for source in sources]
            created = []
            try:
                for source, target in zip(sources, targets):
                    if process.cancelled.is_set():
                        raise InterruptedError("Canceled")
                    if not ours(source, target):
                        publish_file(source, target, process.cancelled, temporary_directory=stage)
                        created.append((source, target))
                    records[target.name] = [file_identity(source), file_identity(target)]
                    atomic_json(journal, records)
                return [str(path) for path in targets]
            except FileExistsError:
                for source, target in created:
                    if ours(source, target):
                        target.unlink()
                        records.pop(target.name, None)
                atomic_json(journal, records)
                choose_name()
                job.log = (job.log + [f"Filename already exists; saving as {job.output_stem}"])[-200:]
        raise RuntimeError("Repeated filename conflicts. Try again after other downloads finish.")

    def cancel(self, identifier: str):
        with self.lock:
            job = next(j for j in self.store.jobs if j.id == identifier)
            if identifier in self.running:
                job.status, job.detail = "Canceling", "Canceling"
                self.running[identifier].cancel()
            elif job.status == "Queued":
                job.status, job.detail = "Canceled", "Canceled before starting"
            self._notify()

    def retry(self, identifier: str):
        with self.lock:
            self._require_writable_state()
            job = next(j for j in self.store.jobs if j.id == identifier)
            if job.status not in TERMINAL or job.status == "Completed":
                return
            job.status, job.detail, job.progress = "Queued", "Waiting", 0
            job.files, job.log = [], []
            job.warnings, job.selected_source = [], ""
            self._notify()
            self._schedule()

    def redownload(self, identifier):
        """Create a new named job without changing the source record or published files."""
        from .naming import suggest_name
        from .preferences import preference_values
        with self.lock:
            self._require_writable_state()
            original = next(j for j in self.store.jobs if j.id == identifier)
            if original.status not in TERMINAL or identifier in self.running:
                raise RuntimeError("Wait for the original download to stop first.")
            options = {key: getattr(original, key) for key in (
                "mode", "quality", "audio_format", "container", "metadata", "subtitles",
                "media_id", "include_id", "source_format", "source_audio", "source_summary",
                "video_processing", "clip_start", "clip_end", "precise_cuts", "auth_ref")}
            job = Job(original.url, original.title, original.output,
                      filename=original.filename or suggest_name(original.title, original.url),
                      **preference_values(original), **options)
            self.add([job])
            return job.id

    def saved_file(self, identifier, filename):
        """Validate an explicitly requested History file off the UI thread."""
        from .batch import MEDIA_EXTENSIONS
        with self.lock:
            job = next(j for j in self.store.jobs if j.id == identifier)
            if job.status != "Completed" or filename not in job.files:
                raise ValueError("This file is not part of a completed download.")
            path = Path(filename)
            if path.suffix.lower() not in MEDIA_EXTENSIONS | {".srt", ".vtt", ".ass", ".lrc", ".ttml"}:
                raise ValueError("Open this file from its folder instead.")
            verify_outputs([filename])
            return str(path.absolute())

    def discard_partial(self, identifier):
        with self.lock:
            self._require_writable_state()
            job = next(j for j in self.store.jobs if j.id == identifier)
            if job.status not in TERMINAL or identifier in self.running:
                raise RuntimeError("Wait for the download to stop before removing retained files.")
            discard_staging(job)

    def clean_abandoned(self):
        """Only marked staging in configured/recorded destinations, never a recursive disk scan."""
        with self.lock:
            self._require_writable_state()
            identifiers = {job.id for job in self.store.jobs if job.status != "Completed"} | set(self.running)
            folders = {Path(path).expanduser().resolve() for path in
                       [self.store.settings.output] + [job.output for job in self.store.jobs]}
            removed = skipped = 0
            for folder in folders:
                root = folder / ".pyrobolic"
                if not root.exists() or root.is_symlink():
                    continue
                for stage in root.iterdir():
                    try:
                        marker = stage / "owner.json"
                        if stage.is_symlink() or not stage.is_dir() or marker.is_symlink():
                            raise ValueError("Unrecognized staging")
                        if marker.stat().st_size > 65536:
                            raise ValueError("Oversized ownership record")
                        owner = json.loads(marker.read_text())
                        if not isinstance(owner, dict) or not isinstance(owner.get("job"), str) or not isinstance(owner.get("url"), str):
                            raise ValueError("Invalid owner")
                        if owner["job"] in identifiers:
                            continue
                        job = Job(owner["url"], "Abandoned Download", str(folder), id=owner["job"],
                                  filename="Recovered", output_stem="Recovered")
                        if staging_directory(job) != stage:
                            raise ValueError("Invalid staging identity")
                        discard_staging(job)
                        removed += 1
                    except (OSError, ValueError, RuntimeError):
                        skipped += 1
            return removed, skipped

    def remove_history(self, identifiers, *, preserve_completions=False):
        with self.lock:
            self._require_writable_state()
            previous = self.store.jobs[:]
            if any(j.id in identifiers and (j.status not in TERMINAL or j.id in self.running) for j in previous):
                raise RuntimeError("Wait for active downloads to stop before removing them.")
            self.store.jobs[:] = [j for j in previous if j.id not in identifiers]
            try:
                self.store.save()
            except Exception as error:
                self.store.jobs[:] = previous
                self.persistence_error = str(error)
                self._notify(persist=False)
                raise
            cleanup_failures = 0
            for job in previous:
                if job.id in identifiers:
                    try:
                        discard_staging(job)
                    except (OSError, ValueError, RuntimeError):
                        cleanup_failures += 1
            retained = {j.id for j in self.store.jobs}
            if not preserve_completions:
                self.completed = {key: job for key, job in self.completed.items() if key in retained}
            pending = {job.batch_id or job.id for job in self.completed.values()}
            self.batches = {key: jobs if key in pending else [j for j in jobs if j.id in retained]
                            for key, jobs in self.batches.items()
                            if key in pending or any(j.id in retained for j in jobs)}
            self._notify(persist=False)
            if cleanup_failures:
                raise StagingCleanupError(f"History removed, but {cleanup_failures} temporary download folder(s) could not be cleaned. Downloaded files were not removed. Review Abandoned Downloads in Advanced Settings.")

    def set_history_retention(self, days):
        from .history import expired_completed
        with self.lock:
            previous = self.store.settings
            self.store.settings = replace(previous, history_retention_days=days, history_retention_seconds=None)
            try:
                identifiers = expired_completed(self.store.jobs, days, self.running)
                self.remove_history(identifiers)
            except StagingCleanupError:
                raise
            except Exception:
                self.store.settings = previous
                self._notify(persist=False)
                raise
            return len(identifiers)

    def prune_history(self, *, now=None):
        from .history import expired_completed_seconds, retention_seconds
        with self.lock:
            identifiers = expired_completed_seconds(self.store.jobs, retention_seconds(self.store.settings), self.running, now=now)
            if identifiers:
                self.remove_history(identifiers, preserve_completions=True)
            return len(identifiers)

    def set_history_clear(self, seconds):
        with self.lock:
            previous = self.store.settings
            self.store.settings = replace(previous, history_retention_seconds=seconds)
            try:
                from .history import expired_completed_seconds
                identifiers = expired_completed_seconds(self.store.jobs, seconds, self.running)
                self.remove_history(identifiers, preserve_completions=True)
            except StagingCleanupError:
                raise
            except Exception:
                self.store.settings = previous
                self._notify(persist=False)
                raise
            return len(identifiers)

    def clear_history(self):
        with self.lock:
            self.remove_history({j.id for j in self.store.jobs if j.status in TERMINAL})

    def shutdown(self):
        with self.lock:
            self.stopped = True
            for job in self.store.jobs:
                if job.status == "Queued":
                    job.status, job.detail = "Interrupted", "Application closed before starting"
            for process in list(self.running.values()):
                process.cancel()
            self._notify()
