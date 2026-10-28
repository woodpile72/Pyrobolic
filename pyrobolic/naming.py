# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Conservative title suggestions and literal, filesystem-safe output names."""

import hashlib
import re
import unicodedata
from urllib.parse import urlsplit


MAX_NAME_BYTES = 140
MAX_STEM_BYTES = 200
_COUNT = r"\d+(?:[.,\s\u00a0]\d+)*(?:\s?(?:[kmb]|mil|mio\.?))?"
_METRIC = r"(?:views?|reactions?|likes?|comments?|shares?|vues?|r[e\u00e9]actions?|j'aime|vistas?|reacciones|visualizaciones|aufrufe|reaktionen|kommentare)"
_ENGAGEMENT = re.compile(rf"^\s*{_COUNT}\s+{_METRIC}\s*[\u00b7\u2022|\uff5c]\s*", re.I)
_INVALID = '/\\<>:"|?*'
_RESERVED = {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}


def clip_bytes(value, limit):
    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def safe_text(value):
    value = unicodedata.normalize("NFC", value)
    value = "".join(" " if ch in _INVALID or unicodedata.category(ch) in {"Cc", "Cs"}
                    or ch in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069" else ch
                    for ch in value)
    return " ".join(value.split()).strip(" .")


def suggest_name(title, url="", source=""):
    title = str(title or "")
    host = (urlsplit(url).hostname or "").lower()
    facebook = "facebook" in source.lower() or host == "fb.watch" or host == "facebook.com" or host.endswith(".facebook.com")
    if facebook:
        had_counters = bool(_ENGAGEMENT.match(title))
        while match := _ENGAGEMENT.match(title):
            title = title[match.end():]
        # Facebook often supplies a whole post caption rather than a separate video title.
        title = title.splitlines()[0] if title.splitlines() else title
        sentence = re.search(r"[!?](?=\s)", title)
        if had_counters and sentence:
            title = title[:sentence.end()]
    title = safe_text(title)
    if len(title.encode("utf-8")) > MAX_NAME_BYTES:
        shortened = clip_bytes(title, MAX_NAME_BYTES)
        if " " in shortened:
            shortened = shortened.rsplit(" ", 1)[0]
        title = shortened.rstrip(" .")
    if title.split(".", 1)[0].upper() in _RESERVED:
        title = "Video " + clip_bytes(title, MAX_NAME_BYTES - 6).rstrip(" .")
    return title or "Video"


def validate_name(name):
    if not isinstance(name, str) or not name or name != safe_text(name):
        raise ValueError("Use a name without path separators, control characters or leading/trailing spaces.")
    if len(name.encode("utf-8")) > MAX_NAME_BYTES:
        raise ValueError(f"The name is too long (maximum {MAX_NAME_BYTES} UTF-8 bytes).")
    if name.split(".", 1)[0].upper() in _RESERVED:
        raise ValueError("Choose a name that is not reserved by a filesystem.")
    return name


def identifier(media_id, url):
    value = str(media_id or "")
    if re.fullmatch(r"[A-Za-z0-9_-]{1,48}", value):
        return value
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]


def output_stem(name, media_id="", url="", include_id=True):
    validate_name(name)
    return name + (f" [{identifier(media_id, url)}]" if include_id else "")


def validate_stem(stem):
    if not isinstance(stem, str) or not stem or stem != safe_text(stem) or len(stem.encode("utf-8")) > MAX_STEM_BYTES:
        raise ValueError("Invalid saved output filename")
    return stem


def available_stem(base, existing, reserved=(), *, suffixes=None):
    occupied = {unicodedata.normalize("NFC", name).casefold() for name in existing}
    reservations = {unicodedata.normalize("NFC", name).casefold() for name in reserved}
    for index in range(1, 10001):
        suffix = f" ({index})" if index > 1 else ""
        candidate = clip_bytes(base, MAX_STEM_BYTES - len(suffix)) + suffix
        key = candidate.casefold()
        collision = (any(name == key or name.startswith(key + ".") for name in occupied)
                     if suffixes is None else any(
                         unicodedata.normalize("NFC", candidate + ending).casefold() in occupied
                         for ending in suffixes))
        if key not in reservations and not collision:
            return candidate
    raise ValueError("Too many files have this name. Choose a different name.")
