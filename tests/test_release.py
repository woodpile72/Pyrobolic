# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
import hashlib
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from migrate_checkout import apply, conflicts, APP_ID
from package_deb import release_files
from release import snapshot, bundle, frozen_commit


class ReleaseTests(unittest.TestCase):
    def test_bundle_builds_extracted_snapshot_with_exact_version(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "release"
            def build_frozen(args, *, cwd, env, check):
                self.assertNotEqual(cwd, Path(__file__).resolve().parents[1])
                inventory = json.loads((cwd / "SOURCE-MANIFEST.json").read_text())
                for name, digest in inventory.items():
                    self.assertEqual(hashlib.sha256((cwd / name).read_bytes()).hexdigest(), digest)
                self.assertIn("--base-version", args)
                self.assertEqual(env["SOURCE_DATE_EPOCH"], "1793188800")
                Path(args[2]).write_bytes(b"package fixture")
            @contextmanager
            def fixture(*_args):
                yield Path(__file__).resolve().parents[1], "f" * 40, 1793188800
            with patch("release.subprocess.run", side_effect=build_frozen), \
                    patch("release.subprocess.check_output", return_value="fixture version"), \
                    patch("release.frozen_commit", fixture):
                package = bundle(destination, None, 1793188800, "Erick Woods <erick@erick.com>")
            self.assertEqual(package.read_bytes(), b"package fixture")
            self.assertEqual(json.loads((destination / "build-info.json").read_text())["source_commit"], "f" * 40)

    def test_commit_snapshot_ignores_worktree_and_rejects_markdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "release-files.json").write_text(json.dumps({"runtime": ["payload"], "source": ["release-files.json"]}))
            (root / "payload").write_text("committed")
            def git(*args):
                return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL)
            git("init", "-q")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.org", "commit", "-qm", "fixture")
            identifier = git("rev-parse", "HEAD").decode().strip()
            (root / "payload").write_text("uncommitted change")
            (root / "private.md").write_text("local notes")
            with frozen_commit(root, identifier) as (frozen, actual, epoch):
                self.assertEqual(actual, identifier)
                self.assertGreater(epoch, 0)
                self.assertEqual((frozen / "payload").read_text(), "committed")
                self.assertFalse((frozen / "private.md").exists())
            git("add", "private.md")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.org", "commit", "-qm", "bad input")
            with self.assertRaisesRegex(ValueError, "Markdown"), frozen_commit(root, "HEAD"):
                pass

    def test_source_snapshot_is_reproducible_and_excludes_debris(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / "one.tar.xz", Path(directory) / "two.tar.xz"
            snapshot(first, 1793188800)
            snapshot(second, 1793188800)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with tarfile.open(first) as archive:
                names = archive.getnames()
                self.assertTrue(all(not name.endswith((".pyc", ".deb", ".log")) and "/.qa/" not in name for name in names))
                inventory = json.load(archive.extractfile("pyrobolic/SOURCE-MANIFEST.json"))
                for name, digest in inventory.items():
                    self.assertEqual(hashlib.sha256(archive.extractfile("pyrobolic/" + name).read()).hexdigest(), digest)

    def test_manifest_rejects_symlinks_and_ignores_unlisted_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "release-files.json").write_text(json.dumps({"runtime": ["kept.py"]}))
            (root / "kept.py").write_text("pass")
            (root / "private.json").write_text("not a release input")
            self.assertEqual(release_files(root, "runtime"), ["kept.py"])
            (root / "kept.py").unlink()
            (root / "kept.py").symlink_to(root / "private.json")
            with self.assertRaises(ValueError):
                release_files(root, "runtime")

    def test_migration_is_explicit_and_preserves_state_with_restorable_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data, checkout, system = root / "data", root / "checkout", root / "system"
            desktop = data / f"applications/{APP_ID}.desktop"
            service = data / f"dbus-1/services/{APP_ID}.service"
            desktop.parent.mkdir(parents=True)
            service.parent.mkdir(parents=True)
            desktop.write_text(f'[Desktop Entry]\nExec="{checkout}/pyrobolic-launcher" %U\n')
            service.write_text(f'[D-BUS Service]\nExec={checkout}/pyrobolic-launcher --gapplication-service\n')
            state = data / "pyrobolic/state.json"
            state.parent.mkdir(parents=True)
            state.write_bytes(b"original state")
            self.assertEqual(len(conflicts(data, checkout)), 2)
            self.assertTrue(desktop.exists())
            with self.assertRaises(ValueError):
                apply(data, checkout, system)
            for name in ("usr/bin/pyrobolic", f"usr/share/applications/{APP_ID}.desktop", f"usr/share/dbus-1/services/{APP_ID}.service"):
                path = system / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            backup = apply(data, checkout, system)
            self.assertFalse(desktop.exists())
            self.assertFalse(service.exists())
            self.assertTrue((backup / desktop.relative_to(data)).is_file())
            self.assertEqual(state.read_bytes(), b"original state")
            self.assertIsNone(apply(data, checkout, system))

    def test_migration_rejects_unknown_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / f"applications/{APP_ID}.desktop"
            path.parent.mkdir()
            path.write_text("[Desktop Entry]\nExec=/some/other/program\n")
            with self.assertRaises(ValueError):
                conflicts(root, root)
            self.assertTrue(path.exists())

    def test_version_without_gi_and_dependency_failure_guidance(self):
        from pyrobolic.launcher import main
        with patch("sys.argv", ["pyrobolic", "--version"]), patch("builtins.print") as output:
            self.assertEqual(main(), 0)
            output.assert_called_once()
        with patch("sys.argv", ["pyrobolic"]), patch.dict("sys.modules", {"gi": None}), patch("builtins.print") as output:
            self.assertEqual(main(), 1)
            self.assertIn("sudo apt install", output.call_args.args[0])
