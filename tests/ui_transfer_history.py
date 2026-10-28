# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Pre-inspection network options, batch handoff and confirmed History retention."""
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pyrobolic.app import Application, Adw, GLib, Gtk, Preferences, Window
from pyrobolic.core import Job, Store
from pyrobolic.history import RETENTION_SECONDS
from pyrobolic.transfer import BROWSER_IDENTITIES
import gi
gi.require_version("GdkX11", "4.0")
from gi.repository import GdkX11

temporary = tempfile.TemporaryDirectory()
store = Store(Path(temporary.name) / "state")
store.settings.output = str(Path(temporary.name) / "media")
store.paused = True
old = Job("https://example.org/old", "Old", temporary.name, status="Completed", completed_at=1)
recent = replace(old, id="recent", title="Recent", completed_at=time.time())
store.jobs[:] = [old, recent]
store.save()
app = Application(store)
state, failures, calls = {}, [], []
hc = bool(os.environ.get("ADW_DEBUG_HIGH_CONTRAST"))
proxy = "http://127.0.0.1:8088"
def unhandled(kind, error, trace):
    failures.append("".join(traceback.format_exception(kind, error, trace)))
    app.quit()
sys.excepthook = unhandled


def fixture(url, process, directory, **options):
    calls.append(options)
    if len(calls) == 1:
        assert options["transfer"].proxy == proxy
        assert not options["transfer"].direct_connection
    else:
        assert options["transfer"].direct_connection
        assert not options["transfer"].proxy
    return [{"url": url, "title": "Network fixture", "media_id": "1", "formats_loaded": True,
             "subtitle_tracks": []}]


def key(value):
    subprocess.run(["xdotool", "key", "--clearmodifiers", value], check=True)


def initial():
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT if hc else Adw.ColorScheme.FORCE_DARK)
    if hc:
        Gtk.Settings.get_default().set_property("gtk-font-name", "Sans 20")
    app.window.set_default_size(440, 850)
    app.window.queue.stopped = True
    state["parent"] = app.window.new_download("https://example.org/fixture")


def menu():
    state["parent"].quick_menu.grab_focus()
    key("space")


def open_transfer():
    parent = state["parent"]
    assert parent.quick_popover.get_visible()
    assert Gtk.test_accessible_has_property(parent.quick_transfer, Gtk.AccessibleProperty.LABEL)
    parent.quick_transfer.grab_focus()
    key("Return")


def transfer():
    editor = state["editor"] = app.window.get_visible_dialog()
    editor.proxy.set_text(proxy)
    editor.rate.set_value(256)
    assert editor.apply.get_sensitive()
    assert not state["parent"].media_preferences.proxy
    editor.proxy.grab_focus()


def screenshot_and_apply():
    editor = state["editor"]
    for widget in (editor.rate, editor.fragments, editor.part_files, editor.proxy, editor.apply, editor.cancel):
        ok, bounds = widget.compute_bounds(app.window)
        assert ok and bounds.get_x() >= 0 and bounds.get_x() + bounds.get_width() <= app.window.get_width() + 1
    target = Path(__file__).resolve().parents[1] / ".qa"
    target.mkdir(exist_ok=True)
    subprocess.run(["import", "-window", str(GdkX11.X11Surface.get_xid(app.window.get_surface())),
                    str(target / ("transfer-before-inspect-hc.png" if hc else "transfer-before-inspect-dark.png"))], check=True)
    editor.apply.grab_focus()
    key("Return")


def inspect():
    parent = state["parent"]
    assert parent.media_preferences.proxy == proxy
    assert not store.settings.proxy
    assert "Proxy" in parent.quick_summary.get_text()
    parent.inspect()


def single_done():
    assert state["parent"].entries
    assert len(calls) == 1
    state["parent"].close()


def batch():
    parent = state["batch"] = app.window.new_batch("https://example.org/first\nhttps://example.org/second")
    state["editor"] = parent.edit_transfer()


def batch_options():
    editor = state["editor"]
    editor.proxy.set_text(proxy)
    editor.rate.set_value(128)
    editor.part_files.set_active(False)
    editor.identity.set_selected(BROWSER_IDENTITIES.index("firefox"))
    assert Gtk.test_accessible_has_property(editor.direct, Gtk.AccessibleProperty.LABEL)
    editor.direct.grab_focus()
    key("space")


def cancel_direct():
    assert state["editor"].direct.get_active()
    assert not state["editor"].proxy.get_sensitive()
    key("Escape")


def reopen_direct():
    assert not state["batch"].media_preferences.direct_connection
    assert not store.settings.direct_connection
    state["editor"] = state["batch"].edit_transfer()


