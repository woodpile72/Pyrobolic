# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Only run on the private, non-activating test bus with temporary XDG paths."""
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pyrobolic.auth import Authentication, AuthenticationError, CredentialVault
from pyrobolic.core import Process
from gi.repository import Gio

root = Path(sys.argv[1]).resolve()
assert root.name.startswith("pyrobolic-keyring-")
assert Path(os.environ["HOME"]) == root
assert Path(os.environ["XDG_DATA_HOME"]) == root / "data"
bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
def owned():
    from gi.repository import GLib
    return bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                         "NameHasOwner", GLib.Variant("(s)", ("org.freedesktop.secrets",)),
                         None, Gio.DBusCallFlags.NONE, 2000, None).unpack()[0]

assert not owned(), "Refusing to use an existing secret service"
vault = CredentialVault()
try:
    vault.get("a" * 32)
    raise AssertionError("Unavailable service should fail")
except AuthenticationError as error:
    assert "unavailable" in str(error)

control = root / "control"
control.mkdir(mode=0o700)
daemon = subprocess.Popen(["gnome-keyring-daemon", "--foreground", "--unlock", "--components=secrets",
                           "--control-directory=" + str(control)], stdin=subprocess.PIPE,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    daemon.stdin.write(b"fixture-keyring-unlock\n")
    daemon.stdin.close()
    deadline = time.monotonic() + 10
    while not owned():
        assert daemon.poll() is None
        assert time.monotonic() < deadline, "Private service did not start"
        time.sleep(0.05)
    url = "https://example.org/private?token=fixture-vault-query-secret"
    auth = Authentication(username="fixture-account", password="fixture-vault-password")
    identifier = vault.put(url, auth)
    assert vault.get(identifier) == (url, auth)
    canceled = Process()
    canceled.cancel()
    try:
        vault.get(identifier, canceled)
        raise AssertionError("Canceled lookup should fail")
    except InterruptedError:
        pass
    files = list((root / "data" / "keyrings").glob("*.keyring"))
    assert files, "Keyring did not persist in isolated XDG data"
    for path in files:
        content = path.read_bytes()
        assert b"fixture-vault-password" not in content
        assert b"fixture-vault-query-secret" not in content
    vault.forget(identifier)
    vault.forget(identifier)
    try:
        vault.get(identifier)
        raise AssertionError("Forgotten authentication should fail")
    except AuthenticationError as error:
        assert "no longer" in str(error)
    locked_id = vault.put(url, auth)
    from gi.repository import Secret
    service = Secret.Service.get_sync(Secret.ServiceFlags.OPEN_SESSION, None)
    collection = Secret.Collection.for_alias_sync(service, "default", Secret.CollectionFlags.NONE, None)
    count, _ = service.lock_sync([collection], None)
    assert count == 1
    waiting = Process()
    timer = threading.Timer(1, waiting.cancel)
    timer.start()
    try:
        vault.get(locked_id, waiting)
        raise AssertionError("Locked keyring must not return secrets without unlock")
    except (AuthenticationError, InterruptedError):
        pass
    finally:
        timer.cancel()
        timer.join()
finally:
    daemon.terminate()
    try:
        daemon.wait(timeout=5)
    except subprocess.TimeoutExpired:
        daemon.kill()
        daemon.wait()
print("Private Secret Service roundtrip, encrypted persistence, cancellation and deletion passed")
