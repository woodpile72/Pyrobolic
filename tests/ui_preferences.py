# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Preferences, subtitle drafts and keyboard interactions on an isolated display."""

from pathlib import Path
import os
import subprocess
import sys
import tempfile
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pyrobolic.app import Application, Adw, GLib, Gtk
from pyrobolic.core import Store
from pyrobolic.formats import catalog, make_choice
from test_formats import FORMATS
import gi
gi.require_version("GdkX11", "4.0")
from gi.repository import GdkX11

temporary = tempfile.TemporaryDirectory()
store = Store(Path(temporary.name) / "state")
store.settings.output = str(Path(temporary.name) / "media")
store.paused = True
app = Application(store)
state, failures = {}, []
output = Path(__file__).resolve().parents[1] / ".qa"
output.mkdir(exist_ok=True)
prefix = "preferences-hc" if os.environ.get("ADW_DEBUG_HIGH_CONTRAST") else "preferences"


def key(value):
    subprocess.run(["xdotool", "key", "--clearmodifiers", value], check=True)


def screenshot(name):
    subprocess.run(["import", "-window", str(GdkX11.X11Surface.get_xid(app.window.get_surface())),
                    str(output / f"{prefix}-{name}.png")], check=True, timeout=10)


def new_dialog():
    dialog = app.window.new_download("https://example.org/fixture")
    dialog.inspected([{"title": "Preferences fixture", "url": "https://example.org/fixture", "media_id": "1",
                       "formats": catalog({"formats": FORMATS}), "formats_loaded": True,
                       "subtitle_tracks": [{"code": "en", "name": "English", "source": "manual"},
                                           {"code": "es", "name": "Spanish", "source": "generated"}]}], None)
    state["parent"] = dialog
    return dialog


def initial():
    if prefix.endswith("hc"):
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        Gtk.Settings.get_default().set_property("gtk-font-name", "Sans 20")
    else:
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
    app.window.queue.stopped = True
    state["editor"] = new_dialog().edit_preferences()


def keyboard_choice():
    xid = str(GdkX11.X11Surface.get_xid(app.window.get_surface()))
    subprocess.run(["xdotool", "windowfocus", "--sync", xid], check=True)
    row = state["editor"].rows["preferred_video_codec"]
    assert Gtk.test_accessible_has_property(row, Gtk.AccessibleProperty.LABEL)
    row.grab_focus()
    key("space")


def choose_h264():
    key("Home")
    key("Down")
    key("Return")


def cancel_preferences():
    editor = state["editor"]
    assert editor.draft.preferred_video_codec == "h264"
    assert state["parent"].media_preferences.preferred_video_codec == "any"
    editor.cancel.grab_focus()
    key("Return")


def reopen():
    assert app.window.get_visible_dialog() == state["parent"]
    assert state["parent"].media_preferences.preferred_video_codec == "any"
    editor = state["editor"] = state["parent"].edit_preferences()
    editor.rows["preferred_video_codec"].set_selected(1)
    editor.rows["preferred_fps"].set_selected(2)


def apply_preferences():
    screenshot("quality-wide")
    state["editor"].apply.grab_focus()
    key("Return")


def discard_parent():
    assert state["parent"].media_preferences.preferred_video_codec == "h264"
    assert store.settings.preferred_video_codec == "any"
    key("Escape")


def fresh():
    assert state["parent"].closed
    assert store.settings.preferred_video_codec == "any"
    parent = new_dialog()
    assert parent.media_preferences.preferred_video_codec == "any"
    state["editor"] = parent.edit_preferences()
    state["editor"].rows["preferred_video_codec"].set_selected(1)
    state["editor"].rows["preferred_fps"].set_selected(2)
    app.window.set_default_size(390, 760)


def narrow_quality():
    editor = state["editor"]
    for row in editor.rows.values():
        okay, bounds = row.compute_bounds(app.window)
        assert okay and bounds.get_x() >= 0
        assert bounds.get_x() + bounds.get_width() <= app.window.get_width() + 1
    assert editor.child_focus(Gtk.DirectionType.TAB_FORWARD)
    screenshot("quality-narrow")
    editor.apply.grab_focus()
    key("Return")


