# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Bounded HIG checks using real key events on a private Xvfb display."""

from pathlib import Path
import os
import subprocess
import sys
import tempfile
import traceback
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pyrobolic.app import Application, Adw, GLib, Gtk, display_title, job_summary
from pyrobolic.core import Job, Store
import gi
gi.require_version("GdkX11", "4.0")
from gi.repository import GdkX11

temporary = tempfile.TemporaryDirectory()
directory = Path(temporary.name)
store = Store(directory / "state")
store.settings.notifications = False
store.settings.output = str(directory / "configured")
store.jobs = [Job(f"https://example.org/{i}", f"History recording {i}", str(directory / "actual"),
                  status="Completed", detail="Download complete", created=i,
                  completed_at=1700000000 + i, total_bytes=1234567) for i in range(10000)]
special = store.jobs[-1]
special.title = "12M views | Original source caption with a long account of the concert and all its details"
special.filename = "Concert in Bern - Barcelona Big Blues Band"
special.output_stem = special.filename
media = directory / "actual" / "Existing filename.mp4"
media.parent.mkdir()
media.write_bytes(b"original file, never renamed")
special.files = [str(media)]
active = Job("https://example.org/active", "Active download", str(directory), status="Downloading", created=11000)
store.jobs.append(active)
paths_before = special.files[:]
app = Application(store)
state, failures = {}, []
output = Path(__file__).resolve().parents[1] / ".qa"
prefix = "hig-hc" if os.environ.get("ADW_DEBUG_HIGH_CONTRAST") == "1" else "hig"


def xid():
    return str(GdkX11.X11Surface.get_xid(app.window.get_surface()))


def key(value):
    subprocess.run(["xdotool", "windowfocus", "--sync", xid(), "key", "--clearmodifiers", value], check=True)


def screenshot(name, root=False):
    subprocess.run(["import", "-window", "root" if root else xid(), str(output / f"{prefix}-{name}.png")],
                   check=True, timeout=10)


def descendants(widget):
    yield widget
    child = widget.get_first_child()
    while child:
        yield from descendants(child)
        child = child.get_next_sibling()


def bounds_fit(widget):
    okay, bounds = widget.compute_bounds(app.window)
    assert okay and bounds.get_x() >= 0, (widget, bounds.get_x())
    assert bounds.get_x() + bounds.get_width() <= app.window.get_width() + 1, (
        widget, bounds.get_x(), bounds.get_width(), app.window.get_width(),
        app.window.get_allocation().width, app.window.get_size_request(),
        app.window.bottom_switcher.measure(Gtk.Orientation.HORIZONTAL, -1))
    return bounds


def initial():
    app.window.queue.stopped = True
    app.window.set_default_size(880, 700)
    assert len(app.window.rows) == 101
    assert job_summary(Job("https://example.org", "Legacy", "/tmp", status="Completed")) == "Completed"
    assert "Download complete" not in job_summary(special)
    assert "MB" in job_summary(special)
    assert display_title(special) == special.filename
    if prefix == "hig-hc":
        assert Adw.StyleManager.get_default().get_high_contrast()
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
    state["open"] = patch.object(app.window, "open_folder").start()
    key("ctrl+2")


def history():
    assert app.window.stack.get_visible_child_name() == "history"
    row = app.window.rows[special.id]
    assert row.title.get_text() == special.filename
    assert row.subtitle.get_text().startswith("Completed")
    assert "Download complete" not in row.subtitle.get_text()
    folder = bounds_fit(app.window.folder_button)
    menu = bounds_fit(app.window.menu_button)
    assert folder.get_x() + folder.get_width() <= menu.get_x()
    for widget in (app.window.new_button, app.window.pause_button, app.window.folder_button,
                   app.window.menu_button, app.window.search, row.command, row.details):
        assert Gtk.test_accessible_has_property(widget, Gtk.AccessibleProperty.LABEL), widget
    row.command.grab_focus()
    key("space")


def item_folder():
    state["open"].assert_called_once_with(special.output)
    key("ctrl+shift+o")


def configured_folder():
    state["open"].assert_called_with(store.settings.output)
    row = app.window.rows[special.id]
    row.details.grab_focus()
    key("space")


