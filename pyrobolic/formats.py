# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Small, URL-free source-format catalog and exact yt-dlp selectors."""

import math
import re


def validate_format_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value):
        raise ValueError("Invalid source format identifier")
    return value


def exact_selector(primary, audio=None):
    if primary is None:
        if audio is not None:
            raise ValueError("An audio stream requires a selected source format")
        return None
    # Filtering avoids interpreting IDs such as 'best' or 'mp4' as yt-dlp selectors.
    result = f'best*[format_id="{validate_format_id(primary)}"]'
    if audio is not None:
        if audio == primary:
            raise ValueError("A source stream cannot be selected twice")
        result += f'+best*[format_id="{validate_format_id(audio)}"]'
    return result


def catalog(info):
    """Keep engine order (worst to best), without signed URLs, headers or fragments."""
    result, seen = [], set()
    raw = info.get("formats")
    if not isinstance(raw, list):
        raw = [info]
    for source in raw:
        if not isinstance(source, dict) or source.get("has_drm"):
            continue
        try:
            identifier = validate_format_id(source.get("format_id"))
        except ValueError:
            continue
        video, audio = source.get("vcodec"), source.get("acodec")
        if (video == "none" and audio == "none") or source.get("ext") in {"mhtml", "jpg", "png"}:
            continue
        if identifier in seen:
            continue
        seen.add(identifier)
        kind = ("audio" if video == "none" else "video" if audio == "none" and video else
                "combined" if video and audio and audio != "none" else "unknown")
        item = {"id": identifier, "kind": kind}
        for key in ("ext", "vcodec", "acodec", "language", "format_note", "dynamic_range"):
            value = source.get(key)
            if isinstance(value, str):
                item[key] = " ".join(value.split())[:100]
        for key in ("width", "height", "fps", "abr", "tbr", "audio_channels", "filesize", "filesize_approx"):
            value = source.get(key)
            if type(value) in (int, float) and math.isfinite(value) and value > 0:
                item[key] = value
        result.append(item)
    return result


def format_label(item):
    parts = []
    if item.get("width") and item.get("height"):
        parts.append(f"{item['width']:g}x{item['height']:g}")
    elif item.get("height"):
        parts.append(f"{item['height']:g}p")
    if item.get("fps"):
        parts.append(f"{item['fps']:g} fps")
    for key in ("vcodec", "acodec"):
        if item.get(key) and item[key] != "none":
            parts.append(item[key])
    if item.get("abr"):
        parts.append(f"{item['abr']:g} kb/s audio")
    elif item.get("tbr"):
        parts.append(f"{item['tbr']:g} kb/s")
    for key in ("ext", "language", "dynamic_range", "format_note"):
        if item.get(key) and item[key] != "SDR":
            parts.append(item[key])
    if item.get("audio_channels"):
        parts.append(f"{item['audio_channels']:g} channels")
    size = item.get("filesize") or item.get("filesize_approx")
    if size:
        parts.append(("~" if not item.get("filesize") else "") + f"{size / 1048576:.1f} MiB")
    if item["kind"] == "unknown":
        parts.append("Stream details unknown")
    parts.append("ID " + item["id"])
    return " · ".join(parts)


def source_choices(formats, mode):
    kinds = {"audio", "combined", "unknown"} if mode == "audio" else {"video", "combined", "unknown"}
    return [item for item in reversed(formats) if item["kind"] in kinds]


def make_choice(formats, mode, primary=None, audio=None):
    if primary is None:
        exact_selector(primary, audio)
        return None
    exact_selector(primary, audio)
    selected = next((f for f in source_choices(formats, mode) if f["id"] == primary), None)
    if selected is None:
        raise ValueError("The selected source format is no longer available")
    summary = format_label(selected)
    if audio is not None:
        extra = next((f for f in formats if f["id"] == audio and f["kind"] == "audio"), None)
        if mode != "video" or selected["kind"] != "video" or extra is None:
            raise ValueError("Select a separate audio stream only with a video-only source")
        summary += " + " + format_label(extra)
    elif mode == "video" and selected["kind"] == "video":
        summary += " · No audio"
    return {"mode": mode, "primary": primary, "audio": audio, "summary": summary}


def output_extension(mode, audio_format, container):
    value = audio_format if mode == "audio" else container
    return None if value in {"best", "auto"} else "ogg" if value == "vorbis" else value


def remux_problem(formats, choice, container):
    """Reject only known WebM incompatibilities; unknown codecs remain engine-validated."""
    if container != "webm" or not choice:
        return ""
    primary = next((f for f in formats if f["id"] == choice["primary"]), {})
    audio = next((f for f in formats if f["id"] == choice["audio"]), primary)
    video_codec, audio_codec = primary.get("vcodec"), audio.get("acodec")
    if ((video_codec and video_codec != "none" and not video_codec.startswith(("vp8", "vp9", "vp09", "av1", "av01"))) or
            (audio_codec and audio_codec != "none" and not audio_codec.startswith(("opus", "vorbis")))):
        return "The selected codecs cannot be remuxed into WebM. Choose compatible streams, another container, or Convert When Needed."
    return ""
