# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Run only under a private Xvfb display/bus: real reparenting WM placement."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import traceback
from unittest.mock import patch

sys.path.insert(0, os.environ.get("PYROBOLIC_TEST_ROOT", str(Path(__file__).resolve().parents[1])))
from pyrobolic.app import Application, GLib
from pyrobolic.core import Store
from pyrobolic.window_placement import capture, restore, surface_id

temporary = tempfile.TemporaryDirectory()
store = Store(Path(temporary.name))
app = Application(store)
failures, positions = [], []
wm = subprocess.Popen(["metacity", "--sm-disable"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def move():
    app.tray.set_ready(True)
    restore(surface_id(app.window), (260, 170))


def hide():
    positions.append(capture(surface_id(app.window)))
    assert positions[-1] == (260, 170), positions
    app.window.close()


def reopen():
    assert not app.window.get_visible()
    app.activate()


def check():
    position = capture(surface_id(app.window))
    assert position == positions[-1], (position, positions)


def race():
    app.window.close()
    app.activate()


def check_race():
    assert app.window.get_visible()
    check()
    app.window.maximize()


def hide_maximized():
    assert app.window.is_maximized()
    app.window.close()


def check_maximized():
    assert app.window.is_maximized()
    app.request_quit()


steps = iter([move, hide, reopen, check, hide, reopen, check, race, check_race,
              hide_maximized, reopen, check_maximized])
def advance():
    try:
        next(steps)()
    except StopIteration:
        return False
    except Exception:
        failures.append(traceback.format_exc())
        app.window.queue.shutdown()
        app.window.actions.shutdown(wait=True)
        app.quit()
        return False
    return True


try:
    with patch.object(app.tray, "configure"):
        GLib.timeout_add(600, advance)
        app.run([])
finally:
    wm.terminate()
    wm.wait(timeout=5)
    temporary.cleanup()
if failures:
    raise AssertionError("\n".join(failures))
print("Placement preserved through repeated hide/reopen, activation race and maximized restore")
