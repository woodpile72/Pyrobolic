# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Private-display bulk workflow, paging, drafts and keyboard checks."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import traceback
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pyrobolic.app import Adw, Application, GLib, Gtk
from pyrobolic.batch import BatchInspection
from pyrobolic.core import Store
import gi
gi.require_version("GdkX11", "4.0")
from gi.repository import GdkX11

temporary = tempfile.TemporaryDirectory()
root = Path(temporary.name)
store = Store(root / "state")
store.settings.output = str(root / "media")
store.paused = True
app = Application(store)
state, failures, calls = {}, [], []
qa = Path(__file__).resolve().parents[1] / ".qa"
qa.mkdir(exist_ok=True)
hc = bool(os.environ.get("ADW_DEBUG_HIGH_CONTRAST"))
original_run = BatchInspection.run


def fixture(url, process, *_):
    calls.append(url)
    if url.endswith("stop"):
        process.cancelled.wait(5)
        raise InterruptedError("Canceled")
    if url.endswith("bad") and not state.get("recovered"):
        raise RuntimeError("Fixture unavailable")
    return [{"url": url, "title": "Source " + url.rsplit("/", 1)[-1], "media_id": "1"}]


def run(self, entries, directory, progress):
    return original_run(self, entries, directory, progress, inspect=fixture)


mock = patch.object(BatchInspection, "run", run)
mock.start()


def screenshot(name):
    xid = GdkX11.X11Surface.get_xid(app.window.get_surface())
    subprocess.run(["import", "-window", str(xid), str(qa / f"batch{'-hc' if hc else ''}-{name}.png")], check=True)


def key(widget, value):
    widget.grab_focus()
    xid = GdkX11.X11Surface.get_xid(app.window.get_surface())
    subprocess.run(["xdotool", "windowfocus", "--sync", str(xid), "key", "--clearmodifiers", value], check=True)


def bounds(widget):
    okay, rect = widget.compute_bounds(app.window)
    assert okay and rect.get_x() >= -1
    assert rect.get_x() + rect.get_width() <= app.window.get_width() + 1, rect


def start():
    app.window.queue.stopped = True
    if hc:
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        Gtk.Settings.get_default().set_property("gtk-font-name", "Sans 20")
    else:
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
    app.window.set_default_size(880, 760)
    app.activate_action("import-downloads", None)
    dialog = state["batch"] = app.window.batch_dialog
    assert not dialog.primary.get_sensitive()
    path = root / "input.txt"
    path.write_text(f"https://example.org/ok\tMy concert\t{root}/custom\nhttps://example.org/bad\nnot-a-url")
    dialog.load_file(path)


def inspect():
    dialog = state["batch"]
    assert dialog.primary.get_sensitive()
    assert not calls, "Reading text must not start network inspection"
    assert Gtk.test_accessible_has_property(dialog.text, Gtk.AccessibleProperty.LABEL)
    screenshot("input-wide")
    key(dialog.primary, "space")


def errors():
    dialog = state["batch"]
    assert dialog.worker is None
    assert len(calls) == 2
    assert dialog.entries[0].items and dialog.entries[1].error and dialog.entries[2].error
    assert not store.jobs
    screenshot("errors-wide")
    app.window.set_default_size(420 if hc else 390, 760)


def retry():
    dialog = state["batch"]
    for widget in (dialog.primary, dialog.retry, dialog.previous, dialog.next, dialog.page_label):
        bounds(widget)
        if isinstance(widget, Gtk.Button) and widget is not dialog.primary:
            assert Gtk.test_accessible_has_property(widget, Gtk.AccessibleProperty.LABEL)
    screenshot("errors-narrow")
    state["recovered"] = True
    key(dialog.retry, "space")


def review():
    dialog = state["batch"]
    assert len(calls) == 3 and calls.count("https://example.org/ok") == 1
    assert "2 items ready" in dialog.status.get_text()
    key(dialog.primary, "space")


def options():
    dialog = state["dialog"] = app.window.download_dialog
    assert len(dialog.entries) == 2 and dialog.is_import
    assert dialog.entries[0][0]["filename"] == "My concert"
    assert dialog.entries[0][0]["destination"] == str(root / "custom")
    dialog.include_id.set_active(False)
    dialog.container.set_selected(1)
    dialog.order.set_selected(1)
    dialog.number_items.set_active(True)
    assert dialog.filename_preview(dialog.entries[0][0]) == "01 - Source bad.mp4"
    assert dialog.filename_preview(dialog.entries[1][0]) == "02 - My concert.mp4"
    dialog.entries[1][0]["source_choices"] = {"video": {"primary": "chosen", "audio": None, "summary": "Chosen"}}
    dialog.item_search.set_text("My concert")
    dialog.filter_items()
    assert "1 hidden by search" in dialog.items_group.get_description()
    dialog.select_all.set_active(False)
    assert dialog.entries[0][1].get_active() and not dialog.entries[1][1].get_active()
    dialog.select_all.set_active(True)
    dialog.item_search.set_text("")
    dialog.filter_items()
    state["editor"] = dialog.edit_name(dialog.entries[1][0])


