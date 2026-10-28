# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Private native metadata draft, keyboard, layout and submission checks."""
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
hc = bool(os.environ.get("ADW_DEBUG_HIGH_CONTRAST"))


def key(value):
    subprocess.run(["xdotool", "key", "--clearmodifiers", value], check=True)


def new():
    parent = state["parent"] = app.window.new_download("https://example.org/fixture")
    parent.inspected([{"url": "https://example.org/fixture", "title": "Metadata fixture", "media_id": "1",
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
    state["parent"].metadata_button.grab_focus()


def activate():
    key("Return")


def choose():
    editor = state["editor"] = app.window.get_visible_dialog()
    for row in (editor.chapters, editor.source_info, editor.description, editor.titles):
        assert Gtk.test_accessible_has_property(row, Gtk.AccessibleProperty.LABEL)
    editor.chapters.grab_focus()
    key("Return")
    key("Down")
    key("Return")


def screenshot_cancel():
    editor = state["editor"]
    assert editor.chapters.get_selected() == 1
    assert editor.split.get_active() and editor.precise.get_active()
    editor.source_info.set_selected(2)
    for widget in (editor.chapters, editor.source_info, editor.apply):
        ok, bounds = widget.compute_bounds(app.window)
        assert ok and bounds.get_x() >= 0 and bounds.get_x() + bounds.get_width() <= app.window.get_width() + 1
    output = Path(__file__).resolve().parents[1] / ".qa"
    output.mkdir(exist_ok=True)
    subprocess.run(["import", "-window", str(GdkX11.X11Surface.get_xid(app.window.get_surface())),
                    str(output / ("metadata-hc.png" if hc else "metadata-dark.png"))], check=True)
    key("Escape")


def choose_split():
    editor = state["editor"]
    assert editor.description.get_active()
    output = Path(__file__).resolve().parents[1] / ".qa"
    output.mkdir(exist_ok=True)
    subprocess.run(["import", "-window", str(GdkX11.X11Surface.get_xid(app.window.get_surface())),
                    str(output / ("description-hc.png" if hc else "description-dark.png"))], check=True)
    editor.split.grab_focus()
    key("space")


def choose_description():
    editor = state["editor"]
    editor.description.grab_focus()
    key("space")


def choose_precise():
    editor = state["editor"]
    assert editor.split.get_active() and editor.precise.get_sensitive()
    editor.precise.grab_focus()
    key("space")


def choose_titles():
    editor = state["editor"]
    assert editor.titles.get_sensitive()
    editor.titles.grab_focus()
    key("space")


def canceled():
    assert not state["parent"].media_preferences.save_description
    assert not store.settings.save_description
    assert not state["parent"].media_preferences.chapter_titles
    assert not store.settings.chapter_titles
    assert state["parent"].media_preferences.chapter_policy == "metadata"
    assert store.settings.chapter_policy == "metadata"
    state["editor"] = state["parent"].edit_metadata()


def apply():
    editor = state["editor"]
    editor.description.set_active(True)
    editor.chapters.set_selected(1)
    editor.source_info.set_selected(2)
    editor.split.set_active(True)
    editor.titles.set_active(True)
    editor.precise.set_active(True)
    editor.apply.grab_focus()
    key("Return")


def parent_cancel():
    assert state["parent"].media_preferences.chapter_titles
    assert not store.settings.chapter_titles
    assert state["parent"].media_preferences.save_description
    assert not store.settings.save_description
    assert state["parent"].media_preferences.chapter_policy == "embed"
    assert store.settings.chapter_policy == "metadata"
    state["parent"].close()


def reopen():
    new()
    state["editor"] = state["parent"].edit_metadata()


def submit():
    state["parent"].enqueue()


def finish():
    restored = Store(store.directory)
    assert restored.settings.save_description and restored.jobs[0].save_description
    assert restored.settings.chapter_titles and restored.jobs[0].chapter_titles
    assert restored.settings.chapter_policy == "embed"
    assert restored.settings.source_info_policy == "omit"
    assert restored.jobs[0].chapter_policy == "embed"
    assert restored.jobs[0].source_info_policy == "omit"
    assert restored.jobs[0].split_chapters and restored.jobs[0].precise_chapters
    assert restored.settings.split_chapters and restored.settings.precise_chapters
    app.window.queue.shutdown()
    app.window.actions.shutdown(wait=True)
    app.quit()


steps = iter([initial, open_editor, activate, choose, choose_description, choose_split, choose_titles, choose_precise, screenshot_cancel, canceled, apply, parent_cancel,
              reopen, apply, submit, finish])
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
print("Metadata UI checks passed")
