# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Private-display tests for transfer drafts, ordering, mixed subtitles and URL handoff."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import traceback
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pyrobolic.app import Application, Adw, Gio, GLib, Gtk
from pyrobolic.core import Store
import gi
gi.require_version("GdkX11", "4.0")
from gi.repository import GdkX11

temp = tempfile.TemporaryDirectory()
store = Store(Path(temp.name) / "state")
store.settings.output = str(Path(temp.name) / "media")
store.paused = True
app = Application(store)
state, failures = {}, []
hc = bool(os.environ.get("ADW_DEBUG_HIGH_CONTRAST"))
qa = Path(__file__).resolve().parents[1] / ".qa"
qa.mkdir(exist_ok=True)


def key(value):
    subprocess.run(["xdotool", "key", "--clearmodifiers", value], check=True)


def shot(name):
    xid = GdkX11.X11Surface.get_xid(app.window.get_surface())
    subprocess.run(["import", "-window", str(xid), str(qa / f"expansion{'-hc' if hc else ''}-{name}.png")], check=True)


def initial():
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT if hc else Adw.ColorScheme.FORCE_DARK)
    if hc:
        Gtk.Settings.get_default().set_property("gtk-font-name", "Sans 20")
    app.window.set_default_size(440, 850)
    app.window.queue.stopped = True
    parent = state["parent"] = app.window.new_download("https://example.org/fixture")
    parent.inspected([{"title": f"Fixture {i}", "url": f"https://example.org/{i}", "media_id": str(i),
                       "formats_loaded": i != 3, "subtitle_tracks": ([{"code": "en", "name": "English", "source": "manual"}] if i == 1 else [])}
                      for i in (1, 2, 3)], None)
    state["transfer"] = parent.edit_transfer()


def edit_cancel():
    editor = state["transfer"]
    editor.rate.set_value(512)
    editor.proxy.set_text("http://user:password@localhost:8000")
    assert not editor.apply.get_sensitive()
    assert Gtk.test_accessible_has_property(editor.proxy, Gtk.AccessibleProperty.LABEL)
    shot("transfer-invalid")
    editor.cancel.grab_focus()
    key("Return")


def reopen_transfer():
    assert state["parent"].media_preferences.rate_limit_kib == 0
    state["transfer"] = state["parent"].edit_transfer()


def apply_transfer():
    editor = state["transfer"]
    editor.rate.set_value(256)
    editor.fragments.set_value(3)
    editor.proxy.set_text("http://127.0.0.1:8080")
    assert editor.apply.get_sensitive()
    editor.apply.grab_focus()
    key("Return")


def order():
    parent = state["parent"]
    assert parent.media_preferences.rate_limit_kib == 256
    assert store.settings.rate_limit_kib == 0
    state["first"] = parent.entries[0][0]
    state["filename"] = parent.edit_name(state["first"])


def reorder():
    editor = state["filename"]
    editor.position.set_value(3)
    editor.done.grab_focus()
    key("Return")


def subtitles():
    parent = state["parent"]
    assert parent.entries[2][0] is state["first"]
    assert parent.order.get_selected() == 2
    state["subtitles"] = parent.edit_preferences(True)


def inspect_subtitles():
    editor = state["subtitles"]
    assert "2 of 3" in editor.availability.get_text()
    assert "1/3 items" in editor.available.get_model().get_string(0)
    assert editor.check_languages.get_visible()
    editor.rows["subtitle_mode"].set_selected(1)
    assert "en: reported on 1 of 2 checked items; missing on 1." in editor.coverage.get_text()
    assert "Unchecked items: 1" in editor.coverage.get_text()
    assert Gtk.test_accessible_has_property(editor.coverage, Gtk.AccessibleProperty.LABEL)
    state["calls"] = []
    def inspect(url, process, directory, **kwargs):
        state["calls"].append(url)
        return [{"url": url, "title": "Fixture 3", "subtitle_tracks": [{"code": "es", "name": "Spanish", "source": "manual"}]}]
    state["patch"] = patch("pyrobolic.app.inspect_url", side_effect=inspect)
    state["patch"].start()
    editor.check_languages.grab_focus()
    key("Return")


def checked():
    editor = state["subtitles"]
    assert editor.worker is None
    assert state["calls"] == ["https://example.org/3"]
    state["patch"].stop()
    assert "3 of 3" in editor.availability.get_text()
    assert not editor.check_languages.get_visible()
    assert editor.available_codes == ["en", "es"]
    assert "en: reported on 1 of 3 checked items; missing on 2." in editor.coverage.get_text()
    assert "Unchecked" not in editor.coverage.get_text()
    assert editor.coverage.grab_focus()


def coverage_layout():
    editor = state["subtitles"]
    ok, bounds = editor.coverage.compute_bounds(app.window)
    assert ok and bounds.get_x() >= 0
    assert bounds.get_x() + bounds.get_width() <= app.window.get_width() + 1
    shot("mixed-subtitles")
    editor.cancel.grab_focus()
    key("Return")