def batch_apply():
    editor = state["editor"]
    assert editor.direct.get_active() and not editor.proxy.get_sensitive()
    assert not editor.draft.proxy
    target = Path(__file__).resolve().parents[1] / ".qa"
    subprocess.run(["import", "-window", str(GdkX11.X11Surface.get_xid(app.window.get_surface())),
                    str(target / ("direct-connection-hc.png" if hc else "direct-connection-dark.png"))], check=True)
    editor.apply.grab_focus()
    key("Return")


def batch_inspect():
    assert state["batch"].media_preferences.direct_connection
    assert not state["batch"].media_preferences.proxy
    assert not store.settings.direct_connection
    state["batch"].inspect()


def review():
    assert len(calls) == 3
    assert not state["batch"].worker
    state["batch"].review()


def submit():
    parent = app.window.download_dialog
    assert parent.media_preferences.direct_connection
    assert not parent.media_preferences.proxy
    assert parent.media_preferences.rate_limit_kib == 128
    assert not parent.media_preferences.use_part_files
    assert parent.media_preferences.browser_identity == "firefox"
    parent.enqueue()


def preferences():
    assert store.settings.direct_connection and not store.settings.proxy
    assert all(job.direct_connection and not job.proxy for job in store.jobs if job.status == "Queued")
    assert all(not job.use_part_files for job in store.jobs if job.status == "Queued")
    assert all(job.browser_identity == "firefox" for job in store.jobs if job.status == "Queued")
    assert store.settings.browser_identity == "default"
    state["prefs"] = Preferences(app.window)
    state["prefs"].present(app.window)


def retention_layout():
    state["prefs"].retention.grab_focus()


def cancel_retention():
    target = Path(__file__).resolve().parents[1] / ".qa"
    subprocess.run(["import", "-window", str(GdkX11.X11Surface.get_xid(app.window.get_surface())),
                    str(target / ("history-retention-hc.png" if hc else "history-retention-dark.png"))], check=True)
    state["prefs"].retention.set_selected(RETENTION_SECONDS.index(604800))
    dialog = app.window.get_visible_dialog()
    assert isinstance(dialog, Adw.AlertDialog)
    assert dialog.get_default_response() == "cancel"


def dismiss_retention():
    key("Escape")


def request_retention():
    assert store.settings.history_retention_days == 0
    assert old in store.jobs
    assert state["prefs"].retention.get_selected() == 0
    state["prefs"].retention.set_selected(RETENTION_SECONDS.index(604800))


def confirm_retention():
    dialog = app.window.get_visible_dialog()
    dialog.emit("response", "apply")
    dialog.close()


def pruned():
    assert store.settings.history_retention_seconds == 604800
    assert old not in store.jobs and recent in store.jobs
    assert len([job for job in store.jobs if job.status == "Queued"]) == 2
    assert Store(store.directory).settings.history_retention_seconds == 604800
    state["failure"] = patch.object(store, "save", side_effect=OSError("Fixture storage failure"))
    state["failure"].start()
    state["prefs"].retention.set_selected(0)


def startup_retention():
    assert store.settings.history_retention_seconds == 604800
    assert state["prefs"].retention.get_selected() == RETENTION_SECONDS.index(604800)
    state["failure"].stop()
    state.pop("failure")
    startup = Store(Path(temporary.name) / "startup-state")
    startup.settings.history_retention_days = 7
    startup.jobs[:] = [old, recent]
    startup.save()
    state["startup"] = Window(app, startup)


def finish():
    extra = state["startup"]
    assert extra.store.jobs == [recent]
    assert [job.id for job in Store(extra.store.directory).jobs] == [recent.id]
    extra.queue.shutdown()
    extra.actions.shutdown(wait=True)
    extra.destroy()
    assert not extra.retention_timer
    app.window.queue.shutdown()
    app.window.actions.shutdown(wait=True)
    app.quit()


steps = iter([initial, menu, open_transfer, transfer, screenshot_and_apply, inspect, single_done,
              batch, batch_options, cancel_direct, reopen_direct, batch_options, batch_apply, batch_inspect, review, submit, preferences, retention_layout, cancel_retention, dismiss_retention,
              request_retention, confirm_retention, pruned, startup_retention, finish])
def advance():
    try:
        next(steps)()
    except StopIteration:
        return False
    except Exception:
        if "failure" in state:
            state["failure"].stop()
        failures.append(traceback.format_exc())
        app.quit()
        return False
    return True

GLib.timeout_add(1000, advance)
with patch("pyrobolic.app.inspect_url", side_effect=fixture), patch("pyrobolic.batch.inspect_url", side_effect=fixture):
    app.run([])
temporary.cleanup()
if failures:
    print("\n".join(failures))
    raise SystemExit(1)
print("Pre-inspection transfer, batch handoff and History retention checks passed")
