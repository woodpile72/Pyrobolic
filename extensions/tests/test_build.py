# SPDX-License-Identifier: GPL-3.0-only
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("extension_build", ROOT / "extensions/build.py")
ext = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ext)


class ExtensionBuildTests(unittest.TestCase):
    def test_submission_archive_has_only_declared_addon_sources(self):
        from package_deb import release_files
        names = release_files(ROOT, "source")
        with tempfile.TemporaryDirectory() as directory:
            first = ext.build_firefox(ROOT, names, Path(directory) / "a.xpi")
            second = ext.build_firefox(ROOT, names, Path(directory) / "b.xpi")
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                self.assertEqual(archive.read("manifest.json"), (ROOT / "extensions/firefox/addon/manifest.json").read_bytes())
                self.assertTrue(all(not name.endswith((".py", ".pyc", ".log")) for name in archive.namelist()))

    def test_signature_metadata_and_source_match_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            addon = root / ext.ADDON
            addon.mkdir(parents=True)
            (addon / "manifest.json").write_text('{"version":"1.0.0"}')
            names = [ext.ADDON + "manifest.json", ext.SIGNED]
            signed = root / ext.SIGNED
            def fixture(payload, metadata):
                with zipfile.ZipFile(signed, "w") as archive:
                    archive.writestr("manifest.json", payload)
                    if metadata:
                        # Structural fixture only; never a genuine signature or released artifact.
                        archive.writestr("META-INF/mozilla.rsa", "test fixture")
            fixture((addon / "manifest.json").read_bytes(), False)
            with self.assertRaisesRegex(ValueError, "signature metadata"):
                ext.validate_signed_payload(root, names)
            fixture(b'{"version":"wrong"}', True)
            with self.assertRaisesRegex(ValueError, "does not match"):
                ext.validate_signed_payload(root, names)
            with self.assertRaisesRegex(ValueError, "required"):
                ext.validate_signed_payload(root, names[:-1])

    def test_combined_package_replaces_preview_in_private_root(self):
        from package_deb import build
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy"
            (legacy / "DEBIAN").mkdir(parents=True)
            (legacy / "DEBIAN/control").write_text("Package: pyrobolic-external-plugins\nVersion: 0.1.0\nArchitecture: all\nMaintainer: Fixture <fixture@example.org>\nDescription: Preview migration fixture\n")
            old_manifest = legacy / "usr/lib/mozilla/native-messaging-hosts/io.github.woodpile72.pyrobolic.json"
            old_manifest.parent.mkdir(parents=True)
            old_manifest.write_text('{"path":"/old/bridge.py"}')
            old_extra = legacy / "usr/lib/pyrobolic-external-plugins/bridge.py"
            old_extra.parent.mkdir(parents=True)
            old_extra.write_text("old fixture")
            old_deb = root / "legacy.deb"
            subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(legacy), str(old_deb)], check=True, capture_output=True)
            new_deb = build(root / "combined.deb")
            private = root / "install"
            admin = private / "var/lib/dpkg"
            admin.mkdir(parents=True)
            (admin / "status").touch()
            personal = private / "home/test/.mozilla/profile/prefs.js"
            personal.parent.mkdir(parents=True)
            personal.write_text("preserve browser settings")
            cmd = ["dpkg", f"--root={private}", "--force-not-root", "--force-depends"]
            subprocess.run(cmd + ["--install", str(old_deb)], check=True, capture_output=True)
            subprocess.run(cmd + ["--install", str(new_deb)], check=True, capture_output=True)
            self.assertFalse((private / old_extra.relative_to(legacy)).exists())
            installed = json.loads((private / old_manifest.relative_to(legacy)).read_text())
            self.assertEqual(installed["path"], "/usr/lib/pyrobolic/extensions/firefox/host.py")
            self.assertEqual(personal.read_text(), "preserve browser settings")
            subprocess.run(cmd + ["--remove", "pyrobolic"], check=True, capture_output=True)
            self.assertFalse((private / old_manifest.relative_to(legacy)).exists())
            self.assertEqual(personal.read_text(), "preserve browser settings")


if __name__ == "__main__":
    unittest.main()
