#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Build a deterministic Debian package without touching the host installation."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from pyrobolic import __version__

APP_ID = "io.github.woodpile72.Pyrobolic"
DEFAULT_MAINTAINER = "Erick Woods <erick@erick.com>"


def release_files(root, category):
    manifest = json.loads((root / "release-files.json").read_text())
    names = manifest[category]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate release manifest entry")
    for name in names:
        path = root / name
        if path.suffix.lower() in {".md", ".markdown", ".mdown"}:
            raise ValueError(f"Markdown is excluded from publication: {name}")
        if Path(name).is_absolute() or ".." in Path(name).parts or not path.is_file() or \
                any(part.is_symlink() for part in [path, *path.parents] if part != root.parent):
            raise ValueError(f"Unsafe or missing release file: {name}")
    return sorted(names)


def build(output, epoch=1793188800, revision=1, maintainer=DEFAULT_MAINTAINER):
    if revision is not None and (type(revision) is not int or revision < 1):
        raise ValueError("Development revision must be a positive integer")
    root = Path(__file__).resolve().parent
    if type(epoch) is not int or epoch < 0:
        raise ValueError("Build epoch must be a nonnegative integer")
    if not re.fullmatch(r"[^<>\r\n]+ <[^<>\s@]+@[^<>\s@]+>", maintainer):
        raise ValueError("Maintainer must be Name <email>, on one line")
    version = __version__ if revision is None else f"{__version__}+dev{revision}"
    output = Path(output).absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError("Choose a new package filename; existing builds are not overwritten.")
    with tempfile.TemporaryDirectory(prefix="pyrobolic-package-") as temporary:
        stage = Path(temporary) / "root"
        def write(path, text, executable=False):
            target = stage / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            target.chmod(0o755 if executable else 0o644)
        for name in release_files(root, "runtime"):
            target = stage / "usr/lib/pyrobolic" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / name, target)
            if name.startswith("pyrobolic/icons/hicolor/") and not name.endswith("/index.theme"):
                icon = stage / "usr/share/icons/hicolor" / Path(name).relative_to("pyrobolic/icons/hicolor")
                icon.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / name, icon)
        write("usr/lib/pyrobolic/pyrobolic/__init__.py",
              '# SPDX-License-Identifier: GPL-3.0-only\n# Copyright 2026 Erick Woods\n'
              f'__version__ = "{version}"\n')
        write("usr/bin/pyrobolic", '#!/usr/bin/python3\nimport sys\nsys.path.insert(0, "/usr/lib/pyrobolic")\nfrom pyrobolic.launcher import main\nraise SystemExit(main())\n', True)
        write(f"usr/share/applications/{APP_ID}.desktop",
              "[Desktop Entry]\nType=Application\nName=Pyrobolic\nComment=Download video and audio\n"
              f"Exec=/usr/bin/pyrobolic %U\nIcon={APP_ID}\nTerminal=false\nCategories=GNOME;GTK;AudioVideo;\n"
              f"DBusActivatable=true\nX-GNOME-UsesNotifications=true\nStartupWMClass={APP_ID}\nStartupNotify=true\n")
        write(f"usr/share/dbus-1/services/{APP_ID}.service",
              f"[D-BUS Service]\nName={APP_ID}\nExec=/usr/bin/pyrobolic --gapplication-service\n")
        write("usr/share/doc/pyrobolic/copyright", "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
              "Upstream-Name: Pyrobolic\n\nFiles: *\nCopyright: 2026 Erick Woods\nLicense: GPL-3\n"
              " Licensed under the GNU General Public License version 3 only.\n"
              " See /usr/share/common-licenses/GPL-3 for the complete license text.\n")
        write("usr/share/doc/pyrobolic/LICENSE", (root / "LICENSE").read_text())
        write("usr/share/doc/pyrobolic/NOTICE", (root / "NOTICE").read_text())
        write("usr/share/man/man1/pyrobolic.1", '.TH PYROBOLIC 1 "2026-10-28" "Pyrobolic"\n'
              '.SH NAME\npyrobolic \\- download video and audio\n.SH SYNOPSIS\n.B pyrobolic\n[URL ...]\n'
              '.SH DESCRIPTION\nReview and download media using GTK, yt-dlp and FFmpeg.\n'
              '.SH OPTIONS\n.TP\n.B --version\nPrint the installed version without opening a window.\n'
              '.SH FILES\nState and managed engines are kept under $XDG_DATA_HOME/pyrobolic,\n'
              'or ~/.local/share/pyrobolic. Removal does not delete user data or media.\n')
        from email.utils import formatdate
        changelog = (f"pyrobolic ({version}) unstable; urgency=medium\n\n"
                     "  * Native media downloading, verified outputs and optional tray operation.\n\n"
                     f" -- {maintainer}  {formatdate(epoch, usegmt=False)}\n")
        write("usr/share/doc/pyrobolic/changelog", changelog)
        for name in ("usr/share/doc/pyrobolic/changelog", "usr/share/man/man1/pyrobolic.1"):
            original = stage / name
            original.with_name(original.name + ".gz").write_bytes(gzip.compress(original.read_bytes(), mtime=0))
            original.unlink()
        size = sum(path.stat().st_size for path in stage.rglob("*") if path.is_file())
        write("DEBIAN/control", f"Package: pyrobolic\nVersion: {version}\nArchitecture: all\n"
              f"Maintainer: {maintainer}\nSection: video\nPriority: optional\nInstalled-Size: {(size + 1023) // 1024}\n"
              "Depends: python3 (>= 3.10), python3-gi, python3-xlib, gir1.2-gtk-4.0 (>= 4.14), gir1.2-adw-1 (>= 1.5), gir1.2-gdkpixbuf-2.0, gir1.2-secret-1, ffmpeg, yt-dlp\n"
              "Recommends: gnome-keyring, xdg-utils\n"
              "Description: Native GTK media downloader powered by yt-dlp\n"
              " Download video and audio with playlists, format and subtitle controls,\n"
              " verified output publication and optional background tray operation.\n")
        write("DEBIAN/md5sums", "".join(
            f"{hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()}  {path.relative_to(stage)}\n"
            for path in sorted((stage / "usr").rglob("*")) if path.is_file()))
        for path in sorted(stage.rglob("*"), reverse=True):
            if path.is_dir():
                path.chmod(0o755)
            elif path != stage / "usr/bin/pyrobolic":
                path.chmod(0o644)
            os.utime(path, (epoch, epoch))
        os.utime(stage, (epoch, epoch))
        built = Path(temporary) / "build.deb"
        subprocess.run(["dpkg-deb", "--root-owner-group", "--uniform-compression", "-Zxz", "-z6", "--build", str(stage), str(built)],
                       env=dict(os.environ, SOURCE_DATE_EPOCH=str(epoch)), check=True, capture_output=True)
        # Exclusive creation also protects against a concurrent build at the same path.
        with built.open("rb") as source, output.open("xb") as target:
            shutil.copyfileobj(source, target)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    versions = parser.add_mutually_exclusive_group()
    versions.add_argument("--revision", type=int, default=1)
    versions.add_argument("--base-version", dest="revision", action="store_const", const=None,
                          help="Use the application version without a development suffix; does not publish")
    parser.add_argument("--maintainer", default=DEFAULT_MAINTAINER)
    args = parser.parse_args()
    print(build(args.output, int(os.environ.get("SOURCE_DATE_EPOCH", "1793188800")), args.revision, args.maintainer))
