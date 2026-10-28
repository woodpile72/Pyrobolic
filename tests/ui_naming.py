# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Private-display filename interaction checks, with no network or real user state."""

from pathlib import Path
import subprocess
import sys
import tempfile
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pyrobolic.app import Application, GLib, Gtk
from pyrobolic.core import Store
from test_naming import FACEBOOK_NAME, FACEBOOK_TITLE
import gi

gi.require_version("GdkX11", "4.0")
from gi.repository import GdkX11


temporary = tempfile.TemporaryDirectory()
store = Store(Path(temporary.name) / "state")
store.settings.output = str(Path(temporary.name) / "media")
store.paused = True
app = Application(store)
state = {}
failures = []
output = Path(__file__).resolve().parents[1] / ".qa"
output.mkdir(exist_ok=True)


def screenshot(name):
    xid = GdkX11.X11Surface.get_xid(app.window.get_surface())
    subprocess.run(["import", "-window", str(xid), str(output / f"{name}.png")], check=True)


def item(title=FACEBOOK_TITLE, suffix="1"):
    return {"title": title, "url": "https://www.facebook.com/" + suffix,
            "media_id": suffix, "source": "Facebook", "duration": 90}


def single():
    app.window.queue.stopped = True
    app.window.set_default_size(880, 760)
    dialog = app.window.new_download("https://www.facebook.com/1")
    dialog.inspected([item()], None)
    state["dialog"] = dialog
    assert dialog.name.get_visible()
    assert dialog.name.get_text() == FACEBOOK_NAME
    assert dialog.include_id.get_active()
    assert dialog.filename_row.get_subtitle() == FACEBOOK_NAME + " [1]"
    assert "automatic extension" in dialog.filename_row.get_title()


def edit_single():
    row = state["dialog"].item_rows[0]
    assert row.get_allocated_height() >= 84, row.get_allocated_height()
    screenshot("filename-single-wide")
    dialog = state["dialog"]
    dialog.name.set_text("../outside")
    assert not dialog.download.get_sensitive()
    assert dialog.name_error.get_visible()
    dialog.name.set_text("100% Concert 2026")
    dialog.include_id.set_active(False)
    dialog.container.set_selected(1)
    assert dialog.filename_row.get_subtitle() == "100% Concert 2026.mp4"
    assert dialog.download.get_sensitive()
    dialog.mode.set_selected(1)
    dialog.audio_format.set_selected(3)
    assert dialog.filename_row.get_subtitle() == "100% Concert 2026.opus"
    dialog.mode.set_selected(0)
    dialog.reset_name()
    assert dialog.name.get_text() == FACEBOOK_NAME
    dialog.name.set_text("Concert 2026")
    dialog.set_focus(dialog.name)
    app.window.set_default_size(390, 760)


def long_media_name():
    dialog = state["dialog"]
    dialog.name.set_text("A live concert recording with a longer title that needs several lines in a narrow window without scrolling inside the media summary")


def media_summary_fits():
    dialog = state["dialog"]
    assert dialog.item_scroll.get_policy()[1] == Gtk.PolicyType.NEVER
    assert dialog.item_rows[0].get_allocated_height() >= 84
    adjustment = dialog.item_scroll.get_vadjustment()
    assert adjustment.get_upper() <= adjustment.get_page_size() + 1
    screenshot("media-summary-narrow")
    dialog.name.set_text("Concert 2026")
    dialog.more.set_expanded(True)


def narrow():
    dialog = state["dialog"]
    assert app.window.get_width() <= 400
    screenshot("filename-single-narrow")
    for widget in (dialog.name, dialog.include_id, dialog.download, dialog.filename_row):
        okay, bounds = widget.compute_bounds(app.window)
        assert okay and bounds.get_x() >= 0
        assert bounds.get_x() + bounds.get_width() <= app.window.get_width()
    assert dialog.child_focus(Gtk.DirectionType.TAB_FORWARD)
    dialog.download.emit("clicked")


def submitted():
    assert state["dialog"].closed
    assert len(store.jobs) == 1
    job = store.jobs[0]
    assert job.filename == "Concert 2026"
    assert job.output_stem == "Concert 2026"
    assert not job.include_id
    assert not Store(store.directory).settings.include_id
    dialog = app.window.new_download("https://www.facebook.com/2")
    dialog.inspected([item("First concert", "2"), item("Second concert", "3")], None)
    state["dialog"] = dialog
    assert not dialog.name.get_visible()
    assert dialog.item_scroll.get_policy()[1] == Gtk.PolicyType.AUTOMATIC
    assert dialog.item_scroll.get_max_content_height() == 220
    assert not dialog.include_id.get_active()
    state["editor"] = dialog.edit_name(dialog.entries[1][0])


def playlist_editor():
    editor = state["editor"]
    editor.name.set_text("")
    assert not editor.done.get_sensitive()
    editor.name.set_text("Second concert - encore")
    assert editor.done.get_sensitive()
    editor.set_focus(editor.name)


def save_playlist_name():
    editor = state["editor"]
    screenshot("filename-playlist-editor-narrow")
    editor.name.emit("entry-activated")


def playlist_edited():
    dialog = state["dialog"]
    assert dialog.entries[1][0]["filename"] == "Second concert - encore"
    assert dialog.item_rows[1].get_subtitle() == "Second concert - encore.mp4"
    dialog.item_search.set_text("encore")
    dialog.filter_items()
    assert not dialog.item_rows[0].get_visible()
    assert dialog.item_rows[1].get_visible()
    assert all(check.get_active() for _, check in dialog.entries)
    state["editor"] = dialog.edit_name(dialog.entries[1][0])
    state["editor"].name.set_text("Do not save this")
    state["editor"].force_close()
    dialog.include_id.set_active(True)
    assert "[3]" in dialog.item_rows[1].get_subtitle()


def canceled_editor():
    dialog = state["dialog"]
    assert dialog.entries[1][0]["filename"] == "Second concert - encore"
    dialog.item_search.set_text("")
    dialog.filter_items()
    screenshot("filename-playlist-narrow")
    dialog.force_close()
    assert not store.settings.include_id
    app.window.closing = True
    app.window.actions.shutdown(wait=True)
    app.quit()


steps = iter([single, edit_single, long_media_name, media_summary_fits, narrow, submitted,
              playlist_editor, save_playlist_name, playlist_edited, canceled_editor])


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


app.connect("activate", lambda *_: GLib.timeout_add(800, step))
app.run([])
temporary.cleanup()
if failures:
    print("\n".join(failures), file=sys.stderr)
    raise SystemExit(1)
print("Filename UI checks passed", flush=True)
