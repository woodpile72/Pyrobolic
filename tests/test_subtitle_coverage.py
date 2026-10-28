# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
from dataclasses import replace
import unittest

from pyrobolic.core import Settings
from pyrobolic.preferences import subtitle_coverage


def item(*tracks, loaded=True):
    return {"subtitles_loaded": loaded, "subtitle_tracks": [
        {"code": code, "name": code, "source": source} for code, source in tracks]}


class SubtitleCoverageTests(unittest.TestCase):
    def setUp(self):
        self.options = Settings(subtitle_mode="sidecar", subtitle_languages="en,es")
        self.items = [item(("en-US", "manual"), ("en-GB", "manual"), ("es", "generated")),
                      item(("es", "manual")), item(loaded=False)]

    def test_regional_variants_count_once_and_unknown_is_not_missing(self):
        report = subtitle_coverage(self.items, self.options)
        self.assertIn("en: reported on 1 of 2 checked items; missing on 1.", report)
        self.assertIn("es: reported on 1 of 2 checked items; missing on 1.", report)
        self.assertIn("Unchecked items: 1. Availability unknown.", report)

    def test_source_policy_and_all_languages(self):
        report = subtitle_coverage(self.items, replace(self.options, subtitle_source="manual-auto"))
        self.assertIn("es: reported on 2 of 2 checked items.", report)
        report = subtitle_coverage(self.items, replace(self.options, subtitle_source="auto"))
        self.assertIn("en: reported on 0 of 2 checked items; missing on 2.", report)
        report = subtitle_coverage(self.items, replace(self.options, subtitle_source="auto", subtitle_language_scope="all"))
        self.assertIn("Subtitles reported on 1 of 2 checked items.", report)

    def test_unknown_catalog_does_not_make_partial_reports_authoritative(self):
        report = subtitle_coverage([item(("en", "manual"), loaded=False)], self.options)
        self.assertIn("0 of 0 checked items.", report)
        self.assertNotIn("missing on", report)

    def test_off_invalid_draft_and_case_insensitive_matching(self):
        self.assertEqual(subtitle_coverage(self.items, replace(self.options, subtitle_mode="off")), "")
        draft = replace(self.options)
        draft.subtitle_languages = "invalid*"
        self.assertEqual(subtitle_coverage(self.items, draft), "")
        report = subtitle_coverage(self.items, replace(self.options, subtitle_languages="EN"))
        self.assertIn("EN: reported on 1 of 2 checked items", report)