def details():
    dialog = app.window.get_visible_dialog()
    assert dialog.get_title() == "Download Details"
    labels = [w for w in descendants(dialog) if isinstance(w, Gtk.Label)]
    assert any(w.get_text() == special.filename and w.get_selectable() and w.get_wrap() for w in labels)
    assert any(w.get_text() == special.title and w.get_selectable() and w.get_wrap() for w in labels)
    saved = next(w for w in labels if str(media) in w.get_text() and w.get_selectable())
    assert saved.get_selectable() and saved.get_wrap() and saved.get_height() > 0
    bounds_fit(saved)
    screenshot("details-light")
    key("Escape")


def search():
    assert app.window.get_visible_dialog() is None
    key("ctrl+f")


def search_focused():
    focus = app.window.get_focus()
    assert focus == app.window.search or focus.is_ancestor(app.window.search)
    key("F10")


def menu():
    assert app.window.menu_button.get_popover().get_visible()
    screenshot("menu-light", root=True)
    key("Escape")


def scrolled_history():
    assert not app.window.menu_button.get_popover().get_visible()
    app.window.rows[store.jobs[-15].id].details.grab_focus()
    app.window.history_scroll.get_vadjustment().set_value(850)


def capture_anchor():
    anchor = app.window.history_anchor()
    assert anchor
    state["anchor"] = anchor
    state["focus"] = app.window.get_focus()
    state["row_ids"] = {identifier: id(row) for identifier, row in app.window.rows.items()}
    state["updates"] = 0
    for row in app.window.rows.values():
        if row.job.status == "Completed":
            original = row.update
            def update(job, original=original):
                state["updates"] += 1
                return original(job)
            row.update = update
    active.progress = .5
    app.window.queue._notify(persist=False, job=active)


def progress_stable():
    assert app.window.get_focus() == state["focus"]
    assert state["updates"] == 0
    assert app.window.history_anchor()[:2] == state["anchor"][:2]
    active.status, active.detail, active.progress = "Completed", "Download complete", 1
    active.completed_at, active.total_bytes = 1700020000, 9999
    app.window.queue._notify(persist=False)


def completion_stable():
    assert app.window.get_focus() == state["focus"]
    assert state["updates"] == 0
    before = state["anchor"]
    after = app.window.history_anchor()
    assert after[0] == before[0] and abs(after[1] - before[1]) < 1, (before, after)
    for identifier, row_id in state["row_ids"].items():
        if identifier in app.window.rows:
            assert id(app.window.rows[identifier]) == row_id
    app.window.history_scroll.get_vadjustment().set_value(0)
    screenshot("history-light")
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
    app.window.set_default_size(390, 760)


def narrow_dark():
    assert app.window.get_width() <= 400
    assert app.window.bottom_switcher.get_reveal()
    for widget in (app.window.folder_button, app.window.menu_button, app.window.bottom_switcher):
        bounds_fit(widget)
    screenshot("history-dark-narrow")
    Gtk.Settings.get_default().set_property("gtk-font-name", "Sans 20")


def large_text():
    assert app.window.bottom_switcher.get_reveal()
    screenshot("history-large-text")
    for widget in (app.window.folder_button, app.window.menu_button, app.window.bottom_switcher):
        bounds_fit(widget)
    screenshot("history-large-text")
    key("ctrl+1")


def large_downloads():
    assert app.window.stack.get_visible_child_name() == "downloads"
    key("ctrl+n")


def new_download():
    assert app.window.get_visible_dialog().get_title() == "New Download"
    bounds_fit(app.window.download_dialog.download)
    bounds_fit(app.window.download_dialog.paste_button)
    screenshot("new-download-large-text")
    key("Escape")


def finish():
    assert special.files == paths_before
    assert media.read_bytes() == b"original file, never renamed"
    assert list(media.parent.iterdir()) == [media]
    app.window.closing = True
    app.window.actions.shutdown(wait=True)
    app.quit()


steps = iter([initial, history, item_folder, configured_folder, details, search, search_focused, menu,
              scrolled_history, capture_anchor, progress_stable, completion_stable, narrow_dark,
              large_text, large_downloads, new_download, finish])
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
patch.stopall()
temporary.cleanup()
if failures:
    print("\n".join(failures), file=sys.stderr)
    raise SystemExit(1)
print("HIG interaction checks passed:", prefix, flush=True)
