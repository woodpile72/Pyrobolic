# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Portable, per-job source preferences and subtitle policy."""

from dataclasses import dataclass, fields
import re
from .transfer import TransferPreferences, validate_transfer


VIDEO_CODECS = ["any", "h264", "h265", "vp9", "av1"]
AUDIO_CODECS = ["any", "aac", "opus", "mp3", "vorbis"]
FRAME_RATES = [0, 24, 30, 60]
AUDIO_RATES = [0, 64, 128, 192, 256, 320]
AUDIO_ENCODING_QUALITIES = ["0", "128K", "192K", "256K", "320K"]
SUBTITLE_MODES = ["off", "sidecar", "embed", "both"]
SUBTITLE_SOURCES = ["manual", "manual-auto", "auto"]
SUBTITLE_LANGUAGE_SCOPES = ["selected", "all"]
SUBTITLE_FORMATS = ["best", "srt", "ass", "vtt", "lrc"]
CODEC_PREFIXES = {"h264": ("h264", "avc"), "h265": ("h265", "hev", "hvc", "hevc"),
                  "vp9": ("vp9", "vp09"), "av1": ("av1", "av01"),
                  "aac": ("aac", "mp4a"), "opus": ("opus",), "mp3": ("mp3",), "vorbis": ("vorbis",)}


@dataclass(kw_only=True)
class MediaPreferences(TransferPreferences):
    sponsorblock: str = "off"
    sponsorblock_categories: str = "sponsor"
    upscaled_sources: str = "allow"
    preferred_video_codec: str = "any"
    preferred_audio_codec: str = "any"
    preferred_fps: int = 0
    preferred_abr: int = 0
    source_quality: str = "best"
    audio_encoding_quality: str = "0"
    video_encoder: str = "default"
    video_encoding_quality: str = "default"
    video_encoding_threads: int = 0
    reencode_video: bool = False
    subtitle_mode: str = "legacy"
    subtitle_languages: str = "en"
    subtitle_source: str = "manual"
    subtitle_format: str = "best"
    subtitle_missing: str = "warn"
    subtitle_language_scope: str = "selected"
    chapter_policy: str = "metadata"
    source_info_policy: str = "metadata"
    artwork_mode: str = "off"
    square_audio_artwork: bool = False
    split_chapters: bool = False
    chapter_titles: bool = False
    precise_chapters: bool = False
    save_description: bool = False


def preference_values(options):
    return {f.name: getattr(options, f.name) for f in fields(MediaPreferences)}


