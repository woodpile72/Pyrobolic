# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Run under tests/session.conf and Xvfb; no personal settings or network."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pyrobolic.app import Application, Adw, GLib, Gtk
from pyrobolic.core import Store
import gi
gi.require_version("GdkX11", "4.0")
from gi.repository import GdkX11

temporary = tempfile.TemporaryDirectory()
store = Store(Path(temporary.name) / "state")
store.settings.output = str(Path(temporary.name) / "media")
store.paused = True
app = Application(store)
state, failures = {}, []


def initial():
    app.window.queue.stopped = True
    app.window.set_default_size(440, 850)
    if os.environ.get("ADW_DEBUG_HIGH_CONTRAST"):
        Gtk.Settings.get_default().set_property("gtk-font-name", "Sans 20")
    parent = app.window.new_download("https://example.org/fixture")
    parent.inspected([{"url": "https://example.org/fixture", "title": "Fixture", "media_id": "1", "formats_loaded": True, "subtitle_tracks": []}], None)
    state["parent"] = parent
    state["editor"] = parent.edit_metadata()


def choose():
    editor = state["editor"]
    assert editor.sponsor_mode.get_selected() == 0
    assert editor.sponsor_categories["sponsor"].get_active()
    assert not editor.sponsor_categories["intro"].get_active()
    editor.sponsor_mode.grab_focus()
    subprocess.run(["xdotool", "key", "--clearmodifiers", "Return", "Down", "Return"], check=True)


def cancel():
    editor = state["editor"]
    assert editor.sponsor_mode.get_selected() == 1
    assert Gtk.test_accessible_has_property(editor.sponsor_mode, Gtk.AccessibleProperty.LABEL)
    for widget in (editor.sponsor_mode, editor.apply):
        ok, bounds = widget.compute_bounds(app.window)
        assert ok and bounds.get_x() >= 0 and bounds.get_x() + bounds.get_width() <= app.window.get_width() + 1
    output = Path(os.environ.get("PYROBOLIC_QA_DIR", temporary.name))
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run(["import", "-window", str(GdkX11.X11Surface.get_xid(app.window.get_surface())), str(output / "sponsorblock.png")], check=True)
    editor.close()


def apply():
    parent = state["parent"]
    assert parent.media_preferences.sponsorblock == "off"
    editor = parent.edit_metadata()
    editor.sponsor_mode.set_selected(1)
    editor.save()
    assert parent.media_preferences.sponsorblock == "mark"
    assert store.settings.sponsorblock == "off"


def submit():
    state["parent"].enqueue()


def finish():
    restored = Store(store.directory)
    assert restored.settings.sponsorblock == "mark"
    assert restored.jobs[0].sponsorblock == "mark"
    app.window.queue.shutdown()
    app.window.actions.shutdown(wait=True)
    app.quit()


steps = iter([initial, choose, cancel, apply, submit, finish])
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

GLib.timeout_add(1000, advance)
app.run([])
temporary.cleanup()
if failures:
    raise RuntimeError("\n".join(failures))
print("SponsorBlock native draft, keyboard, layout and persistence checks passed")
