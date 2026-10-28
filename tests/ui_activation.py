# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Cold-start notification action using a private bus and temporary installation."""

import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gi.repository import Gio, GLib
from pyrobolic.core import Job, Store


root = Path(__file__).resolve().parents[1]
app_id = "io.github.woodpile72.Pyrobolic"
with tempfile.TemporaryDirectory() as directory:
    data = Path(directory)
    env = dict(os.environ, XDG_DATA_HOME=directory)
    subprocess.run([sys.executable, str(root / "install.py")], env=env, check=True, capture_output=True)
    store = Store(data / "pyrobolic")
    store.jobs = [Job("https://example.org/notification", "Notification cold-start fixture", directory,
                      id="cold-start", status="Completed", detail="Download complete")]
    store.save()
    config = ET.parse(root / "tests/session.conf")
    ET.SubElement(config.getroot(), "servicedir").text = str(data / "dbus-1/services")
    config_path = data / "bus.conf"
    config.write(config_path)
    bus = subprocess.Popen(["dbus-daemon", "--nofork", "--print-address=1", f"--config-file={config_path}"],
                           env=env, stdout=subprocess.PIPE, text=True)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(bus.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=10), "Private D-Bus did not start"
        address = bus.stdout.readline().strip()
        connection = Gio.DBusConnection.new_for_address_sync(address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
        def activate(action, parameters=()):
            connection.call_sync(app_id, "/io/github/woodpile72/Pyrobolic", "org.gtk.Actions", "Activate",
                                 GLib.Variant("(sava{sv})", (action, list(parameters), {})), None,
                                 Gio.DBusCallFlags.NONE, 15000, None)
        activate("show-download", [GLib.Variant("s", "cold-start")])
        windows = []
        previous = []
        stable_since = time.monotonic()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            found = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", "^Pyrobolic$"], capture_output=True, text=True)
            windows = found.stdout.split()
            if windows != previous:
                stable_since = time.monotonic()
                previous = windows
            if windows and time.monotonic() - stable_since > 1:
                break
            time.sleep(.1)
        assert windows, "Activation did not present the application"
        subprocess.run(["import", "-window", windows[-1], str(root / ".qa/notification-cold-start.png")],
                       check=True, timeout=10)
        activate("quit")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            owned = connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                "NameHasOwner", GLib.Variant("(s)", (app_id,)), None, Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]
            if not owned:
                break
            time.sleep(.1)
        assert not owned, "Activated application did not shut down"
        connection.close_sync(None)
    finally:
        bus.terminate()
        bus.wait(timeout=10)
        bus.stdout.close()
print("Cold-start notification action check passed", flush=True)
