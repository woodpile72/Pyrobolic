# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Synthetic per-entry batch editing on a private display; no source network access."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time
import traceback
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pyrobolic.app import Adw, Application, GLib, Gtk
from pyrobolic.batch import BatchInspection, parse_batch
from pyrobolic.core import Store
import gi
gi.require_version("GdkX11", "4.0")
from gi.repository import GdkX11

temporary = tempfile.TemporaryDirectory()
root = Path(temporary.name)
store = Store(root / "state")
store.settings.output = str(root / "media")
store.settings.notifications = False
store.paused = True
app = Application(store)
state, failures, calls = {}, [], []
qa = Path(__file__).resolve().parents[1] / ".qa"
qa.mkdir(exist_ok=True)
hc = bool(os.environ.get("ADW_DEBUG_HIGH_CONTRAST"))
run = BatchInspection.run


def fixture(url, process, *_):
    calls.append(url)
    if url.endswith("bad") and not state.get("recovered"):
        raise RuntimeError("Synthetic source failure")
    return [{"url": url, "title": "Source " + url.rsplit("/", 1)[-1], "media_id": "1"}]


mock = patch.object(BatchInspection, "run", lambda self, entries, directory, progress:
                    run(self, entries, directory, progress, inspect=fixture))
mock.start()


def key(widget, value):
    assert widget.grab_focus()
    xid = GdkX11.X11Surface.get_xid(app.window.get_surface())
    subprocess.run(["xdotool", "windowfocus", "--sync", str(xid), "key", "--clearmodifiers", value], check=True)


def bounds(widget):
    okay, rect = widget.compute_bounds(app.window)
    assert okay and rect.get_x() >= -1
    assert rect.get_x() + rect.get_width() <= app.window.get_width() + 1, rect


def screenshot(name):
    xid = GdkX11.X11Surface.get_xid(app.window.get_surface())
    subprocess.run(["import", "-window", str(xid), str(qa / f"batch-edit{'-hc' if hc else ''}-{name}.png")], check=True)


def start():
    app.window.queue.stopped = True
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT if hc else Adw.ColorScheme.FORCE_DARK)
    if hc:
        Gtk.Settings.get_default().set_property("gtk-font-name", "Sans 20")
    app.window.set_default_size(420 if hc else 390, 760)
    state["batch"] = app.window.new_batch(
        f"# Keep this comment\nhttps://example.org/ok\tChosen\t{root}/custom\n"
        f"invalid\tRepair me\t{root}/repaired\nhttps://example.org/bad")


def inspect():
    key(state["batch"].primary, "space")


def results():
    dialog = state["batch"]
    if dialog.worker:
        return False
    assert len(calls) == 2 and not store.jobs
    assert dialog.entries[1].name == "Repair me"
    state["rows"] = dict(dialog.rows)
    state["original"] = dialog.entries[0]
    for row in dialog.rows.values():
        assert Gtk.test_accessible_has_property(row.edit_button, Gtk.AccessibleProperty.LABEL)
        bounds(row.edit_button)
    key(dialog.rows[3].edit_button, "space")


def cancel_draft():
    editor = state["editor"] = app.window.get_visible_dialog()
    assert editor.entry is state["batch"].entries[1]
    assert editor.name.get_text() == "Repair me"
    assert not editor.apply.get_sensitive()
    editor.url.set_text("https://example.org/discarded")
    editor.name.set_text("Discarded")
    assert editor.apply.get_sensitive()
    screenshot("draft-narrow")
    key(editor.cancel, "space")


def canceled():
    dialog = state["batch"]
    assert dialog.entries[1].url == "invalid" and dialog.entries[1].name == "Repair me"
    assert len(calls) == 2 and not store.jobs
    key(dialog.rows[3].edit_button, "space")


def validate():
    editor = state["editor"] = app.window.get_visible_dialog()
    editor.url.set_text("https://example.org/ok")
    editor.name.set_text("Chosen")
    editor.destination.set_text(str(root / "custom"))
    assert not editor.apply.get_sensitive() and "Duplicate" in editor.error.get_text()
    editor.url.set_text("https://example.org/fixed")
    editor.name.set_text("../bad")
    assert not editor.apply.get_sensitive()
    editor.name.set_text("Repaired")
    editor.destination.set_text("relative")
    assert not editor.apply.get_sensitive()
    editor.destination.set_text(str(root / "repaired"))
    assert editor.apply.get_sensitive()
    for widget in (editor.url, editor.name, editor.destination, editor.apply, editor.browse, editor.reset):
        bounds(widget)
        if widget is not editor.apply:
            assert Gtk.test_accessible_has_property(widget, Gtk.AccessibleProperty.LABEL)


def navigate():
    key(state["editor"].name, "Tab")


def valid_snapshot():
    editor = state["editor"]
    assert app.window.get_focus() is not None
    screenshot("valid-narrow")
    key(editor.apply, "space")