def subtitles():
    editor = state["editor"] = state["parent"].edit_preferences(True)
    editor.rows["subtitle_mode"].set_selected(1)
    assert editor.available_codes == ["en"]
    editor.languages.set_text("en.*")
    assert not editor.apply.get_sensitive()
    assert editor.error.get_text()
    editor.add_language()
    assert editor.languages.get_text() == "en.*"
    editor.languages.set_text("en")
    assert editor.apply.get_sensitive()
    editor.rows["subtitle_source"].set_selected(1)
    editor.rows["subtitle_format"].set_selected(3)
    editor.rows["subtitle_missing"].set_selected(1)


def language_popup():
    editor = state["editor"]
    assert editor.available.get_enable_search()
    assert Gtk.test_accessible_has_property(editor.available, Gtk.AccessibleProperty.LABEL)
    editor.available.grab_focus()
    key("space")


def language_search():
    subprocess.run(["xdotool", "type", "--clearmodifiers", "Spanish"], check=True)


def language_select():
    key("Down")
    key("Return")


def language_add():
    editor = state["editor"]
    assert editor.available.get_selected() == 1
    editor.add_language_button.grab_focus()
    key("Return")


def subtitle_layout():
    editor = state["editor"]
    assert editor.languages.get_text() == "en,es"
    for row in [*editor.rows.values(), editor.languages, editor.apply, editor.available, editor.add_language_button]:
        okay, bounds = row.compute_bounds(app.window)
        assert okay and bounds.get_x() >= 0
        assert bounds.get_x() + bounds.get_width() <= app.window.get_width() + 1
    assert Gtk.test_accessible_has_property(editor.languages, Gtk.AccessibleProperty.LABEL)
    editor.languages.grab_focus()
    key("Tab")
    screenshot("subtitles-narrow")


def cancel_subtitles():
    editor = state["editor"]
    assert state["parent"].media_preferences.subtitle_mode == "legacy"
    editor.cancel.grab_focus()
    key("Return")


def canceled_subtitles():
    assert state["parent"].media_preferences.subtitle_mode == "legacy"
    editor = state["editor"] = state["parent"].edit_preferences(True)
    editor.rows["subtitle_mode"].set_selected(1)
    editor.languages.set_text("en,es")
    editor.rows["subtitle_source"].set_selected(1)
    editor.rows["subtitle_format"].set_selected(3)


def apply_subtitles():
    state["editor"].apply.grab_focus()
    key("Return")


def enqueue():
    parent = state["parent"]
    assert parent.media_preferences.subtitle_languages == "en,es"
    assert store.settings.subtitle_mode == "legacy"
    item = parent.entries[0][0]
    item["source_choices"] = {"video": make_choice(item["formats"], "video", "v-high", "a-low")}
    parent.refresh_names()
    assert not parent.preferences_row.get_sensitive()
    parent.metadata.set_active(False)
    parent.enqueue()


def persisted():
    assert state["parent"].closed
    restored = Store(store.directory)
    job = restored.jobs[0]
    assert restored.settings.preferred_video_codec == "h264"
    assert restored.settings.preferred_fps == 30
    assert restored.settings.subtitle_languages == "en,es"
    assert restored.settings.subtitle_source == "manual-auto"
    assert (job.source_format, job.source_audio) == ("v-high", "a-low")
    assert job.preferred_video_codec == "h264" and job.subtitle_format == "vtt"
    assert not job.metadata
    parent = new_dialog()
    assert parent.media_preferences.preferred_video_codec == "h264"
    assert parent.source_choice(parent.entries[0][0]) is None
    assert parent.preferences_row.get_sensitive()
    assert parent.media_preferences.subtitle_mode == "sidecar"
    state["editor"] = parent.edit_preferences(True)


