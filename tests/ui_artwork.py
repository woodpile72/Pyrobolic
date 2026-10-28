# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Native artwork choices, cancellation, compatibility and submitted snapshots."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pyrobolic.app import Application, Adw, GLib, Gtk
from pyrobolic.core import CONTAINERS, Store
import gi
gi.require_version("GdkX11", "4.0")
from gi.repository import GdkX11

temporary = tempfile.TemporaryDirectory()
store = Store(Path(temporary.name) / "state")
store.settings.output = str(Path(temporary.name) / "media")
store.paused = True
app = Application(store)
state, failures = {}, []
hc = bool(os.environ.get("ADW_DEBUG_HIGH_CONTRAST"))


def key(value):
    subprocess.run(["xdotool", "key", "--clearmodifiers", value], check=True)


def new():
    parent = state["parent"] = app.window.new_download("https://example.org/fixture")
    parent.inspected([{"url": "https://example.org/fixture", "title": "Artwork fixture", "media_id": "1",
                       "formats_loaded": True, "subtitle_tracks": []}], None)


def initial():
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT if hc else Adw.ColorScheme.FORCE_DARK)
    if hc:
        Gtk.Settings.get_default().set_property("gtk-font-name", "Sans 20")
    app.window.set_default_size(440, 850)
    app.window.queue.stopped = True
    new()


def open_editor():
    state["parent"].more.set_expanded(True)
    state["parent"].artwork_button.grab_focus()


def activate():
    key("Return")


def choose():
    editor = state["editor"] = app.window.get_visible_dialog()
    assert Gtk.test_accessible_has_property(editor.mode, Gtk.AccessibleProperty.LABEL)
    editor.mode.grab_focus()
    key("Return")
    key("Down")
    key("Return")


def screenshot_cancel():
    editor = state["editor"]
    assert editor.mode.get_selected() == 1
    for widget in (editor.mode, editor.apply):
        ok, bounds = widget.compute_bounds(app.window)
        assert ok and bounds.get_x() >= 0 and bounds.get_x() + bounds.get_width() <= app.window.get_width() + 1
    output = Path(__file__).resolve().parents[1] / ".qa"
    output.mkdir(exist_ok=True)
    subprocess.run(["import", "-window", str(GdkX11.X11Surface.get_xid(app.window.get_surface())),
                    str(output / ("artwork-hc.png" if hc else "artwork-dark.png"))], check=True)
    key("Escape")


def crop():
    editor = state["editor"]
    assert editor.square.get_sensitive()
    editor.square.grab_focus()
    key("space")


def canceled():
    assert state["parent"].media_preferences.artwork_mode == "off"
    assert not state["parent"].media_preferences.square_audio_artwork
    assert store.settings.artwork_mode == "off"
    state["parent"].container.set_selected(CONTAINERS.index("webm"))
    state["editor"] = state["parent"].edit_artwork()


def incompatible():
    editor = state["editor"]
    editor.mode.set_selected(2)
    assert not editor.apply.get_sensitive()
    assert "cannot embed" in editor.error.get_text()
    editor.mode.set_selected(1)
    assert editor.apply.get_sensitive()
    editor.apply.grab_focus()
    key("Return")


def parent_cancel():
    assert state["parent"].media_preferences.artwork_mode == "save"
    assert store.settings.artwork_mode == "off"
    state["parent"].close()


def reopen():
    new()
    state["editor"] = state["parent"].edit_artwork()


def apply():
    state["editor"].mode.set_selected(3)
    state["editor"].square.set_active(True)
    state["editor"].apply.grab_focus()
    key("Return")


def submit():
    state["parent"].enqueue()


def finish():
    restored = Store(store.directory)
    assert restored.settings.artwork_mode == "both"
    assert restored.jobs[0].artwork_mode == "both"
    assert restored.jobs[0].square_audio_artwork
    assert restored.settings.square_audio_artwork
    app.window.queue.shutdown()
    app.window.actions.shutdown(wait=True)
    app.quit()


steps = iter([initial, open_editor, activate, choose, crop, screenshot_cancel, canceled, incompatible,
              parent_cancel, reopen, apply, submit, finish])
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
    print("\n".join(failures))
    raise SystemExit(1)
print("Artwork UI checks passed")
