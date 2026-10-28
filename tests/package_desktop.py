# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Installed menu launch, single instance and icon lookup on a private display/bus.

Run with temporary HOME/XDG directories, after installing the package in a
disposable root. This test does not register a checkout or alter desktop settings.
"""
import os
from pathlib import Path
import subprocess
import sys
import time

assert os.environ.get("PYROBOLIC_TEST_ROOT") == "/usr/lib/pyrobolic"
assert os.environ.get("XDG_DATA_HOME", "").startswith("/tmp/"), "Use isolated state"
sys.path.insert(0, "/usr/lib/pyrobolic")
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk, Gdk
from Xlib.display import Display
from pyrobolic import __version__

APP_ID = "io.github.woodpile72.Pyrobolic"
desktop = f"/usr/share/applications/{APP_ID}.desktop"
subprocess.run(["desktop-file-validate", desktop], check=True)
assert subprocess.check_output(["/usr/bin/pyrobolic", "--version"], text=True).strip() == __version__
Gtk.init()
theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
for name in (APP_ID, APP_ID + "-tray-symbolic", APP_ID + "-tray-color"):
    assert theme.has_icon(name), name
    paintable = theme.lookup_icon(name, None, 24, 1, Gtk.TextDirection.LTR, Gtk.IconLookupFlags.FORCE_REGULAR)
    assert paintable.get_file() and Path(paintable.get_file().get_path()).is_file(), name

bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
display = Display()
root = display.screen().root
atom = display.intern_atom("_NET_WM_NAME")


def owned():
    return bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
                         GLib.Variant("(s)", (APP_ID,)), None, Gio.DBusCallFlags.NONE, 3000, None).unpack()[0]


def windows():
    found = []
    for window in root.query_tree().children:
        try:
            title = window.get_full_property(atom, 0)
            if title is not None and bytes(title.value).decode() == "Pyrobolic":
                found.append(window.id)
        except Exception:
            pass
    return found


def wait_for(predicate):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.1)
    raise AssertionError("Desktop launch/exit timed out")


def quit_app():
    bus.call_sync(APP_ID, "/io/github/woodpile72/Pyrobolic", "org.gtk.Actions", "Activate",
                  GLib.Variant("(sava{sv})", ("quit", [], {})), None, Gio.DBusCallFlags.NONE, 3000, None)


try:
    subprocess.run(["gio", "launch", desktop], check=True)
    wait_for(lambda: owned() and len(windows()) == 1)
    original = windows()
    subprocess.run(["gio", "launch", desktop], check=True)
    time.sleep(1)
    assert windows() == original, "Second menu launch created another window"
    quit_app()
    wait_for(lambda: not owned())
finally:
    if owned():
        quit_app()
        wait_for(lambda: not owned())
    display.close()
print("Installed menu launch, single instance, explicit exit and all three themed icons passed")
