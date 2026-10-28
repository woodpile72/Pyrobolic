# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Persist window size through the real close path, including storage failure recovery."""
from pathlib import Path
import sys
import tempfile
import traceback
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pyrobolic.app import Application, Adw, GLib, Preferences, Window
from pyrobolic.core import Store

temporary = tempfile.TemporaryDirectory()
store = Store(Path(temporary.name) / "state")
app = Application(store)
state, failures = {}, []
def unhandled(kind, error, trace):
    failures.append("".join(traceback.format_exception(kind, error, trace)))
    app.quit()
sys.excepthook = unhandled


def initial():
    app.hold()
    app.window.set_default_size(720, 500)
    state["prefs"] = Preferences(app.window)
    state["prefs"].present(app.window)
    state["failure"] = patch.object(store, "save", side_effect=OSError("Fixture disk full"))
    state["failure"].start()
    state["prefs"].remember_window.set_active(False)


def pref_failed():
    assert state["prefs"].remember_window.get_active()
    assert store.settings.remember_window
    state["prefs"].close()
    app.window.finish_close()


def close_failed():
    dialog = app.window.get_visible_dialog()
    assert isinstance(dialog, Adw.AlertDialog)
    assert dialog.get_default_response() == "stay"
    assert store.settings.window_width == 880
    assert store.settings.window_height == 650
    dialog.emit("response", "stay")
    dialog.close()
    state["failure"].stop()
    state.pop("failure")


def close_successfully():
    assert not app.window.closing
    app.window.finish_close()


def reopen():
    restored = Store(store.directory)
    assert restored.settings.window_width == 720
    assert restored.settings.window_height == 500
    assert not restored.settings.window_maximized
    app.window = Window(app, restored)
    assert app.window.get_default_size() == (720, 500)
    app.window.present()


def disable():
    state["prefs"] = Preferences(app.window)
    state["prefs"].present(app.window)
    state["prefs"].remember_window.set_active(False)


def close_disabled():
    assert not app.window.store.settings.remember_window
    state["prefs"].close()
    app.window.set_default_size(1000, 700)
    app.window.finish_close()


def reopen_disabled():
    restored = Store(store.directory)
    assert not restored.settings.remember_window
    assert (restored.settings.window_width, restored.settings.window_height) == (720, 500)
    app.window = Window(app, restored)
    assert app.window.get_default_size() == (880, 650)
    app.window.present()
    restored.settings.remember_window = True
    restored.settings.window_maximized = True
    with patch.object(Window, "maximize") as maximize:
        extra = Window(app, restored)
        maximize.assert_called_once()
        extra.queue.shutdown()
        extra.actions.shutdown(wait=True)
        extra.destroy()


def finish():
    app.window.queue.shutdown()
    app.window.actions.shutdown(wait=True)
    app.quit()


steps = iter([initial, pref_failed, close_failed, close_successfully, reopen, disable,
              close_disabled, reopen_disabled, finish])
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
app.run([])
temporary.cleanup()
if failures:
    print("\n".join(failures))
    raise SystemExit(1)
print("Window state, failed save recovery and disabled-restore checks passed")
