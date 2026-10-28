# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Public summaries of effective download choices; never render credentials or proxy URLs."""
from pathlib import Path
from dataclasses import replace
import re

from .preferences import preference_summary, subtitle_mode, subtitle_summary
from .encoding import encoding_summary
from .ranges import range_summary


def authentication_needed(message):
    return bool(re.search(r"(?:authentication|login|sign[ -]?in) (?:is )?required|"
                          r"please (?:log in|sign in)|use --cookies|password.protected|"
                          r"authentication was not accepted", message, re.IGNORECASE))


def download_summary(options, items=(), authenticated=False):
    choices = [item.get("source_choices", {}).get(options.mode) for item in items]
    exact = sum(bool(choice) for choice in choices)
    if exact and exact == len(items):
        quality = "Exact source formats"
    elif options.source_quality == "smallest":
        quality = "Smaller source"
    elif options.mode == "video" and options.quality:
        quality = f"Up to {options.quality}p"
    else:
        quality = "Best available"
    parts = [quality, Path(options.output).name or options.output]
    if options.sponsorblock != "off":
        from .sponsorblock import summary
        parts.append(summary(options))
    if exact and exact != len(items):
        parts.append(f"{exact} exact source choices")
    if options.mode == "audio":
        parts.append("Original audio" if options.audio_format == "best" else options.audio_format.upper())
        if options.audio_format in {"mp3", "m4a", "opus", "vorbis"} and options.audio_encoding_quality != "0":
            parts.append("Audio conversion: " + options.audio_encoding_quality)
    elif options.container != "auto":
        if options.video_processing == "convert":
            parts.append(("Always re-encode to " if options.reencode_video else "Convert when needed to ") + options.container.upper())
            parts.append(encoding_summary(options))
        else:
            parts.append("Remux to " + options.container.upper())
    source_options = (replace(options, preferred_video_codec="any", preferred_fps=0,
                              upscaled_sources="allow") if options.mode == "audio" else options)
    source = preference_summary(source_options)
    if source != "Best Available" and (not items or exact != len(items)):
        parts.append(source)
    if subtitle_mode(options) != "off":
        parts.append("Subtitles: " + subtitle_summary(options))
    ranges = [item for item in items if item.get("clip_start", 0) or item.get("clip_end") is not None]
    if len(items) == 1 and ranges:
        item = ranges[0]
        parts.append("Time range: " + range_summary(item.get("clip_start", 0), item.get("clip_end"), item.get("precise_cuts", False)))
    elif ranges:
        parts.append(f"Time ranges: {len(ranges)} items")
    if authenticated:
        parts.append("Authentication on; source metadata off")
    else:
        if not options.metadata:
            parts.append("Metadata off")
        if options.chapter_policy != "metadata":
            parts.append("Add chapters" if options.chapter_policy == "embed" else "No chapters")
        if options.source_info_policy != "metadata":
            parts.append("Add source data" if options.source_info_policy == "embed" else "No source data")
        if options.split_chapters:
            parts.append("Split chapters" + (" (precise cuts)" if options.precise_chapters else ""))
        if options.save_description:
            parts.append("Save description")
        if options.artwork_mode != "off":
            parts.append("Artwork: " + options.artwork_mode)
    if options.direct_connection:
        parts.append("Direct connection")
    elif options.proxy:
        parts.append("Proxy enabled")
    if options.rate_limit_kib:
        parts.append(f"Rate limit: {options.rate_limit_kib:,} KiB/s")
    if options.fragment_concurrency != 1:
        parts.append(f"{options.fragment_concurrency} fragments")
    if not options.use_part_files:
        parts.append("Restart incomplete downloads")
    if options.browser_identity != "default":
        parts.append("Browser identity: " + options.browser_identity.capitalize())
    if any(item.get("destination") and item["destination"] != options.output for item in items):
        parts.append("Individual destinations")
    return " · ".join(parts)