def enqueue():
    state["parent"].enqueue()


def saved():
    assert len(store.jobs) == 3
    assert [job.url for job in store.jobs] == ["https://example.org/2", "https://example.org/3", "https://example.org/1"]
    restored = Store(store.directory)
    assert restored.settings.rate_limit_kib == 256
    assert all(j.fragment_concurrency == 3 for j in restored.jobs)
    app.do_open([Gio.File.new_for_uri("https://example.org/incoming")], 1, "")


def handoff():
    dialog = app.window.download_dialog
    assert dialog.url.get_text() == "https://example.org/incoming"
    assert dialog.process is None and not dialog.entries
    app.do_open([Gio.File.new_for_uri("https://example.org/waiting")], 1, "")
    assert app.pending_urls == ["https://example.org/waiting"]
    assert dialog.url.get_text() == "https://example.org/incoming"
    dialog.close()


def delayed_handoff():
    dialog = app.window.download_dialog
    assert dialog.url.get_text() == "https://example.org/waiting"
    assert dialog.process is None
    shot("handoff")
    dialog.inspected([{"url": "https://example.org/waiting", "title": "Range Fixture", "duration": 5}], None)
    state["range_parent"] = dialog
    state["range"] = dialog.edit_range()


def range_draft():
    editor = state["range"]
    editor.to_end.set_active(False)
    editor.start.set_value(2)
    editor.end.set_value(1)
    assert not editor.apply.get_sensitive()
    editor.start.set_value(1.5)
    editor.end.set_value(3.5)
    editor.precise.set_active(True)
    assert editor.apply.get_sensitive()
    assert "clip_start" not in state["range_parent"].entries[0][0]


def range_apply():
    shot("time-range")
    state["range"].apply.grab_focus()
    key("Return")


def range_submit():
    parent = state["range_parent"]
    assert parent.entries[0][0]["clip_start"] == 1.5
    parent.enqueue()


def range_saved():
    assert len(store.jobs) == 4
    assert store.jobs[-1].precise_cuts
    assert store.jobs[-1].clip_end == 3.5
    assert not hasattr(store.settings, "clip_start")
    assert Store(store.directory).jobs[-1].clip_start == 1.5
    app.window.queue.cancel_all()
    job = store.jobs[0]
    job.status, job.transfer_paused = "Downloading", True
    app.window.queue._notify(persist=False)


def pause_row():
    row = app.window.rows[store.jobs[0].id]
    assert row.pause.get_tooltip_text() == "Resume Transfer"
    assert row.subtitle.get_text() == "Transfer Paused"
    assert Gtk.test_accessible_has_property(row.pause, Gtk.AccessibleProperty.LABEL)
    app.window.switch_view("downloads")
    row.pause.grab_focus()


def finish():
    row = app.window.rows[store.jobs[0].id]
    for widget in (row.pause, row.command, row.details):
        ok, bounds = widget.compute_bounds(app.window)
        assert ok and bounds.get_x() >= 0 and bounds.get_x() + bounds.get_width() <= app.window.get_width() + 1
    shot("paused-row")
    state["inhibit"] = patch.object(app, "inhibit", return_value=42)
    state["uninhibit"] = patch.object(app, "uninhibit")
    state["inhibit_mock"] = state["inhibit"].start()
    state["uninhibit_mock"] = state["uninhibit"].start()
    store.settings.prevent_suspend = True
    app.window.sync_inhibition()
    state["inhibit_mock"].assert_not_called()
    app.window.jobs[store.jobs[0].id].transfer_paused = False
    app.window.sync_inhibition()
    state["inhibit_mock"].assert_called_once()
    app.window.sync_inhibition()
    state["inhibit_mock"].assert_called_once()
    app.window.jobs[store.jobs[0].id].transfer_paused = True
    app.window.sync_inhibition()
    state["uninhibit_mock"].assert_called_once_with(42)
    state["inhibit"].stop()
    state["uninhibit"].stop()
    app.window.queue.shutdown()
    app.window.actions.shutdown(wait=True)
    app.quit()


steps = iter([initial, edit_cancel, reopen_transfer, apply_transfer, order, reorder, subtitles,
              inspect_subtitles, checked, coverage_layout, enqueue, saved, handoff, delayed_handoff,
              range_draft, range_apply, range_submit, range_saved, pause_row, finish])
def advance():
    try:
        next(steps)()
    except StopIteration:
        return False
    except Exception:
        failures.append(traceback.format_exc())
        if state.get("patch"):
            state["patch"].stop()
        app.quit()
        return False
    return True

GLib.timeout_add(850, advance)
app.run([])
temp.cleanup()
if failures:
    print("\n".join(failures))
    raise SystemExit(1)
print("Expansion UI checks passed")
