#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Inspect checkout launcher conflicts; change them only with an explicit --apply."""
import argparse
import configparser
import datetime
import os
from pathlib import Path
import shlex
import shutil

APP_ID = "io.github.woodpile72.Pyrobolic"


def conflicts(data, checkout):
    candidates = [(f"applications/{APP_ID}.desktop", "Desktop Entry", ([], ["%U"])),
                  (f"dbus-1/services/{APP_ID}.service", "D-BUS Service", (["--gapplication-service"],))]
    found = []
    for relative, section, suffixes in candidates:
        path = data / relative
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink():
            raise ValueError(f"Review symlink manually: {path}")
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(path)
        command = shlex.split(parser[section]["Exec"])
        expected = str(checkout / "pyrobolic-launcher")
        if command not in [[expected, *suffix] for suffix in suffixes]:
            raise ValueError(f"Unrecognized launcher; left unchanged: {path}")
        found.append((path, path.read_bytes()))
    return found


def apply(data, checkout, system=Path("/")):
    if not (system / "usr/bin/pyrobolic").is_file() or not (system / f"usr/share/applications/{APP_ID}.desktop").is_file() or \
            not (system / f"usr/share/dbus-1/services/{APP_ID}.service").is_file():
        raise ValueError("Install and verify the system package before retiring the checkout launchers.")
    found = conflicts(data, checkout)
    # Only remove icon overrides that are byte-identical to this checkout's supplied assets.
    for source in (checkout / "pyrobolic/icons/hicolor").rglob("*"):
        if not source.is_file() or source.name == "index.theme":
            continue
        target = data / "icons/hicolor" / source.relative_to(checkout / "pyrobolic/icons/hicolor")
        if target.is_file() and not target.is_symlink() and target.read_bytes() == source.read_bytes():
            found.append((target, target.read_bytes()))
    if not found:
        return None
    backup = data / "pyrobolic-launcher-backups" / datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup.mkdir(parents=True, exist_ok=False)
    for path, content in found:
        copy = backup / path.relative_to(data)
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_bytes(content)
    removed = []
    try:
        for path, content in found:
            if path.is_symlink() or path.read_bytes() != content:
                raise ValueError(f"File changed during migration: {path}")
            path.unlink()
            removed.append(path)
    except Exception:
        for path in removed:
            with path.open("xb") as stream:
                stream.write((backup / path.relative_to(data)).read_bytes())
        raise
    return backup


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--apply", action="store_true", help="Archive recognized per-user overrides after package installation")
    args = parser.parse_args()
    data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    try:
        if args.apply:
            print("Launcher backup:", apply(data, args.checkout.absolute()) or "No changes needed")
            print("Log out and back in to refresh desktop and D-Bus activation caches.")
        else:
            for path, _ in conflicts(data, args.checkout.absolute()):
                print("Checkout override:", path)
            print("Read-only inspection. No files were changed.")
    except (ValueError, OSError, KeyError, configparser.Error) as error:
        parser.exit(1, str(error) + "\n")
