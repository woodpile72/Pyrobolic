# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Strict per-item time ranges, never inherited as defaults by another video."""
import math


def validate_range(start, end, precise):
    if type(start) not in (int, float) or not math.isfinite(start) or not 0 <= start <= 604800:
        raise ValueError("Start time must be between 0 and 604800 seconds.")
    if end is not None and (type(end) not in (int, float) or not math.isfinite(end) or not start < end <= 604800):
        raise ValueError("End time must be after the start time, up to 604800 seconds.")
    if type(precise) is not bool:
        raise ValueError("Invalid precise-cut setting")


def range_arguments(job):
    validate_range(job.clip_start, job.clip_end, job.precise_cuts)
    if not job.clip_start and job.clip_end is None:
        return []
    end = f"{job.clip_end:g}" if job.clip_end is not None else "inf"
    args = ["--download-sections", f"*{job.clip_start:g}-{end}"]
    if job.precise_cuts:
        args.append("--force-keyframes-at-cuts")
    return args


def range_summary(start, end, precise=False):
    if not start and end is None:
        return "Entire Media"
    interval = f"{start:g} s to {end:g} s" if end is not None else f"From {start:g} s"
    return interval + (" · Precise Cut" if precise else " · Keyframe Cut")
