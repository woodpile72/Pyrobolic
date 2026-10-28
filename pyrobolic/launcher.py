# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Small entry point with actionable diagnostics before importing the UI."""
import sys


def main():
    if "--version" in sys.argv[1:]:
        from . import __version__
        print(__version__)
        return 0
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Gtk, Adw
    except (ImportError, ValueError) as error:
        print("Pyrobolic needs Python GObject, GTK 4 and libadwaita.\n"
              "Install the .deb with: sudo apt install ./pyrobolic_<version>_all.deb\n"
              "For a checkout: sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1\n"
              f"Dependency error: {error}", file=sys.stderr)
        return 1
    if (Gtk.get_major_version(), Gtk.get_minor_version()) < (4, 14) or \
            (Adw.get_major_version(), Adw.get_minor_version()) < (1, 5):
        print("Pyrobolic requires GTK 4.14+ and libadwaita 1.5+. "
              "Use Linux Mint 22.x / Ubuntu 24.04 or a distribution with those libraries.", file=sys.stderr)
        return 1
    from .app import main as run
    return run()