def automatic_popup():
    editor = state["editor"]
    editor.rows["subtitle_source"].grab_focus()
    key("space")


def choose_automatic():
    key("End")
    key("Return")


def all_popup():
    editor = state["editor"]
    assert editor.draft.subtitle_source == "auto"
    assert editor.available_codes == ["es"]
    editor.rows["subtitle_language_scope"].grab_focus()
    key("space")


def choose_all():
    key("End")
    key("Return")


def all_layout():
    editor = state["editor"]
    assert editor.draft.subtitle_language_scope == "all"
    assert not editor.languages.get_visible()
    assert not editor.available.get_visible()
    assert "many translated" in editor.availability.get_text()
    assert "not substituted" in editor.availability.get_text()
    for row in [*editor.rows.values(), editor.apply]:
        okay, bounds = row.compute_bounds(app.window)
        assert okay and bounds.get_x() >= 0
        assert bounds.get_x() + bounds.get_width() <= app.window.get_width() + 1
    screenshot("all-generated-narrow")
    editor.cancel.grab_focus()
    key("Return")


def canceled_all():
    parent = state["parent"]
    assert parent.media_preferences.subtitle_source == "manual-auto"
    assert parent.media_preferences.subtitle_language_scope == "selected"
    editor = state["editor"] = parent.edit_preferences(True)
    editor.rows["subtitle_source"].set_selected(2)
    editor.rows["subtitle_language_scope"].set_selected(1)
    editor.rows["subtitle_language_scope"].set_selected(0)
    assert editor.languages.get_visible()
    assert editor.languages.get_text() == "en,es"
    editor.rows["subtitle_language_scope"].set_selected(1)


def apply_all():
    state["editor"].apply.grab_focus()
    key("Return")


def enqueue_all():
    assert store.settings.subtitle_language_scope == "selected"
    state["parent"].enqueue()


def persisted_all():
    restored = Store(store.directory)
    assert restored.settings.subtitle_source == "auto"
    assert restored.settings.subtitle_language_scope == "all"
    assert restored.settings.subtitle_languages == "en,es"
    assert restored.jobs[0].subtitle_source == "manual-auto"
    assert restored.jobs[1].subtitle_source == "auto"
    assert restored.jobs[1].subtitle_language_scope == "all"
    assert restored.jobs[1].source_format is None
    parent = new_dialog()
    parent.mode.set_selected(1)
    state["editor"] = parent.edit_preferences()


def audio_preferences():
    editor = state["editor"]
    for name in ("preferred_video_codec", "preferred_fps", "upscaled_sources"):
        assert not editor.rows[name].get_visible()
    assert editor.rows["preferred_audio_codec"].get_visible()
    assert editor.rows["preferred_abr"].get_visible()
    assert editor.draft.preferred_video_codec == "h264"
    screenshot("audio-source-preferences")
    editor.cancel.grab_focus()
    key("Return")


def finish():
    assert store.settings.preferred_video_codec == "h264"
    app.window.closing = True
    app.window.actions.shutdown(wait=True)
    app.quit()


steps = iter([initial, keyboard_choice, choose_h264, cancel_preferences, reopen, apply_preferences,
              discard_parent, fresh, narrow_quality, subtitles, language_popup, language_search, language_select,
              language_add, subtitle_layout, cancel_subtitles, canceled_subtitles,
              apply_subtitles, enqueue, persisted, automatic_popup, choose_automatic, all_popup, choose_all,
              all_layout, canceled_all, apply_all, enqueue_all, persisted_all, audio_preferences, finish])


def step():
    try:
        next(steps)()
    except StopIteration:
        return False
    except Exception:
        failures.append(traceback.format_exc())
        app.quit()
        return False
    return True


app.connect("activate", lambda *_: GLib.timeout_add(700, step))
app.run([])
temporary.cleanup()
if failures:
    print("\n".join(failures), file=sys.stderr)
    raise SystemExit(1)
print("Preference/subtitle UI checks passed", flush=True)