def corrected():
    dialog = state["batch"]
    assert dialog.entries[0] is state["original"] and dialog.entries[0].items
    assert all(dialog.rows[line] is row for line, row in state["rows"].items())
    focus = app.window.get_focus()
    assert focus is dialog.rows[3].edit_button or focus.is_ancestor(dialog.rows[3].edit_button)
    assert dialog.entries[1].url.endswith("fixed") and not dialog.entries[1].inspected
    assert "1 not inspected" in dialog.status.get_text()
    assert len(calls) == 2
    buffer = dialog.text.get_buffer()
    text = buffer.get_text(*buffer.get_bounds(), True)
    assert text.startswith("# Keep this comment\n")
    assert "https://example.org/fixed\tRepaired" in text
    screenshot("corrected-narrow")
    state["recovered"] = True
    key(dialog.retry, "space")


def retried():
    dialog = state["batch"]
    if dialog.worker:
        return False
    assert calls.count("https://example.org/ok") == 1
    assert calls.count("https://example.org/fixed") == 1
    assert calls.count("https://example.org/bad") == 2
    assert "3 items ready" in dialog.status.get_text()
    state["count"] = len(calls)
    key(dialog.rows[2].edit_button, "space")


def metadata():
    editor = state["editor"] = app.window.get_visible_dialog()
    editor.name.set_text("Renamed")
    key(editor.reset, "space")


def metadata_reset():
    editor = state["editor"]
    assert editor.destination.get_text() == ""
    key(editor.apply, "space")


def metadata_applied():
    dialog = state["batch"]
    assert len(calls) == state["count"]
    item = dialog.entries[0].items[0]
    assert item["import_name"] == "Renamed" and "destination" not in item
    assert dialog.entries[0].inspected and "3 items ready" in dialog.status.get_text()
    key(dialog.primary, "space")


def submit():
    dialog = state["review"] = app.window.download_dialog
    assert [item["filename"] for item, _ in dialog.entries] == ["Renamed", "Repaired", "Source bad"]
    dialog.include_id.set_active(False)
    key(dialog.download, "space")


def saved():
    if state["review"].saving:
        return False
    assert len(store.jobs) == 3
    saved = Store(store.directory)
    assert [j.filename for j in saved.jobs] == ["Renamed", "Repaired", "Source bad"]
    assert saved.jobs[0].output == store.settings.output
    assert saved.jobs[1].output == str(root / "repaired")
    state["batch"] = app.window.new_batch("https://example.org/single")
    state["batch"].inspect()


def single():
    dialog = state["batch"]
    if dialog.worker:
        return False
    assert dialog.primary.get_sensitive()
    state["editor"] = dialog.edit_entry(dialog.entries[0])
    state["editor"].url.set_text("https://example.org/changed")


def invalidate():
    key(state["editor"].apply, "space")


def invalidated():
    dialog = state["batch"]
    assert not dialog.primary.get_sensitive() and dialog.retry.get_sensitive()
    assert not dialog.entries[0].items and not dialog.entries[0].inspected
    assert "https://example.org/changed" not in calls
    dialog.edit()
    buffer = dialog.text.get_buffer()
    assert buffer.get_text(*buffer.get_bounds(), True) == "https://example.org/changed"
    assert dialog.primary.get_sensitive()
    dialog.force_close()
    state["batch"] = app.window.new_batch("\n".join(f"https://example.org/{i}" for i in range(105)))
    dialog = state["batch"]
    dialog.entries = parse_batch("\n".join(f"https://example.org/{i}" for i in range(105)))
    for entry in dialog.entries:
        entry.inspected = True
        entry.items = [{"url": entry.url, "title": str(entry.line)}]
    dialog.input_box.set_visible(False)
    dialog.inspected(None)
    dialog.change_page(1)
    state["rows"] = dict(dialog.rows)


def page_edit():
    dialog = state["batch"]
    adjustment = dialog.results_scroll.get_vadjustment()
    adjustment.set_value(30)
    state["scroll"] = adjustment.get_value()
    state["editor"] = dialog.edit_entry(dialog.entries[101])
    state["editor"].name.set_text("Page two")


def page_apply():
    key(state["editor"].apply, "space")


def paged():
    dialog = state["batch"]
    assert dialog.page == 1 and len(dialog.rows) == 5
    assert all(dialog.rows[line] is row for line, row in state["rows"].items())
    assert dialog.results_scroll.get_vadjustment().get_value() == state["scroll"]
    assert dialog.entries[101].items[0]["import_name"] == "Page two"
    assert "Page two" in dialog.rows[102].get_title()
    dialog.force_close()
    app.window.queue.shutdown()
    app.window.actions.shutdown(wait=True)
    app.quit()


steps = iter([start, inspect, results, cancel_draft, canceled, validate, navigate, valid_snapshot, corrected, retried,
              metadata, metadata_reset, metadata_applied, submit, saved, single, invalidate, invalidated, page_edit, page_apply, paged])
current, deadline = next(steps), time.monotonic() + 15


def advance():
    global current, deadline
    try:
        assert time.monotonic() < deadline, f"Timed out in {current.__name__}"
        if current() is not False:
            current = next(steps)
            deadline = time.monotonic() + 15
    except StopIteration:
        return False
    except Exception:
        failures.append(traceback.format_exc())
        for worker in list(app.window.processes):
            worker.cancel()
            worker.close()
        app.window.queue.shutdown()
        app.window.actions.shutdown(wait=True)
        app.quit()
        return False
    return True


GLib.timeout_add(700, advance)
app.run([])
mock.stop()
temporary.cleanup()
if failures:
    print("\n".join(failures))
    raise SystemExit(1)
print("Batch entry editing UI checks passed")
