# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Native encoding draft, compatibility, keyboard and persisted-submission checks."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pyrobolic.app import Application, Adw, GLib, Gtk
from pyrobolic.core import CONTAINERS, Store
from pyrobolic.encoding import ENCODERS, QUALITIES
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
    parent.inspected([{"url": "https://example.org/fixture", "title": "Encoding fixture", "media_id": "1",
                       "formats_loaded": True, "subtitle_tracks": []}], None)
    assert not parent.encoding_row.get_visible()
    parent.container.set_selected(CONTAINERS.index("webm"))
    parent.processing.set_selected(1)
    assert parent.encoding_row.get_visible()


def initial():
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT if hc else Adw.ColorScheme.FORCE_DARK)
    if hc:
        Gtk.Settings.get_default().set_property("gtk-font-name", "Sans 20")
    app.window.set_default_size(440, 850)
    app.window.queue.stopped = True
    new()


def open_editor():
    state["parent"].more.set_expanded(True)
    state["parent"].encoding_button.grab_focus()


def activate_editor():
    key("Return")


def choose():
    editor = state["editor"] = app.window.get_visible_dialog()
    assert Gtk.test_accessible_has_property(editor.encoder, Gtk.AccessibleProperty.LABEL)
    editor.encoder.set_selected(ENCODERS.index("h264"))
    assert not editor.apply.get_sensitive()
    assert "container" in editor.error.get_text()
    editor.encoder.set_selected(ENCODERS.index("vp9"))
    editor.quality.set_selected(QUALITIES.index("high"))
    editor.threads.set_value(2)
    editor.always.set_active(True)
    assert Gtk.test_accessible_has_property(editor.always, Gtk.AccessibleProperty.LABEL)
    assert editor.apply.get_sensitive()
    assert "CRF 24" in editor.details.get_text()
    assert state["parent"].media_preferences.video_encoder == "default"
    editor.encoder.grab_focus()


def screenshot():
    editor = state["editor"]
    assert editor.always.get_active()
    for widget in (editor.always, editor.encoder, editor.quality, editor.apply):
        ok, bounds = widget.compute_bounds(app.window)
        assert ok and bounds.get_x() >= 0 and bounds.get_x() + bounds.get_width() <= app.window.get_width() + 1
    output = Path(__file__).resolve().parents[1] / ".qa"
    output.mkdir(exist_ok=True)
    subprocess.run(["import", "-window", str(GdkX11.X11Surface.get_xid(app.window.get_surface())),
                    str(output / ("encoding-hc.png" if hc else "encoding-dark.png"))], check=True)
    editor.cancel.grab_focus()
    key("Return")


def toggle_always():
    state["editor"].always.grab_focus()
    key("space")


def toggle_back():
    assert not state["editor"].always.get_active()
    key("space")


def canceled():
    assert state["parent"].media_preferences.video_encoder == "default"
    assert store.settings.video_encoder == "default"
    assert not store.settings.reencode_video
    assert not state["parent"].media_preferences.reencode_video
    state["editor"] = state["parent"].edit_encoding()


def apply():
    choose()
    state["editor"].apply.grab_focus()
    key("Return")


def cancel_parent():
    parent = state["parent"]
    assert parent.media_preferences.video_encoder == "vp9"
    assert parent.media_preferences.reencode_video
    assert store.settings.video_encoder == "default"
    assert not store.settings.reencode_video
    parent.close()


def submit():
    parent = state["parent"]
    parent.mode.set_selected(1)
    assert not parent.encoding_row.get_visible()
    parent.mode.set_selected(0)
    assert parent.encoding_row.get_visible()
    parent.enqueue()


def finish():
    assert state["parent"].closed
    restored = Store(store.directory)
    assert restored.settings.video_encoder == "vp9"
    assert restored.settings.video_encoding_quality == "high"
    assert restored.jobs[0].video_encoder == "vp9"
    assert restored.jobs[0].video_encoding_quality == "high"
    assert restored.jobs[0].video_encoding_threads == 2
    assert restored.settings.reencode_video
    assert restored.jobs[0].reencode_video
    app.window.queue.shutdown()
    app.window.actions.shutdown(wait=True)
    app.quit()


steps = iter([initial, open_editor, activate_editor, choose, toggle_always, toggle_back, screenshot, canceled, apply, cancel_parent,
              new, open_editor, activate_editor, apply, submit, finish])
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
print("Encoding UI checks passed")
