# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from package_deb import build


class PackagingTests(unittest.TestCase):
    def test_base_version_without_development_suffix(self):
        from pyrobolic import __version__
        with tempfile.TemporaryDirectory() as directory:
            package = build(Path(directory) / "base.deb", revision=None)
            version = subprocess.check_output(["dpkg-deb", "-f", str(package), "Version"], text=True).strip()
            self.assertEqual(version, __version__)

    def test_reproducible_build_install_upgrade_remove_in_private_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = build(root / "first.deb"), build(root / "second.deb")
            self.assertEqual(hashlib.sha256(first.read_bytes()).digest(), hashlib.sha256(second.read_bytes()).digest())
            with self.assertRaises(FileExistsError):
                build(first)
            unpacked = root / "unpacked"
            subprocess.run(["dpkg-deb", "-x", str(first), str(unpacked)], check=True, capture_output=True)
            self.assertTrue((unpacked / "usr/bin/pyrobolic").stat().st_mode & 0o111)
            self.assertFalse(list(unpacked.rglob("*.pyc")))
            docs = unpacked / "usr/share/doc/pyrobolic"
            self.assertFalse(list(unpacked.rglob("*.md")))
            self.assertTrue((docs / "NOTICE").is_file())
            self.assertTrue((docs / "LICENSE").is_file())
            subprocess.run(["/usr/bin/python3", "-c", "from pyrobolic.core import Settings; assert Settings().concurrency == 2"],
                           cwd=root, env=dict(os.environ, PYTHONPATH=str(unpacked / "usr/lib/pyrobolic")), check=True)
            control = subprocess.check_output(["dpkg-deb", "-f", str(first)], text=True)
            self.assertIn("gir1.2-adw-1 (>= 1.5)", control)
            private = root / "install"
            admin = private / "var/lib/dpkg"
            admin.mkdir(parents=True)
            (admin / "status").touch()
            user_data = private / "home/test/.local/share/pyrobolic/state.json"
            user_data.parent.mkdir(parents=True)
            user_data.write_bytes(b"keep personal state")
            command = ["dpkg", f"--root={private}", "--force-not-root", "--force-depends"]
            upgrade = build(root / "upgrade.deb", revision=2)
            for package in (first, upgrade):
                subprocess.run(command + ["--install", str(package)], check=True, capture_output=True)
                self.assertTrue((private / "usr/bin/pyrobolic").exists())
            subprocess.run(command + ["--remove", "pyrobolic"], check=True, capture_output=True)
            self.assertFalse((private / "usr/bin/pyrobolic").exists())
            self.assertEqual(user_data.read_bytes(), b"keep personal state")
