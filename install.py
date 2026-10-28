#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Register this checkout in the current user's application menu."""

import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parent
    launcher = root / "pyrobolic-launcher"
    launcher.chmod(0o755)
    data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    applications = data / "applications"
    icons = data / "icons/hicolor"
    applications.mkdir(parents=True, exist_ok=True)
    icons.mkdir(parents=True, exist_ok=True)
    icon = "io.github.woodpile72.Pyrobolic"
    source_icons = root / "pyrobolic/icons/hicolor"
    for source in source_icons.rglob("*"):
        if source.is_file() and source.name != "index.theme":
            destination = icons / source.relative_to(source_icons)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
    if shutil.which("gtk-update-icon-cache") and (icons / "index.theme").exists():
        subprocess.run(["gtk-update-icon-cache", "-f", str(icons)], check=True)
    # Desktop-entry escaping is distinct from shell quoting; Exec does not run a shell.
    command = str(launcher).replace("\\", "\\\\\\\\").replace('"', '\\\\"').replace("`", "\\\\`").replace("$", "\\\\$").replace("%", "%%")
    desktop = applications / f"{icon}.desktop"
    desktop.write_text(
        "[Desktop Entry]\nType=Application\nName=Pyrobolic\n"
        "Comment=Download video and audio\n"
        f'Exec="{command}" %U\nIcon={icon}\n'
        "Terminal=false\nCategories=GNOME;GTK;AudioVideo;\n"
        "DBusActivatable=true\nX-GNOME-UsesNotifications=true\n"
        "Keywords=video;audio;download;youtube;yt-dlp;\n"
        f"StartupWMClass={icon}\nStartupNotify=true\n", encoding="utf-8")
    services = data / "dbus-1/services"
    services.mkdir(parents=True, exist_ok=True)
    (services / f"{icon}.service").write_text(
        f"[D-BUS Service]\nName={icon}\nExec={shlex.quote(str(launcher))} --gapplication-service\n",
        encoding="utf-8")
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(applications)], check=True)
    print(f"Installed menu entry: {desktop}\nKeep this checkout at {root}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
