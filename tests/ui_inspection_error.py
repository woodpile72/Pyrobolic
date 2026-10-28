# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Inspection errors expand the dialog and remain selectable and readable."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import traceback
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pyrobolic.app import Application, Adw, GLib, Gtk
from pyrobolic.core import Store
import gi
gi.require_version("GdkX11", "4.0")
from gi.repository import GdkX11

temporary = tempfile.TemporaryDirectory()
app = Application(Store(Path(temporary.name) / "state"))
state, failures = {}, []
hc = bool(os.environ.get("ADW_DEBUG_HIGH_CONTRAST"))
error = "ERROR: [ExampleEmbed] Unable to download JSON metadata: HTTP Error 403: Forbidden (caused by HTTP Error 403: Forbidden)"
release = threading.Event()


def inspect_fixture(*args, **kwargs):
    if not release.wait(15):
        raise RuntimeError("Inspection fixture timed out")
    raise RuntimeError(error)


def initial():
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT if hc else Adw.ColorScheme.FORCE_DARK)
    if hc:
        Gtk.Settings.get_default().set_property("gtk-font-name", "Sans 20")
    app.window.set_default_size(440 if hc else 880, 850 if hc else 650)


def open_download():
    state["dialog"] = app.window.new_download("https://example.org/fixture")


def inspect():
    dialog = state["dialog"]
    state["original_height"] = dialog.get_child().get_height()
    state["minimum_width"] = dialog.get_child().measure(Gtk.Orientation.HORIZONTAL, -1)[0]
    dialog.inspect()


def busy():
    dialog = state["dialog"]
    assert dialog.process is not None
    assert dialog.spinner.get_visible() and dialog.spinner.get_spinning()
    assert dialog.download.get_label() == "Continue"
    assert not any(widget.get_sensitive() for widget in (
        dialog.url, dialog.download, dialog.paste_button, dialog.quick_button,
        dialog.quick_menu, dialog.auth_button))
    assert dialog.cancel.get_sensitive()
    assert dialog.cancel.grab_focus()
    assert dialog.get_child().measure(Gtk.Orientation.HORIZONTAL, -1)[0] <= state["minimum_width"]
    release.set()


def visible():
    dialog = state["dialog"]
    output = Path(__file__).resolve().parents[1] / ".qa"
    output.mkdir(exist_ok=True)
    subprocess.run(["import", "-window", str(GdkX11.X11Surface.get_xid(app.window.get_surface())),
                    str(output / ("inspection-error-hc.png" if hc else "inspection-error-dark.png"))], check=True)
    height = dialog.get_child().get_height()
    assert height >= state["original_height"]
    assert dialog.message.get_selectable()
    assert dialog.get_content_height() >= 300
    assert dialog.download.get_sensitive() and dialog.quick_menu.get_sensitive()
    assert dialog.process is None and not dialog.spinner.get_visible()
    assert dialog.url.get_sensitive() and dialog.auth_button.get_sensitive()
    assert dialog.message.get_text() == error
    ok, bounds = dialog.message.compute_bounds(app.window)
    assert ok and bounds.get_x() >= 0 and bounds.get_x() + bounds.get_width() <= app.window.get_width() + 1
    scroll = dialog.message.get_parent().get_parent()
    assert isinstance(scroll, Gtk.Viewport)
    adjustment = scroll.get_vadjustment()
    assert adjustment.get_upper() <= adjustment.get_page_size() + 1, "Typical error should fit without scrolling"
    dialog.show_message(error + "\n" + "Additional diagnostic detail. " * 300)


def long_error():
    dialog = state["dialog"]
    assert dialog.get_content_height() == 720
    assert dialog.cancel.get_sensitive()
    assert len(dialog.message.get_text()) > 4000
    dialog.url.set_text("https://example.org/retry")
    assert not dialog.message.get_visible()
    assert dialog.get_content_height() == 240


def open_transfer():
    state["editor"] = state["dialog"].edit_transfer()


def identity_menu():
    state["editor"].identity.grab_focus()
    subprocess.run(["xdotool", "key", "--clearmodifiers", "space"], check=True)


def choose_identity():
    subprocess.run(["xdotool", "key", "--clearmodifiers", "Down", "Return"], check=True)


def cancel_identity():
    editor = state["editor"]
    assert editor.draft.browser_identity == "firefox"
    editor.cancel.grab_focus()
    subprocess.run(["xdotool", "key", "--clearmodifiers", "Return"], check=True)


def finish():
    assert state["dialog"].media_preferences.browser_identity == "default"
    assert app.window.store.settings.browser_identity == "default"
    app.window.queue.shutdown()
    app.window.actions.shutdown(wait=True)
    app.quit()


steps = iter([initial, open_download, inspect, busy, visible, long_error, open_transfer, identity_menu, choose_identity, cancel_identity, finish])
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

GLib.timeout_add(1100, advance)
with patch("pyrobolic.app.inspect_url", side_effect=inspect_fixture):
    try:
        app.run([])
    finally:
        release.set()
temporary.cleanup()
if failures:
    print("\n".join(failures))
    raise SystemExit(1)
print("Expanded inspection error checks passed")
