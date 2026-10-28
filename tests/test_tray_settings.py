# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from pyrobolic.core import Settings, Store, Queue


class TraySettingsTests(unittest.TestCase):
    def test_defaults_validation_and_persistence(self):
        self.assertFalse(Settings().background_mode)
        self.assertEqual(Settings().tray_appearance, "symbolic")
        for fields in ({"background_mode": "yes"}, {"tray_appearance": "other"}):
            with self.assertRaises(ValueError):
                Settings(**fields)
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            queue = Queue(store)
            queue.configure(replace(store.settings, background_mode=True, tray_appearance="color"))
            restored = Store(Path(directory)).settings
            self.assertTrue(restored.background_mode)
            self.assertEqual(restored.tray_appearance, "color")
            with patch.object(store, "save", side_effect=OSError("full")), self.assertRaises(OSError):
                queue.configure(replace(store.settings, background_mode=False))
            self.assertTrue(store.settings.background_mode)

    def test_symbolic_asset_is_flat_and_untiled(self):
        path = Path(__file__).resolve().parents[1] / "pyrobolic/icons/hicolor/scalable/apps/io.github.woodpile72.Pyrobolic-tray-symbolic.svg"
        root = ET.parse(path).getroot()
        ns = {"svg": "http://www.w3.org/2000/svg"}
        self.assertFalse(root.findall(".//svg:linearGradient", ns))
        self.assertFalse(root.findall(".//svg:filter", ns))
        self.assertFalse(root.findall("./svg:rect", ns))
        paths = root.findall(".//svg:path", ns)
        self.assertEqual(len(paths), 3)
        self.assertEqual({path.get("fill") for path in paths}, {"#2e3436"})
