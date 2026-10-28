# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Named subtitle policies; jobs always keep independent value snapshots."""
from dataclasses import replace
import unicodedata

from .preferences import MediaPreferences, subtitle_mode, validate_preferences

SUBTITLE_FIELDS = ("subtitle_mode", "subtitle_languages", "subtitle_source", "subtitle_format",
                   "subtitle_missing", "subtitle_language_scope")
MAX_PRESETS = 32


def preset_name(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 64 or
            any(unicodedata.category(c).startswith("C") for c in value)):
        raise ValueError("Enter a preset name of 1 to 64 characters without control characters.")
    return value.strip()


def subtitle_values(options):
    values = {key: getattr(options, key) for key in SUBTITLE_FIELDS}
    values["subtitle_mode"] = subtitle_mode(options)
    validate_preferences(MediaPreferences(**values))
    return values


def validate_presets(presets):
    if not isinstance(presets, dict) or len(presets) > MAX_PRESETS:
        raise ValueError(f"At most {MAX_PRESETS} subtitle presets are supported.")
    names = set()
    for name, values in presets.items():
        if preset_name(name) != name or name.casefold() in names:
            raise ValueError("Subtitle preset names must be unique.")
        names.add(name.casefold())
        if not isinstance(values, dict) or set(values) != set(SUBTITLE_FIELDS):
            raise ValueError("Invalid saved subtitle preset fields.")
        if values["subtitle_mode"] == "legacy":
            raise ValueError("Subtitle presets must specify their output mode.")
        validate_preferences(MediaPreferences(**values))


def matching_name(presets, name):
    return next((key for key in presets if key.casefold() == name.casefold()), None)


def save_preset(presets, name, options, *, overwrite=False):
    validate_presets(presets)
    name = preset_name(name)
    existing = matching_name(presets, name)
    if existing is not None and not overwrite:
        raise ValueError("A subtitle preset with this name already exists.")
    result = {key: dict(value) for key, value in presets.items() if key != existing}
    result[name] = subtitle_values(options)
    validate_presets(result)
    return result


def load_preset(options, presets, name):
    validate_presets(presets)
    return replace(options, **presets[name])
