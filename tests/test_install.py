# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods

import configparser
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


class InstallationTests(unittest.TestCase):
    def test_notification_desktop_and_activation_registration(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run([sys.executable, str(root / "install.py")],
                           env=dict(os.environ, XDG_DATA_HOME=directory), check=True, capture_output=True)
            desktop = configparser.ConfigParser(interpolation=None)
            desktop.read(Path(directory) / "applications/io.github.woodpile72.Pyrobolic.desktop")
            self.assertTrue(desktop["Desktop Entry"].getboolean("DBusActivatable"))
            self.assertTrue(desktop["Desktop Entry"].getboolean("X-GNOME-UsesNotifications"))
            service = configparser.ConfigParser(interpolation=None)
            service.read(Path(directory) / "dbus-1/services/io.github.woodpile72.Pyrobolic.service")
            self.assertEqual(service["D-BUS Service"]["Name"], "io.github.woodpile72.Pyrobolic")
            self.assertEqual(shlex.split(service["D-BUS Service"]["Exec"]),
                             [str(root / "pyrobolic-launcher"), "--gapplication-service"])