def edit_cancel():
    editor = state["editor"]
    assert editor.folder.get_subtitle() == str(root / "custom")
    editor.name.set_text("Discarded")
    editor.destination = str(root / "discarded")
    editor.force_close()


def edit_apply():
    dialog = state["dialog"]
    item = dialog.entries[1][0]
    assert item["filename"] == "My concert" and item["destination"] == str(root / "custom")
    editor = state["editor"] = dialog.edit_name(item)
    editor.name.set_text("Kept name")
    editor.destination = str(root / "kept")
    editor.folder.set_subtitle(editor.destination)


def apply_name():
    key(state["editor"].done, "space")
    app.window.set_default_size(420 if hc else 390, 760)


def submit():
    dialog = state["dialog"]
    assert dialog.filename_preview(dialog.entries[1][0]) == "02 - Kept name.mp4"
    for widget in (dialog.download, dialog.order, dialog.item_search, dialog.number_items):
        bounds(widget)
    assert dialog.item_scroll.get_height() >= 220
    screenshot("review-narrow")
    key(dialog.download, "space")


def saved():
    assert len(store.jobs) == 2, (len(store.jobs), state["dialog"].message.get_text(),
                                  state["dialog"].download.get_sensitive(), app.window.get_visible_dialog())
    loaded = Store(store.directory)
    assert [j.filename for j in loaded.jobs] == ["01 - Source bad", "02 - Kept name"]
    assert loaded.jobs[1].output == str(root / "kept")
    assert loaded.jobs[1].source_format == "chosen"
    assert loaded.jobs[0].source_format is None
    assert loaded.settings.output == str(root / "media")
    dialog = state["large"] = app.window.new_download("https://example.org/playlist")
    dialog.inspected([{"title": f"Item {i:04}", "url": f"https://example.org/{i}", "media_id": str(i)}
                      for i in range(1000)], None)
    assert len(dialog.entries) == 1000
    assert sum(row is not None for row in dialog.item_rows) == 100
    assert dialog.items.observe_children().get_n_items() == 100
    dialog.entries[0][1].set_active(False)
    dialog.number_items.set_active(True)


def next_page():
    key(state["large"].item_next, "space")


def large_page():
    dialog = state["large"]
    assert dialog.item_page == 1
    assert dialog.item_rows[0] is None and dialog.item_rows[100] is not None
    assert dialog.filename_preview(dialog.entries[100][0]).startswith("100 - Item 0100")
    assert not dialog.entries[0][1].get_active()
    dialog.item_search.set_text("0999")
    dialog.filter_items()
    assert dialog.items.observe_children().get_n_items() == 1
    assert "998 hidden by search" in dialog.items_group.get_description()
    dialog.select_all.set_active(False)
    assert not dialog.entries[-1][1].get_active()
    dialog.item_search.set_text("")
    dialog.filter_items()
    dialog.order.set_selected(1)


def large_reversed():
    dialog = state["large"]
    assert dialog.entries[0][0]["title"] == "Item 0999"
    assert not dialog.entries[0][1].get_active()
    assert not dialog.entries[-1][1].get_active()
    assert dialog.filename_preview(dialog.entries[1][0]).startswith("001 - Item 0998")
    for widget in (dialog.item_next, dialog.item_previous, dialog.item_page_label):
        bounds(widget)
    assert dialog.item_scroll.get_height() >= 220
    screenshot("large-narrow")
    state["row"] = dialog.item_rows[1]
    state["adjustment"] = dialog.item_scroll.get_vadjustment().get_value()
    dialog.entries[2][1].set_active(False)
    assert dialog.item_rows[1] is state["row"]
    assert dialog.item_scroll.get_vadjustment().get_value() == state["adjustment"]
    dialog.force_close()


def cancel_setup():
    dialog = state["cancel"] = app.window.new_batch("https://example.org/stop\nhttps://example.org/not-started")
    assert not dialog.entries


def cancel_start():
    key(state["cancel"].primary, "space")


def cancel_running():
    dialog = state["cancel"]
    assert dialog.worker in app.window.processes
    state["worker"] = dialog.worker
    dialog.force_close()


def cancel_done():
    assert state["worker"].done.is_set()
    assert state["worker"] not in app.window.processes
    assert "https://example.org/not-started" not in calls
    assert len(store.jobs) == 2


def finish():
    assert len(store.jobs) == 2
    app.window.queue.shutdown()
    app.window.actions.shutdown(wait=True)
    app.quit()


steps = iter([start, inspect, errors, retry, review, options, edit_cancel, edit_apply, apply_name, submit, saved,
              next_page, large_page, large_reversed, cancel_setup, cancel_start, cancel_running, cancel_done, finish])


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
mock.stop()
temporary.cleanup()
if failures:
    print("\n".join(failures))
    raise SystemExit(1)
print("Batch UI checks passed")
