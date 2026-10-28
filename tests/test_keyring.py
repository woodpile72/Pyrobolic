# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.environ.get("PYROBOLIC_INTEGRATION") == "1" and shutil.which("gnome-keyring-daemon"),
                     "Requires opt-in integration tests and gnome-keyring")
class KeyringTests(unittest.TestCase):
    def test_isolated_secret_service(self):
        tests = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory(prefix="pyrobolic-keyring-") as directory:
            root = Path(directory)
            env = dict(os.environ)
            for name in ("DBUS_SESSION_BUS_ADDRESS", "GNOME_KEYRING_CONTROL", "DISPLAY", "WAYLAND_DISPLAY"):
                env.pop(name, None)
            env["HOME"] = str(root)
            for name, child in (("XDG_DATA_HOME", "data"), ("XDG_CONFIG_HOME", "config"),
                                ("XDG_CACHE_HOME", "cache"), ("XDG_RUNTIME_DIR", "runtime")):
                (root / child).mkdir(mode=0o700)
                env[name] = str(root / child)
            result = subprocess.run(["dbus-run-session", "--config-file=" + str(tests / "session.conf"),
                                     "--", "/usr/bin/python3", str(tests / "keyring_fixture.py"), str(root)],
                                    env=env, capture_output=True, text=True, timeout=35)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
