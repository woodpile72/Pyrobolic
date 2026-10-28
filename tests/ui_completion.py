# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Completion UI checks on an isolated display; desktop service calls are captured."""

from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pyrobolic.app import Application, GLib, Preferences
from pyrobolic.core import Job, Store
import gi
gi.require_version("GdkX11", "4.0")
from gi.repository import GdkX11

temporary = tempfile.TemporaryDirectory()
store = Store(Path(temporary.name) / "state")
store.settings.output = temporary.name
store.paused = True
old = Job("https://example.org/old", "Old completed download", temporary.name, status="Completed")
store.jobs = [old]
app = Application(store)
failures, state = [], {}
root = Path(__file__).resolve().parents[1]


def screenshot(name):
    xid = GdkX11.X11Surface.get_xid(app.window.get_surface())
    subprocess.run(["import", "-window", str(xid), str(root / ".qa" / f"{name}.png")], check=True)


def complete(count=1):
    jobs = [Job(f"https://example.org/{index}", f"Concert {index}", temporary.name) for index in range(count)]
    app.window.queue.add(jobs)
    for job in jobs:
        media = Path(temporary.name) / f"{job.id}.mp4"
        media.write_bytes(b"verified test fixture")
        job.status, job.progress, job.detail = "Completed", 1, "Download complete"
        job.files = [str(media)]
        app.window.queue.completed[job.id] = replace(job)
    app.window.queue._notify()
    return jobs


def first():
    app.window.queue.stopped = True
    app.window.set_default_size(880, 700)
    state["notify"] = patch.object(app, "send_notification").start()
    state["open"] = patch.object(app.window, "open_folder").start()
    state["toast"] = patch.object(app.window.toast, "add_toast", wraps=app.window.toast.add_toast).start()
    app.window.completion_feedback()
    state["notify"].assert_not_called()
    state["job"] = complete()[0]


def notification_and_action():
    state["notify"].assert_called_once()
    state["open"].assert_not_called()
    toast = state["toast"].call_args.args[0]
    assert toast.get_button_label() == "Show in History"
    assert app.window.stack.get_visible_child_name() == "downloads"
    screenshot("completion-toast-wide")
    toast.emit("button-clicked")
    assert app.window.stack.get_visible_child_name() == "history"
    app.window.search.set_text("does not match")
    app.activate_action("show-download", GLib.Variant("s", state["job"].id))
    assert app.window.search.get_text() == ""
    assert state["job"].id in app.window.rows
    app.window.completion_feedback()
    app.window.refresh()
    state["notify"].assert_called_once()
    store.settings.open_folder_on_completion = True
    state["jobs"] = complete(3)


def grouped():
    assert state["notify"].call_count == 2
    state["open"].assert_called_once_with(temporary.name)
    assert state["toast"].call_args.args[0].get_title() == "3 Downloads Complete"
    state["notify"].side_effect = GLib.Error("No notification service")
    complete()


def unavailable():
    assert state["notify"].call_count == 3
    assert state["toast"].call_args.args[0].get_title() == "Download Complete"
    assert all(j.status == "Completed" for j in store.jobs)
    store.settings.notifications = False
    store.settings.open_folder_on_completion = False
    complete()


def disabled():
    assert state["notify"].call_count == 3
    assert state["open"].call_count == 2
    # Action routing remains useful after files or history have been removed.
    for path in state["job"].files:
        Path(path).unlink()
    app.activate_action("show-download", GLib.Variant("s", state["job"].id))
    assert app.window.stack.get_visible_child_name() == "history"
    app.activate_action("show-download", GLib.Variant("s", "no-longer-in-history"))
    preferences = Preferences(app.window)
    preferences.present(app.window)
    state["preferences"] = preferences
    assert not preferences.completion_settings["notifications"].get_active()
    preferences.completion_settings["notifications"].set_active(True)
    preferences.completion_settings["open_folder_on_completion"].set_active(True)
    app.window.set_default_size(390, 760)


def saved_preferences():
    saved = Store(store.directory)
    assert saved.settings.notifications
    assert saved.settings.open_folder_on_completion
    screenshot("completion-preferences-narrow")
    state["preferences"].force_close()
    state["notify"].side_effect = None
    store.settings.open_folder_on_completion = False
    state["cleared"] = complete()[0]
    app.window.queue.set_history_clear(-1)
    # Policy changes must not leave a pending toast pointing to a removed record.
    app.window.queue.set_history_clear(0)


def cleared_feedback():
    assert state["cleared"] not in store.jobs
    toast = state["toast"].call_args.args[0]
    assert toast.get_button_label() == "Open Folder"
    state["open"].reset_mock()
    toast.emit("button-clicked")
    state["open"].assert_called_once_with(temporary.name)
    state["open"].reset_mock()
    app.activate_action("open-completed-folder", GLib.Variant("s", temporary.name))
    state["open"].assert_called_once_with(temporary.name)
    assert all(Path(path).is_file() for path in state["cleared"].files)
    app.window.closing = True
    app.window.actions.shutdown(wait=True)
    app.quit()


steps = iter([first, notification_and_action, grouped, unavailable, disabled, saved_preferences, cleared_feedback])
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

app.connect("activate", lambda *_: GLib.timeout_add(850, step))
app.run([])
patch.stopall()
temporary.cleanup()
if failures:
    print("\n".join(failures), file=sys.stderr)
    raise SystemExit(1)
print("Completion UI checks passed", flush=True)
