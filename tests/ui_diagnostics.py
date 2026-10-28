# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Private-display dependency UI checks without reading personal application data."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import traceback
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pyrobolic.app import Application, Preferences, Adw, GLib, Gtk
from pyrobolic.core import Store
from pyrobolic.diagnostics import Dependency, TOOLS
import gi
gi.require_version("GdkX11", "4.0")
from gi.repository import GdkX11

temporary = tempfile.TemporaryDirectory()
app = Application(Store(Path(temporary.name) / "state"))
state, failures = {}, []
hc = bool(os.environ.get("ADW_DEBUG_HIGH_CONTRAST"))
entered = threading.Event()
waiting = threading.Event()


def fixture(directory, processes):
    if waiting.is_set():
        state["worker_processes"] = processes
        entered.set()
        if not processes[0].cancelled.wait(5):
            raise RuntimeError("Close did not cancel dependency checks")
        raise InterruptedError("Canceled")
    return [Dependency(name, "Available", "/usr/local/bin/" + name, "Fixture version 1.2.3")
            if name != "deno" else Dependency(name, "Not Found", detail="Some sites need a JavaScript runtime.")
            for name in TOOLS]


def key(value):
    subprocess.run(["xdotool", "key", "--clearmodifiers", value], check=True)


def initial():
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT if hc else Adw.ColorScheme.FORCE_DARK)
    if hc:
        Gtk.Settings.get_default().set_property("gtk-font-name", "Sans 20")
    app.window.set_default_size(440, 850)
    preferences = state["preferences"] = Preferences(app.window)
    preferences.present(app.window)


def focus():
    state["preferences"].advanced.set_expanded(True)
    state["preferences"].dependencies_button.grab_focus()


def activate():
    key("Return")


def inspect():
    dialog = state["dialog"] = app.window.get_visible_dialog()
    assert dialog.status.get_text() == "Local checks complete"
    assert dialog.refresh.get_sensitive() and len(dialog.results) == 4
    assert "Not Found" in dialog.labels["deno"].get_text()
    for name, widget in dialog.labels.items():
        assert widget.get_selectable() and Gtk.test_accessible_has_property(widget, Gtk.AccessibleProperty.LABEL)
        ok, bounds = widget.compute_bounds(app.window)
        assert ok and bounds.get_x() >= 0 and bounds.get_x() + bounds.get_width() <= app.window.get_width() + 1, name
    assert not app.window.processes
    output = Path(__file__).resolve().parents[1] / ".qa"
    output.mkdir(exist_ok=True)
    subprocess.run(["import", "-window", str(GdkX11.X11Surface.get_xid(app.window.get_surface())),
                    str(output / ("dependencies-hc.png" if hc else "dependencies-dark.png"))], check=True)
    waiting.set()
    dialog.refresh.grab_focus()
    key("Return")


def close_busy():
    assert entered.is_set()
    assert not state["dialog"].refresh.get_sensitive()
    key("Escape")


def finish():
    assert state["dialog"].closed
    assert all(process.cancelled.is_set() for process in state["worker_processes"])
    assert not app.window.processes
    app.window.queue.shutdown()
    app.window.actions.shutdown(wait=True)
    app.quit()


steps = iter([initial, focus, activate, inspect, close_busy, finish])
def advance():
    try:
        next(steps)()
    except StopIteration:
        return False
    except Exception:
        failures.append(traceback.format_exc())
        app.quit()
        return False
    return True

with patch("pyrobolic.diagnostics_ui.check_dependencies", side_effect=fixture), patch.object(Preferences, "version"):
    GLib.timeout_add(1000, advance)
    app.run([])
temporary.cleanup()
if failures:
    print("\n".join(failures))
    raise SystemExit(1)
print("Dependency UI checks passed")
