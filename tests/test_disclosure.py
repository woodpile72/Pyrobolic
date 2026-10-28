# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from pyrobolic.core import Settings, Store
from pyrobolic.disclosure import authentication_needed, download_summary


class DisclosureTests(unittest.TestCase):
    def test_default_summary_and_secret_free_overrides(self):
        settings = Settings(output="/tmp/Downloads")
        self.assertTrue(download_summary(settings).startswith("Best available · Downloads"))
        settings = replace(settings, proxy="http://secret.example:8080", rate_limit_kib=256,
                           container="mp4", video_processing="convert", reencode_video=True)
        result = download_summary(settings, [{"clip_start": 10, "clip_end": 20}], True)
        for text in ("Authentication on", "Proxy enabled", "256 KiB/s", "Time range", "Always re-encode"):
            self.assertIn(text, result)
        self.assertNotIn("secret.example", result)

    def test_authentication_offer_requires_specific_evidence(self):
        for message in ("HTTP Error 403: Forbidden", "Network timeout", "Unable to download metadata"):
            self.assertFalse(authentication_needed(message))
        for message in ("Sign in required", "Please log in", "Use --cookies", "Authentication was not accepted"):
            self.assertTrue(authentication_needed(message))

    def test_disclosures_persist_independently_of_media_preferences(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            store.settings = replace(store.settings, settings_expanded=True, download_expanded=False,
                                     quality=720, rate_limit_kib=256)
            store.save()
            settings = Store(store.directory).settings
            self.assertTrue(settings.settings_expanded)
            self.assertFalse(settings.download_expanded)
            self.assertEqual(settings.quality, 720)
            self.assertEqual(settings.rate_limit_kib, 256)