def language_codes(value):
    if not isinstance(value, str) or len(value) > 200:
        raise ValueError("Enter comma-separated language codes, such as en, es, de.")
    codes = list(dict.fromkeys(part.strip() for part in value.split(",")))
    if not codes or any(not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", code) for code in codes):
        raise ValueError("Enter comma-separated language codes, such as en, es, de.")
    return codes


def validate_preferences(options):
    validate_transfer(options)
    from .sponsorblock import validate
    validate(options)
    if any(type(getattr(options, field)) is not bool for field in ("split_chapters", "chapter_titles", "precise_chapters", "square_audio_artwork", "save_description", "reencode_video")):
        raise ValueError("Invalid media processing options")
    if type(options.video_encoding_threads) is not int or not 0 <= options.video_encoding_threads <= 64:
        raise ValueError("Video encoder threads must be from 0 (automatic) to 64")
    from .encoding import ENCODERS, QUALITIES
    choices = {"preferred_video_codec": VIDEO_CODECS, "preferred_audio_codec": AUDIO_CODECS,
               "chapter_policy": ["metadata", "embed", "omit"],
               "source_info_policy": ["metadata", "embed", "omit"],
               "artwork_mode": ["off", "save", "embed", "both"],
               "video_encoder": ENCODERS, "video_encoding_quality": QUALITIES,
               "upscaled_sources": ["allow", "exclude"],
               "audio_encoding_quality": AUDIO_ENCODING_QUALITIES,
               "source_quality": ["best", "smallest"], "subtitle_mode": ["legacy"] + SUBTITLE_MODES,
               "subtitle_source": SUBTITLE_SOURCES, "subtitle_format": SUBTITLE_FORMATS,
               "subtitle_language_scope": SUBTITLE_LANGUAGE_SCOPES,
               "subtitle_missing": ["warn", "fail"]}
    for key, allowed in choices.items():
        if getattr(options, key) not in allowed:
            raise ValueError(f"Invalid {key.replace('_', ' ')}")
    for key, allowed in (("preferred_fps", FRAME_RATES), ("preferred_abr", AUDIO_RATES)):
        if type(getattr(options, key)) is not int or getattr(options, key) not in allowed:
            raise ValueError(f"Invalid {key.replace('_', ' ')}")
    if not isinstance(options.subtitle_languages, str) or len(options.subtitle_languages) > 200:
        raise ValueError("Invalid subtitle language codes")
    if options.subtitle_language_scope == "selected":
        language_codes(options.subtitle_languages)


def subtitle_mode(options):
    if options.subtitle_mode == "legacy":
        return "both" if options.subtitles and options.mode == "video" else "off"
    return options.subtitle_mode


def preference_sort(options):
    if options.source_format is not None:
        return []
    order = []
    if options.mode == "video" and options.preferred_video_codec != "any":
        order.append("vcodec:" + options.preferred_video_codec)
    if options.preferred_audio_codec != "any":
        order.append("acodec:" + options.preferred_audio_codec)
    if options.mode == "video" and options.preferred_fps:
        order.append(f"fps:{options.preferred_fps}")
    if options.preferred_abr:
        order.append(f"abr:{options.preferred_abr}")
    if options.source_quality == "smallest":
        order.extend(["+res", "+br"] if options.mode == "video" else ["+abr"])
    return ["--format-sort", ",".join(order)] if order else []


def source_variant_filter(options):
    if options.source_format is not None or options.mode != "video" or options.upscaled_sources == "allow":
        return ""
    # Missing notes remain eligible. This filters reported variants, not image content.
    return r'[format_note!~=?"(?i)\b(?:AI[- ]upscaled|super[- ]resolution)\b"]'


def source_feedback(options, selected):
    """Compare actual engine selection, never guess availability from a cached catalog."""
    warnings = []
    if options.source_format is not None:
        return warnings
    for key, requested in (("vcodec", options.preferred_video_codec if options.mode == "video" else "any"),
                           ("acodec", options.preferred_audio_codec)):
        actual = selected.get(key)
        if requested != "any" and (not isinstance(actual, str) or not actual.casefold().startswith(CODEC_PREFIXES[requested])):
            kind = "video codec" if key == "vcodec" else "audio codec"
            warnings.append(f"Preferred {requested.upper()} was not selected; source {kind} is {actual or 'unknown'}. "
                            "Source availability and other selection constraints take precedence.")
    for key, requested in (("fps", options.preferred_fps if options.mode == "video" else 0),
                           ("abr", options.preferred_abr)):
        actual = selected.get(key)
        if requested and (type(actual) not in (int, float) or actual > requested + 0.1):
            name, unit = ("frame rate", "fps") if key == "fps" else ("audio bitrate", "kb/s")
            warnings.append(f"Preferred {name} up to {requested} {unit} was not met or could not be verified; "
                            f"selected value: {actual or 'unknown'}.")
    return warnings


def subtitle_arguments(options):
    mode = subtitle_mode(options)
    if mode == "off":
        return []
    languages = ("all,-live_chat" if options.subtitle_language_scope == "all" else
                 ",".join("(?i:" + re.escape(code) + r"(?:-.*)?)" for code in language_codes(options.subtitle_languages)))
    args = ["--sub-langs", languages, "--sub-format", "best"]
    # Keep sidecars until verification, even for embed-only output.
    args += ["--no-write-subs" if options.subtitle_source == "auto" else "--write-subs"]
    args += ["--write-auto-subs" if options.subtitle_source != "manual" else "--no-write-auto-subs"]
    if options.subtitle_format != "best":
        args += ["--convert-subs", options.subtitle_format]
    if mode in {"embed", "both"}:
        if options.subtitle_source == "auto":
            # The normal embed flag deletes auto-only sidecars. Keep them for verification;
            # after_move also ensures remux/conversion has finished before embedding.
            args += ["--use-postprocessor", "FFmpegEmbedSubtitle:already_have_subtitle=True;when=after_move"]
        else:
            args.append("--embed-subs")
    # Capture intent before yt-dlp can remove a subtitle whose fetch failed.
    args += ["--print", "video:PYRO_SUBTITLES:%(requested_subtitles)j"]
    args += ["--print", "after_move:PYRO_SUBFILES:%(requested_subtitles.:.filepath)j"]
    return args


def subtitle_feedback(options, selected_languages):
    if options.subtitle_language_scope == "all":
        return "No subtitles available for the selected subtitle sources." if not selected_languages else ""
    missing = [code for code in language_codes(options.subtitle_languages)
               if not any(lang.casefold() == code.casefold() or lang.casefold().startswith(code.casefold() + "-")
                          for lang in selected_languages)]
    return "Requested subtitles unavailable: " + ", ".join(missing) if missing else ""


def preference_summary(options):
    parts = []
    if options.upscaled_sources == "exclude":
        parts.append("Exclude Reported AI Upscales")
    if options.source_quality == "smallest":
        parts.append("Lower resolution/bitrate")
    if options.preferred_video_codec != "any":
        parts.append(options.preferred_video_codec.upper())
    if options.preferred_audio_codec != "any":
        parts.append(options.preferred_audio_codec.upper())
    if options.preferred_fps:
        parts.append(f"Prefer up to {options.preferred_fps} fps")
    if options.preferred_abr:
        parts.append(f"Prefer up to {options.preferred_abr} kb/s")
    return " · ".join(parts) or "Best Available"


def subtitle_summary(options):
    mode = subtitle_mode(options)
    if mode == "off":
        return "Off"
    output = {"sidecar": "Sidecar Files", "embed": "Embedded Only", "both": "Embedded and Sidecar Files"}[mode]
    source = {"manual": "Manual Only", "manual-auto": "Manual or Generated", "auto": "Auto-Generated Only"}[options.subtitle_source]
    format_name = {"best": "Original", "vtt": "WebVTT"}.get(options.subtitle_format, options.subtitle_format.upper())
    languages = "All Available" if options.subtitle_language_scope == "all" else options.subtitle_languages
    return f"{languages} · {output} · {source} · {format_name}"


def subtitle_catalog(items, source):
    """Aggregate known availability; unknown playlist entries are not missing tracks."""
    tracks = {}
    known = 0
    for item in items:
        if item.get("subtitles_loaded", item.get("formats_loaded", False)):
            known += 1
        seen = set()
        for track in item.get("subtitle_tracks", []):
            if (source == "manual" and track["source"] != "manual") or (source == "auto" and track["source"] != "generated"):
                continue
            try:
                code = language_codes(track["code"])[0]
            except ValueError:
                continue
            value = tracks.setdefault(code, {"name": track["name"], "sources": set(), "count": 0})
            value["sources"].add(track["source"])
            if code not in seen:
                value["count"] += 1
                seen.add(code)
    return tracks, known


def subtitle_coverage(items, options):
    """Describe inspected entries only; download-time verification remains authoritative."""
    if subtitle_mode(options) == "off":
        return ""
    checked = [item for item in items if item.get("subtitles_loaded", item.get("formats_loaded", False))]
    unknown = len(items) - len(checked)
    catalogs = [subtitle_catalog([item], options.subtitle_source)[0] for item in checked]
    if options.subtitle_language_scope == "all":
        available = sum(bool(catalog) for catalog in catalogs)
        lines = [f"Subtitles reported on {available} of {len(checked)} checked items."]
    else:
        try:
            requested = language_codes(options.subtitle_languages)
        except ValueError:
            return ""
        lines = []
        for code in requested:
            matched = sum(any(lang.casefold() == code.casefold() or
                              lang.casefold().startswith(code.casefold() + "-") for lang in catalog)
                          for catalog in catalogs)
            missing = len(checked) - matched
            lines.append(f"{code}: reported on {matched} of {len(checked)} checked items" +
                         (f"; missing on {missing}." if missing else "."))
    if unknown:
        lines.append(f"Unchecked items: {unknown}. Availability unknown.")
    return "\n".join(lines)
