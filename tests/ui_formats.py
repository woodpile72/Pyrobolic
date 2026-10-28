# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Exact-format workflow on a private display, with isolated state and probe fixtures."""

from pathlib import Path
import os
import subprocess
import sys
import tempfile
import traceback
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pyrobolic.app import Application, Adw, GLib, Gtk
from pyrobolic.core import Store
from pyrobolic.formats import catalog
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
prefix = "formats-hc" if os.environ.get("ADW_DEBUG_HIGH_CONTRAST") else "formats"


def xid():
    return str(GdkX11.X11Surface.get_xid(app.window.get_surface()))


def key(value):
    subprocess.run(["xdotool", "key", "--clearmodifiers", value], check=True)


def screenshot(name, root=False):
    subprocess.run(["import", "-window", "root" if root else xid(), str(output / f"{prefix}-{name}.png")],
                   check=True, timeout=10)


def reveal(widget):
    scroll = widget.get_ancestor(Gtk.ScrolledWindow)
    okay, bounds = widget.compute_bounds(scroll.get_child())
    assert okay
    scroll.get_vadjustment().set_value(max(0, bounds.get_y() - 12))


def item(suffix="1", loaded=True):
    return {"title": "Source format test " + suffix, "url": "https://example.org/" + suffix,
            "media_id": suffix, "source": "Fixture", "formats": catalog({"formats": FORMATS}) if loaded else [],
            "formats_loaded": loaded}


def choose(row, formats, identifier):
    row.set_selected(next(i + 1 for i, f in enumerate(formats) if f["id"] == identifier))


def initial():
    if prefix == "formats-hc":
        assert Adw.StyleManager.get_default().get_high_contrast()
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        Gtk.Settings.get_default().set_property("gtk-font-name", "Sans 20")
    else:
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
    app.window.queue.stopped = True
    dialog = app.window.new_download("https://example.org/1")
    dialog.inspected([item()], None)
    state["dialog"] = dialog
    state["editor"] = dialog.edit_formats(dialog.entries[0][0])


def automatic():
    subprocess.run(["xdotool", "windowfocus", "--sync", xid()], check=True)
    editor = state["editor"]
    assert editor.primary.get_enable_search()
    assert editor.choice() is None
    assert not editor.audio.get_visible()
    assert Gtk.test_accessible_has_property(editor.primary, Gtk.AccessibleProperty.LABEL)
    choose(editor.primary, editor.primary_formats, "v-high")
    assert editor.audio.get_visible()
    choose(editor.audio, editor.audio_formats, "a-low")
    assert editor.choice()["audio"] == "a-low"
    screenshot("selected-wide")
    app.window.set_default_size(390, 760)


def narrow():
    editor = state["editor"]
    for widget in (editor.primary, editor.audio, editor.apply, editor.summary, state["dialog"].download,
                   state["dialog"].paste_button):
        okay, bounds = widget.compute_bounds(app.window)
        assert okay and bounds.get_x() >= 0
        assert bounds.get_x() + bounds.get_width() <= app.window.get_width() + 1
    assert editor.child_focus(Gtk.DirectionType.TAB_FORWARD)
    screenshot("selected-narrow")
    editor.primary.grab_focus()
    key("space")


def popup():
    screenshot("searchable-choices", root=True)
    subprocess.run(["xdotool", "type", "--clearmodifiers", "av01"], check=True)


def search_result():
    screenshot("filtered-choices", root=True)
    key("Down")
    key("Return")


def apply():
    editor = state["editor"]
    assert not editor.closed
    assert not editor.primary.has_css_class("has-open-popup")
    assert editor.apply.grab_focus()
    key("Return")


def preserved():
    dialog = state["dialog"]
    screenshot("after-apply", root=True)
    assert state["editor"].closed
    choice = dialog.source_choice(dialog.entries[0][0])
    assert (choice["primary"], choice["audio"]) == ("v-high", "a-low")
    assert not dialog.quality.get_visible()
    assert "v-high" in dialog.source_row.get_subtitle()
    editor = state["editor"] = dialog.edit_formats(dialog.entries[0][0])
    assert editor.choice() == choice
    editor.audio.set_selected(0)
    assert "No audio" in editor.choice()["summary"]


def cancel_draft():
    state["editor"].cancel.grab_focus()
    key("Return")


def canceled():
    dialog = state["dialog"]
    assert dialog.source_choice(dialog.entries[0][0])["audio"] == "a-low"
    dialog.mode.set_selected(1)
    assert dialog.source_choice(dialog.entries[0][0]) is None
    editor = state["editor"] = dialog.edit_formats(dialog.entries[0][0])
    choose(editor.primary, editor.primary_formats, "a-high")
    assert not editor.audio.get_visible()
    editor.save()


def output_options():
    dialog = state["dialog"]
    dialog.more.set_expanded(True)
    assert state["editor"].closed
    assert app.window.get_visible_dialog() == dialog
    dialog.container.set_selected(3)
    assert dialog.processing.get_visible()
    assert dialog.format_error.get_visible()
    assert not dialog.download.get_sensitive()
    dialog.processing.grab_focus()


def output_reveal():
    reveal(state["dialog"].processing)


