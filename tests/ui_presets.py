# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Subtitle preset drafts, confirmations and keyboard access on a private display."""
from pathlib import Path
import os
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
    parent.inspected([{"title": "Preset fixture", "url": "https://example.org/fixture", "media_id": "1",
                       "formats_loaded": True, "subtitle_tracks": []}], None)
    state["editor"] = parent.edit_preferences(True)


def initial():
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT if hc else Adw.ColorScheme.FORCE_DARK)
    if hc:
        Gtk.Settings.get_default().set_property("gtk-font-name", "Sans 20")
    app.window.set_default_size(440, 850)
    app.window.queue.stopped = True
    new()


def save_draft():
    editor = state["editor"]
    editor.rows["subtitle_mode"].set_selected(1)
    editor.languages.set_text("en,es")
    editor.rows["subtitle_format"].set_selected(1)
    editor.presets.name.set_text("Bilingual")
    assert editor.presets.save.get_sensitive()
    assert Gtk.test_accessible_has_property(editor.presets.save, Gtk.AccessibleProperty.LABEL)
    editor.presets.save.grab_focus()
    key("Return")


def cancel_editor():
    assert "Bilingual" in state["editor"].presets.library
    assert store.settings.subtitle_presets == {}
    assert state["parent"].media_preferences.subtitle_presets == {}
    state["editor"].cancel.grab_focus()
    key("Return")


def again():
    assert state["parent"].media_preferences.subtitle_presets == {}
    state["editor"] = state["parent"].edit_preferences(True)


def replacement_prompt():
    editor = state["editor"]
    assert "Bilingual" in editor.presets.library
    editor.languages.set_text("de")
    editor.presets.save_clicked()


def cancel_replace():
    dialog = app.window.get_visible_dialog()
    assert isinstance(dialog, Adw.AlertDialog)
    assert dialog.get_default_response() == "cancel"
    key("Escape")


def load_saved():
    editor = state["editor"]
    assert editor.presets.library["Bilingual"]["subtitle_languages"] == "en,es"
    editor.presets.load.grab_focus()
    key("space")


def choose():
    key("End")
    key("Return")


def deletion_prompt():
    editor = state["editor"]
    assert editor.languages.get_text() == "en,es"
    editor.presets.remove.grab_focus()
    key("Return")


def cancel_delete():
    dialog = app.window.get_visible_dialog()
    assert isinstance(dialog, Adw.AlertDialog)
    assert dialog.get_default_response() == "cancel"
    key("Escape")


def delete_again():
    state["editor"].presets.remove_clicked()


def confirm_delete():
    def descendants(widget):
        yield widget
        child = widget.get_first_child()
        while child:
            yield from descendants(child)
            child = child.get_next_sibling()
    dialog = app.window.get_visible_dialog()
    target = next(w for w in descendants(dialog) if isinstance(w, Gtk.Button) and w.get_label() == "Delete")
    target.grab_focus()
    key("Return")


def deleted():
    assert not state["editor"].presets.library
    assert not state["editor"].draft.subtitle_presets
    assert store.settings.subtitle_presets == {}
    save_draft()


def inspect_layout():
    editor = state["editor"]
    assert "Bilingual" in editor.presets.library
    editor.presets.name.grab_focus()


def screenshot_and_apply():
    editor = state["editor"]
    for widget in (editor.presets.name, editor.presets.save, editor.presets.remove):
        ok, bounds = widget.compute_bounds(app.window)
        assert ok and bounds.get_x() >= 0 and bounds.get_x() + bounds.get_width() <= app.window.get_width() + 1
    output = Path(__file__).resolve().parents[1] / ".qa"
    output.mkdir(exist_ok=True)
    subprocess.run(["import", "-window", str(GdkX11.X11Surface.get_xid(app.window.get_surface())),
                    str(output / ("presets-hc.png" if hc else "presets-dark.png"))], check=True)
    editor.apply.grab_focus()
    key("Return")


def cancel_parent():
    assert "Bilingual" in state["parent"].media_preferences.subtitle_presets
    assert store.settings.subtitle_presets == {}
    state["parent"].close()


def apply_editor():
    state["editor"].apply.grab_focus()
    key("Return")


def submit():
    state["parent"].enqueue()


def finish():
    assert state["parent"].closed
    restored = Store(store.directory)
    assert restored.settings.subtitle_presets["Bilingual"]["subtitle_languages"] == "en,es"
    assert restored.jobs[0].subtitle_languages == "en,es"
    assert not hasattr(restored.jobs[0], "subtitle_presets")
    app.window.queue.shutdown()
    app.window.actions.shutdown(wait=True)
    app.quit()


steps = iter([initial, save_draft, cancel_editor, again, save_draft, replacement_prompt,
              cancel_replace, load_saved, choose, deletion_prompt, cancel_delete, delete_again,
              confirm_delete, deleted, inspect_layout,
              screenshot_and_apply, cancel_parent, new, save_draft, apply_editor, submit, finish])
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

GLib.timeout_add(900, advance)
app.run([])
temporary.cleanup()
if failures:
    print("\n".join(failures))
    raise SystemExit(1)
print("Subtitle preset UI checks passed")
