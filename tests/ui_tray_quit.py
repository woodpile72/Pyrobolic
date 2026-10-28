# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Exercise each explicit quit route with an active child and queued work."""
from pathlib import Path
import sys
import tempfile
import time
import traceback
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pyrobolic.app import Application, Adw, GLib
from pyrobolic.core import Store, Job

route = sys.argv[1] if len(sys.argv) > 1 else "tray"
temporary = tempfile.TemporaryDirectory()
store = Store(Path(temporary.name) / "state")
store.settings.background_mode = True
store.settings.notifications = False
store.settings.concurrency = 1
app = Application(store)
state, failures = {}, []


def initial():
    app.tray.set_ready(True)
    jobs = [Job(f"https://example.org/{i}", f"Fixture {i}", temporary.name) for i in range(2)]
    app.window.queue.add(jobs)
    state["jobs"] = jobs


def trigger():
    assert state["jobs"][0].status == "Downloading"
    assert state["jobs"][1].status == "Queued"
    state["process"] = next(iter(app.window.queue.running.values()))
    app.window.close()
    deadline = time.monotonic() + 2
    while app.window.get_visible() and time.monotonic() < deadline:
        GLib.MainContext.default().iteration(False)
        time.sleep(.005)
    assert not app.window.get_visible()
    if route == "tray":
        app.tray.invoke(4)
    elif route == "menu":
        assert app.window.quit_menu.get_item_attribute_value(0, "action", None).unpack() == "app.quit"
        app.activate_action("quit", None)
    else:
        assert "<Control>q" in app.get_accels_for_action("app.quit")
        app.activate_action("quit", None)


def keep_open():
    assert app.window.get_visible()
    dialog = app.window.get_visible_dialog()
    assert isinstance(dialog, Adw.AlertDialog) and dialog.get_heading() == "Cancel Downloads and Quit?"
    app.request_quit()
    assert app.window.get_visible_dialog() is dialog
    dialog.emit("response", "keep")
    dialog.force_close()


def quit_again():
    assert not app.window.closing
    app.request_quit()
    dialog = app.window.get_visible_dialog()
    dialog.emit("response", "quit")
    dialog.force_close()


steps = iter([initial, trigger, keep_open, quit_again])
ticks = 0
def advance():
    global ticks
    ticks += 1
    try:
        next(steps)()
    except StopIteration:
        if ticks < 12:
            return True
        failures.append("Safe quit did not exit")
        app.quit()
        return False
    except Exception:
        failures.append(traceback.format_exc())
        app.quit()
        return False
    return True


with patch.object(app.tray, "configure"), patch("pyrobolic.core.engine_path", return_value="fixture"), \
        patch("pyrobolic.core.download_arguments", return_value=[sys.executable, "-c", "import time; time.sleep(60)"]):
    GLib.timeout_add(500, advance)
    app.run([])
assert not app.background_held
if failures:
    app.window.queue.shutdown()
    raise AssertionError("\n".join(failures))
assert state["process"].done.is_set()
assert state["process"].process.poll() is not None
assert [job.status for job in state["jobs"]] == ["Canceled", "Interrupted"]
assert len(Store(store.directory).jobs) == 2
temporary.cleanup()
print(f"{route}: confirmation, keep-open, active child cleanup, queued state and explicit exit passed")
