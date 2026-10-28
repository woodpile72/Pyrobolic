# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Bounded text imports, cancelable batch inspection and explicit M3U export."""

from dataclasses import dataclass, field, replace
from functools import partial
import os
from pathlib import Path
import stat
import tempfile
import threading

from .core import Process, inspect_url, publish_file, validate_url
from .naming import validate_name


MAX_BATCH_ITEMS = 1000
MAX_BATCH_BYTES = 1024 * 1024
MEDIA_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".flv", ".ts",
                    ".mp3", ".m4a", ".aac", ".opus", ".ogg", ".flac", ".wav", ".weba"}


@dataclass
class ImportEntry:
    line: int
    url: str
    name: str = ""
    destination: str = ""
    error: str = ""
    items: list[dict] = field(default_factory=list)
    inspected: bool = False


def validated_entry(line, url, name="", destination=""):
    if any(any(ord(c) < 32 for c in value) for value in (url, name, destination)):
        raise ValueError("Use one line per field, without tabs or control characters.")
    entry = ImportEntry(line, validate_url(url), name.strip(), destination.strip())
    if entry.name:
        validate_name(entry.name)
    if entry.destination:
        path = Path(entry.destination).expanduser()
        if not path.is_absolute():
            raise ValueError("Use an absolute destination folder, or leave it empty.")
        entry.destination = str(path)
    return entry


def edited_entry(entries, original, url, name="", destination=""):
    """Validate a draft without mutating the import or repeating source inspection."""
    if not any(entry is original for entry in entries):
        raise ValueError("This entry is no longer in the import list.")
    candidate = validated_entry(original.line, url, name, destination)
    key = (candidate.url, candidate.name, candidate.destination)
    for entry in entries:
        if entry is not original and (entry.inspected or not entry.error):
            if key == (entry.url, entry.name, entry.destination):
                raise ValueError("Duplicate entry; change the URL, name or folder.")
    if key == (original.url, original.name, original.destination) and (original.inspected or not original.error):
        return original
    if candidate.url == original.url and original.inspected and not original.error:
        if candidate.name and len(original.items) != 1:
            raise ValueError("A custom name requires one video. Remove the name to import a playlist.")
        items = []
        for item in original.items:
            item = dict(item)
            item.pop("import_name", None)
            item.pop("destination", None)
            if candidate.name:
                item["import_name"] = candidate.name
            if candidate.destination:
                item["destination"] = candidate.destination
            items.append(item)
        candidate = replace(candidate, items=items, inspected=True)
    return candidate


def replace_import_line(text, entry, syntax="tsv"):
    lines = text.splitlines()
    if not 1 <= entry.line <= len(lines):
        raise ValueError("The import list changed. Reopen this entry before editing it.")
    separator, fields = import_fields(entry, syntax)
    if any(separator in value for value in fields):
        raise ValueError("A field contains the delimiter used by this import format.")
    lines[entry.line - 1] = separator.join(fields).rstrip(separator)
    updated = "\n".join(lines)
    if len(updated.encode("utf-8")) > MAX_BATCH_BYTES:
        raise ValueError("The import is too large (maximum 1 MiB).")
    return updated


def import_fields(entry, syntax):
    if syntax == "tsv":
        return "\t", (entry.url, entry.name, entry.destination)
    if syntax == "parabolic":
        return "|", (entry.url, entry.destination, entry.name)
    raise ValueError("Unknown import format")


def parse_batch(text, syntax="tsv"):
    """Plain URLs or tab-separated URL, optional name, optional absolute folder."""
    if len(text.encode("utf-8")) > MAX_BATCH_BYTES:
        raise ValueError("The import is too large (maximum 1 MiB).")
    separator, _ = import_fields(ImportEntry(0, ""), syntax)
    entries, seen = [], set()
    for number, line in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if len(entries) == MAX_BATCH_ITEMS:
            raise ValueError("Import up to 1,000 entries at a time.")
        fields = line.split(separator)
        name_index, folder_index = (1, 2) if syntax == "tsv" else (2, 1)
        entry = ImportEntry(number, fields[0].strip(), fields[name_index].strip() if len(fields) > name_index else "",
                            fields[folder_index].strip() if len(fields) > folder_index else "")
        try:
            if len(fields) > 3:
                raise ValueError("Expected URL, optional name and optional folder, separated by tabs." if syntax == "tsv" else
                                 "Expected URL | optional folder | optional name. Fields cannot contain a pipe.")
            entry = validated_entry(number, entry.url, entry.name, entry.destination)
            key = (entry.url, entry.name, entry.destination)
            if key in seen:
                raise ValueError("Duplicate entry; the first copy is kept.")
            seen.add(key)
        except (ValueError, RuntimeError) as error:
            entry.error = str(error)
        entries.append(entry)
    if not entries:
        raise ValueError("Add at least one HTTP or HTTPS URL.")
    return entries


