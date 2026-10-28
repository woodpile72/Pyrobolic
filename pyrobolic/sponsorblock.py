# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Opt-in SponsorBlock policy and verification before output publication."""
import json
import math

MODES = ["off", "mark", "remove"]
LABELS = ["Off", "Mark as Chapters", "Remove Segments"]
CATEGORIES = {"sponsor": "Sponsor Segments", "intro": "Intros", "outro": "Credits and Outros",
              "selfpromo": "Self Promotion", "preview": "Previews and Recaps", "filler": "Filler",
              "interaction": "Interaction Reminders", "music_offtopic": "Non-Music Sections",
              "hook": "Greetings and Hooks"}


def validate(options):
    if options.sponsorblock not in MODES:
        raise ValueError("Invalid SponsorBlock mode")
    categories = options.sponsorblock_categories
    if not isinstance(categories, str) or not categories or any(c not in CATEGORIES for c in categories.split(",")):
        raise ValueError("Select at least one supported SponsorBlock category")
    if options.sponsorblock == "off":
        return
    from .preferences import subtitle_mode
    if getattr(options, "clip_start", 0) or getattr(options, "clip_end", None) is not None or options.split_chapters:
        raise ValueError("SponsorBlock cannot currently be combined with time ranges or chapter splitting.")
    if getattr(options, "auth_ref", None):
        raise ValueError("SponsorBlock is not available for authenticated downloads.")
    if options.sponsorblock == "remove" and subtitle_mode(options) != "off":
        raise ValueError("Turn off subtitles before removing SponsorBlock segments; subtitle retiming is not supported.")
    if options.sponsorblock == "mark" and options.chapter_policy == "omit":
        raise ValueError("Allow chapter markers before enabling SponsorBlock chapter marking.")
    if options.sponsorblock == "mark" and getattr(options, "mode", "video") == "audio":
        raise ValueError("SponsorBlock chapter marking currently requires video output.")


def arguments(options):
    validate(options)
    if options.sponsorblock == "off":
        return ["--no-sponsorblock"]
    if getattr(options, "output_stem", None) is None:
        raise ValueError("SponsorBlock requires a new download with private staging.")
    args = ["--sponsorblock-" + options.sponsorblock, options.sponsorblock_categories,
            "--print", "before_dl:PYRO_SPONSOR:%(.{duration,sponsorblock_chapters})j"]
    if options.sponsorblock == "mark":
        args += ["--embed-chapters", "--sponsorblock-chapter-title", "SponsorBlock: %(category)s"]
    elif options.mode == "video":
        args += ["--force-keyframes-at-cuts"]
    return args


def summary(options):
    return "SponsorBlock: " + LABELS[MODES.index(options.sponsorblock)]


def intervals(report):
    if not isinstance(report, dict) or not isinstance(report.get("sponsorblock_chapters"), list):
        raise RuntimeError("SponsorBlock did not report segment data; processing could not be verified.")
    result = []
    for chapter in report["sponsorblock_chapters"]:
        start, end = chapter.get("start_time"), chapter.get("end_time")
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in (start, end)) or not 0 <= start < end:
            raise RuntimeError("SponsorBlock returned invalid segment timing")
        result.append((start, end))
    merged = []
    for start, end in sorted(result):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def verify(options, report, files, run):
    if options.sponsorblock == "off":
        return []
    segments = intervals(report)
    if not segments:
        return ["SponsorBlock found no matching segments; no segments were marked or removed."]
    for media in files:
        data = json.loads(run(["ffprobe", "-v", "error", "-show_format", "-show_chapters", "-of", "json", media], 30))
        if options.sponsorblock == "remove":
            original = report.get("duration")
            if type(original) not in (int, float) or not math.isfinite(original) or original <= 0:
                raise RuntimeError("SponsorBlock removal cannot be verified without the original duration")
            removed = sum(end - start for start, end in segments)
            actual = float(data.get("format", {}).get("duration", "nan"))
            tolerance = min(.75, removed / 3)
            if not math.isfinite(actual) or abs(actual - (original - removed)) > tolerance:
                raise RuntimeError("SponsorBlock removal did not produce the expected duration. The file was not published.")
        else:
            chapters = [c for c in data.get("chapters", []) if c.get("tags", {}).get("title", "").startswith("SponsorBlock:")]
            for start, end in segments:
                covered = sorted((float(c["start_time"]), float(c["end_time"])) for c in chapters)
                cursor = start
                for left, right in covered:
                    if left <= cursor + .1 and right > cursor:
                        cursor = right
                if cursor < end - .1:
                    raise RuntimeError("SponsorBlock chapter timing could not be verified. The file was not published.")
    return []
