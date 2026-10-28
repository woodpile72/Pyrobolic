# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""History retention selects records only; it never opens or removes their files."""
import math
import time

RETENTION_DAYS = [0, 7, 30, 90, 365]
RETENTION_LABELS = ["Forever", "7 Days", "30 Days", "90 Days", "1 Year"]
RETENTION_SECONDS = [0, -1, 3600, 7200, 43200, 86400, 604800, 2592000, 7776000, 31536000]
CLEAR_LABELS = ["Never", "Immediately", "1 Hour", "2 Hours", "12 Hours", "1 Day", "7 Days", "30 Days", "90 Days", "1 Year"]


def validate_clear_interval(seconds):
    if seconds is not None and (type(seconds) is not int or seconds not in RETENTION_SECONDS):
        raise ValueError("Invalid completed History auto-clear interval")


def retention_seconds(settings):
    return settings.history_retention_days * 86400 if settings.history_retention_seconds is None else settings.history_retention_seconds


def validate_retention(days):
    if type(days) is not int or days not in RETENTION_DAYS:
        raise ValueError("Invalid completed History retention")


def expired_completed(jobs, days, running=(), *, now=None):
    validate_retention(days)
    return expired_completed_seconds(jobs, days * 86400, running, now=now)


def expired_completed_seconds(jobs, seconds, running=(), *, now=None):
    validate_clear_interval(seconds)
    if seconds is None or seconds == 0:
        return set()
    cutoff = (time.time() if now is None else now) - seconds
    identifiers = set()
    for job in jobs:
        if job.status != "Completed" or job.id in running:
            continue
        timestamp = job.completed_at if job.completed_at is not None else job.created
        if seconds == -1 or (type(timestamp) in (int, float) and math.isfinite(timestamp) and timestamp < cutoff):
            identifiers.add(job.id)
    return identifiers