def read_batch(path):
    if not Path(path).is_file():
        raise ValueError("Choose a regular text file.")
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_BATCH_BYTES + 1)
    if len(data) > MAX_BATCH_BYTES:
        raise ValueError("The import is too large (maximum 1 MiB).")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("Choose a UTF-8 text file.") from None


def inspect_batch_url(url, process, directory, *, transfer=None):
    # One extra item detects an oversized playlist instead of silently truncating it.
    return inspect_url(url, process, directory, playlist_limit=MAX_BATCH_ITEMS + 1,
                       **({"transfer": transfer} if transfer is not None else {}))


class BatchInspection:
    """One owned process at a time; cancellation cannot miss a just-starting process."""

    def __init__(self):
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.process = None
        self.done = threading.Event()

    def cancel(self):
        with self.lock:
            self.cancelled.set()
            if self.process:
                self.process.cancel()

    def run(self, entries, directory, progress=lambda *_: None, inspect=inspect_batch_url, *, transfer=None):
        try:
            if transfer is not None and inspect is inspect_batch_url:
                inspect = partial(inspect_batch_url, transfer=transfer)
            return self._run(entries, directory, progress, inspect)
        finally:
            self.done.set()

    def close(self):
        self.done.wait()

    def _run(self, entries, directory, progress, inspect):
        total = sum(len(entry.items) for entry in entries)
        for index, entry in enumerate(entries):
            if self.cancelled.is_set():
                break
            if entry.inspected or entry.error:
                continue
            process = Process()
            with self.lock:
                if self.cancelled.is_set():
                    break
                self.process = process
            try:
                items = inspect(entry.url, process, directory)
                if self.cancelled.is_set():
                    break
                if entry.name and len(items) != 1:
                    raise ValueError("A custom name requires one video. Remove the name to import a playlist.")
                if len(items) + total > MAX_BATCH_ITEMS:
                    raise ValueError("This entry would exceed 1,000 reviewed items. Import it separately.")
                entry.items = [dict(item, **({"import_name": entry.name} if entry.name else {}),
                                    **({"destination": entry.destination} if entry.destination else {})) for item in items]
                total += len(items)
            except InterruptedError:
                break
            except Exception as error:
                entry.error = str(error)
            finally:
                process.close()
                with self.lock:
                    self.process = None
            entry.inspected = True
            progress(index + 1, len(entries))
        return entries


def numbered_name(name, position, count):
    return validate_name(f"{position:0{max(2, len(str(count)))}d} - {name}")


def export_m3u(jobs, target):
    """Explicit export of verified completed media, never overwrite an existing file."""
    target = Path(target).absolute()
    lines = ["#EXTM3U"]
    count = 0
    for job in jobs:
        if job.status != "Completed":
            continue
        for filename in job.files:
            path = Path(filename)
            if path.suffix.lower() not in MEDIA_EXTENSIONS:
                continue
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
                raise ValueError(f"The completed media is missing or empty: {path}")
            relative = os.path.relpath(path, target.parent)
            if any(c in relative for c in "\r\n\x00"):
                raise ValueError("A media path contains a line break and cannot be exported to M3U.")
            # Prefix avoids interpreting a filename beginning with '#' as a directive.
            if not relative.startswith("."):
                relative = "./" + relative
            title = " ".join((job.filename or job.title).split())
            lines.extend([f"#EXTINF:-1,{title}", relative])
            count += 1
    if not count:
        raise ValueError("There are no completed media files to export.")
    fd, temporary = tempfile.mkstemp(prefix=".playlist-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(lines) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            publish_file(Path(temporary), target, threading.Event())
        except FileExistsError:
            raise FileExistsError("A file with that name already exists. Choose another playlist name.") from None
    finally:
        Path(temporary).unlink(missing_ok=True)
    return count
