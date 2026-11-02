# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Clipboard focus regression checks on a private Xvfb/Metacity session."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import traceback
from unittest.mock import patch

sys.path.insert(0, os.environ.get("PYROBOLIC_TEST_ROOT", str(Path(__file__).resolve().parents[1])))
from pyrobolic.app import Application, Adw, GLib, Gtk, Window
from pyrobolic.core import Store
import gi
gi.require_version("GdkX11", "4.0")
from gi.repository import GdkX11

temporary = tempfile.TemporaryDirectory()
store = Store(Path(temporary.name) / "state")
store.settings.output = str(Path(temporary.name) / "Downloads")
store.settings.notifications = False
app = Application(store)
state, failures = {}, []


def focus(window):
    xid = GdkX11.X11Surface.get_xid(window.get_surface())
    subprocess.run(["xdotool", "windowactivate", "--sync", str(xid)], check=True, timeout=5)


def clipboard(text):
    app.window.get_display().get_clipboard().set(text)


def dialog_url(expected):
    dialog = app.window.download_dialog
    assert dialog and not dialog.closed and dialog.url.get_text() == expected
    assert not store.jobs and not app.window.queue.running
    return dialog


def no_dialog():
    assert app.window.get_visible_dialog() is None


def initial():
    assert app.window.is_active()
    state["other"] = Gtk.Window(title="Clipboard source", default_width=300, default_height=200)
    state["other"].present()


def copy_away():
    assert not app.window.is_active()
    clipboard("https://example.org/focus-one")


def return_to_main():
    no_dialog()
    focus(app.window)


def first_offer():
    dialog_url("https://example.org/focus-one").force_close()


def same_url_away():
    no_dialog()
    focus(state["other"])


def same_url_return():
    focus(app.window)


def same_url_suppressed():
    no_dialog()
    assert app.window.is_active()
    clipboard("https://example.org/focus-two")


def no_continuous_monitor():
    no_dialog()
    focus(state["other"])


def second_return():
    focus(app.window)


def second_offer():
    d = dialog_url("https://example.org/focus-two")
    d.url.set_text("https://example.org/typed-draft")
    state["draft"] = d
    focus(state["other"])


def change_with_draft():
    clipboard("https://example.org/focus-three")
    focus(app.window)


def preserve_draft():
    assert app.window.download_dialog is state["draft"]
    dialog_url("https://example.org/typed-draft").force_close()


def third_away():
    focus(state["other"])


def third_return():
    focus(app.window)


def third_offer():
    dialog_url("https://example.org/focus-three").force_close()


def invalid_away():
    focus(state["other"])
    clipboard("ordinary text")


def invalid_return():
    focus(app.window)


def invalid_ignored():
    no_dialog()
    assert app.window.last_clipboard_url is None
    clipboard("file:///tmp/private")
    app.window.offer_clipboard()


def local_url_ignored():
    no_dialog()
    state["preferences"] = Adw.Dialog(title="Existing dialog", child=Gtk.Label(label="Keep this open"))
    state["preferences"].present(app.window)
    focus(state["other"])


def preferences_return():
    clipboard("https://example.org/preference-draft")
    focus(app.window)


def preferences_preserved():
    assert app.window.get_visible_dialog() is state["preferences"]
    state["preferences"].force_close()
    clipboard("https://example.org/manual")


def manual_open():
    state["manual"] = app.window.new_download()


def manual_prefill():
    dialog_url("https://example.org/manual").force_close()


def manual_away():
    focus(state["other"])


def manual_return():
    focus(app.window)


def manual_not_reoffered():
    no_dialog()
    focus(state["other"])
    clipboard("https://example.org/notification-must-not-open")


def notification_activation():
    app.pending_download = "missing-history-entry"
    app.activate()


def notification_not_interrupted():
    no_dialog()
    assert app.window.is_active()
    clipboard("")


class DelayedClipboard:
    def __init__(self):
        self.callbacks = []
    def get_clipboard(self):
        return self
    def read_text_async(self, _cancel, callback):
        self.callbacks.append(callback)
    def read_text_finish(self, result):
        return result
    def complete(self, index, text):
        self.callbacks[index](self, text)


def delayed_reads():
    # Use real window/dialog objects with controlled asynchronous delivery.
    c = DelayedClipboard()
    with patch.object(Window, "get_display", return_value=c):
        app.window.offer_clipboard()
        app.window.offer_clipboard()
    c.complete(0, "https://example.org/stale")
    no_dialog()
    c.complete(1, "https://example.org/latest")
    dialog_url("https://example.org/latest")


def close_delayed_offer():
    dialog_url("https://example.org/latest").force_close()


def modal_race():
    c = DelayedClipboard()
    with patch.object(Window, "get_display", return_value=c):
        app.window.offer_clipboard()
    d = Adw.Dialog(title="Dialog opened while reading", child=Gtk.Label(label="Keep this open"))
    d.present(app.window)
    c.complete(0, "https://example.org/too-late")
    assert app.window.get_visible_dialog() is d
    state["race_dialog"] = d


def close_race_dialog():
    state["race_dialog"].force_close()


def lose_focus_during_read():
    c = DelayedClipboard()
    with patch.object(Window, "get_display", return_value=c):
        app.window.offer_clipboard(require_active=True)
    state["late"] = c
    focus(state["other"])


def stale_focus_ignored():
    assert not app.window.is_active()
    state["late"].complete(0, "https://example.org/no-focus")
    no_dialog()
    focus(app.window)


def hidden_and_closing():
    c = DelayedClipboard()
    with patch.object(Window, "get_display", return_value=c):
        app.window.offer_clipboard()
    app.window.set_visible(False)
    c.complete(0, "https://example.org/hidden")
    no_dialog()
    app.window.present()
    c = DelayedClipboard()
    with patch.object(Window, "get_display", return_value=c):
        app.window.offer_clipboard()
    app.window.closing = True
    c.complete(0, "https://example.org/closing")
    no_dialog()
    app.window.closing = False


def finish():
    assert not store.jobs and not app.window.queue.running
    inspect.assert_not_called()
    app.window.closing = True
    app.window.actions.shutdown(wait=True)
    state["other"].destroy()
    app.quit()


steps = iter([initial, copy_away, return_to_main, first_offer, same_url_away, same_url_return,
              same_url_suppressed, no_continuous_monitor, second_return, second_offer,
              change_with_draft, preserve_draft, third_away, third_return, third_offer,
              invalid_away, invalid_return, invalid_ignored, local_url_ignored,
              preferences_return, preferences_preserved, manual_open, manual_prefill,
              manual_away, manual_return, manual_not_reoffered, notification_activation,
              notification_not_interrupted, delayed_reads, close_delayed_offer, modal_race,
              close_race_dialog, lose_focus_during_read,
              stale_focus_ignored, hidden_and_closing, finish])


def step():
    try:
        action = next(steps)
        action()
        print(action.__name__, flush=True)
    except StopIteration:
        return False
    except Exception:
        failures.append(traceback.format_exc())
        print(failures[-1], flush=True)
        app.quit()
        return False
    return True


with patch("pyrobolic.app.inspect_url") as inspect:
    GLib.timeout_add(450, step)
    app.run([])
raise SystemExit(bool(failures))