def output_conversion():
    dialog = state["dialog"]
    screenshot("remux-conflict")
    dialog.processing.set_selected(1)
    assert not dialog.format_error.get_visible()
    assert dialog.download.get_sensitive()
    assert dialog.conversion_note.get_visible()
    assert dialog.filename_row.get_subtitle().endswith(".webm")
    dialog.media_preferences.subtitles = True
    dialog.container.set_selected(5)
    assert "AVI cannot embed" in dialog.format_error.get_text()
    assert not dialog.download.get_sensitive()
    dialog.media_preferences.subtitles = False
    dialog.container.set_selected(0)
    assert dialog.processing.get_selected() == 0
    assert not dialog.processing.get_visible()
    dialog.mode.set_selected(1)
    dialog.audio_format.set_selected(5)
    assert dialog.filename_row.get_subtitle().endswith(".wav")
    dialog.audio_format.set_selected(6)
    assert dialog.filename_row.get_subtitle().endswith(".ogg")
    assert not dialog.processing.get_visible()
    dialog.mode.set_selected(0)
    dialog.container.set_selected(3)
    dialog.processing.set_selected(1)
    dialog.processing.grab_focus()


def output_layout():
    dialog = state["dialog"]
    for widget in (dialog.processing, dialog.conversion_note, dialog.download):
        okay, bounds = widget.compute_bounds(app.window)
        assert okay and bounds.get_x() >= 0
        assert bounds.get_x() + bounds.get_width() <= app.window.get_width() + 1
    assert Gtk.test_accessible_has_property(dialog.processing, Gtk.AccessibleProperty.LABEL)
    assert dialog.child_focus(Gtk.DirectionType.TAB_FORWARD)
    reveal(dialog.processing)


def output_capture():
    screenshot("conversion-narrow")


def enqueue():
    dialog = state["dialog"]
    assert dialog.source_choice(dialog.entries[0][0])["primary"] == "a-high"
    dialog.mode.set_selected(0)
    assert dialog.source_choice(dialog.entries[0][0])["primary"] == "v-high"
    dialog.enqueue()


def saved():
    assert state["dialog"].closed
    job = Store(store.directory).jobs[0]
    assert (job.source_format, job.source_audio) == ("v-high", "a-low")
    assert "1080x1920" in job.source_summary
    assert job.video_processing == "convert" and job.container == "webm"
    assert Store(store.directory).settings.video_processing == "convert"
    dialog = app.window.new_download("https://example.org/list")
    dialog.inspected([item("2", False), item("3", False)], None)
    state["dialog"] = dialog
    assert not dialog.source_row.get_visible()
    state["probe"] = patch("pyrobolic.app.inspect_url", side_effect=RuntimeError("Fixture probe failed")).start()
    state["editor"] = dialog.edit_formats(dialog.entries[0][0])


def failure():
    editor = state["editor"]
    assert "probe failed" in editor.error.get_text()
    assert not editor.apply.get_sensitive()
    assert editor.retry.get_visible()
    state["probe"].side_effect = None
    state["probe"].return_value = [item("2")]
    editor.load()


def retry():
    editor = state["editor"]
    assert editor.apply.get_sensitive()
    assert not editor.error.get_text()
    assert state["probe"].call_args.kwargs == {"single": True}
    choose(editor.primary, editor.primary_formats, "muxed")
    assert not editor.audio.get_visible()
    editor.save()


def playlist():
    dialog = state["dialog"]
    assert dialog.source_choice(dialog.entries[0][0])["primary"] == "muxed"
    assert dialog.source_choice(dialog.entries[1][0]) is None
    assert dialog.quality.get_visible()
    dialog.entries[1][1].set_active(False)
    assert not dialog.quality.get_visible()
    dialog.entries[1][1].set_active(True)
    assert dialog.quality.get_visible()
    def wait_for_cancel(_url, process, _directory, **_kwargs):
        assert process.cancelled.wait(5), "Probe was not canceled"
        raise InterruptedError("Canceled")
    state["probe"].side_effect = wait_for_cancel
    editor = state["editor"] = dialog.edit_formats(dialog.entries[1][0])
    state["process"] = editor.process


def cancel_inspection():
    assert app.window.get_visible_dialog() == state["editor"]
    key("Escape")


def canceled_probe():
    screenshot("canceled-inspection", root=True)
    assert state["process"].cancelled.is_set(), (state["editor"].closed, state["editor"].error.get_text(), app.window.get_focus())
    assert state["process"] not in app.window.processes
    assert state["dialog"].source_choice(state["dialog"].entries[1][0]) is None
    state["dialog"].enqueue()


def finish():
    assert state["dialog"].closed
    jobs = Store(store.directory).jobs
    assert [j.source_format for j in jobs] == ["v-high", "muxed", None]
    assert not app.window.processes
    app.window.closing = True
    app.window.actions.shutdown(wait=True)
    app.quit()


steps = iter([initial, automatic, narrow, popup, search_result, apply, preserved, cancel_draft, output_options,
              output_reveal, output_conversion, output_layout, output_capture, canceled, enqueue, saved,
              failure, retry, playlist, cancel_inspection, canceled_probe, finish])
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
patch.stopall()
temporary.cleanup()
if failures:
    print("\n".join(failures), file=sys.stderr)
    raise SystemExit(1)
print("Source format UI checks passed", flush=True)
