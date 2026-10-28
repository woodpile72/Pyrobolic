# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Isolated History actions; desktop launchers are intercepted, not actually opened."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import traceback
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pyrobolic.app import Application, Adw, GLib, Gtk
from pyrobolic.core import Job, Queue, Store
import gi
gi.require_version("GdkX11", "4.0")
from gi.repository import GdkX11

temporary = tempfile.TemporaryDirectory()
root = Path(temporary.name)
store = Store(root / "state")
store.paused = True
job = Job("https://example.org/public-fixture", "History actions fixture", str(root), filename="Fixture")
Queue(store).add([job])
media = root / "Fixture.mp4"
media.write_bytes(b"test file; no player is launched")
job.files, job.status = [str(media)], "Completed"
store.save()
app = Application(store)
state, failures = {}, []
hc = bool(os.environ.get("ADW_DEBUG_HIGH_CONTRAST"))


def key(value):
    subprocess.run(["xdotool", "key", "--clearmodifiers", value], check=True)


def widgets(widget):
    yield widget
    child = widget.get_first_child()
    while child:
        yield from widgets(child)
        child = child.get_next_sibling()


def control(tooltip):
    return next(w for w in widgets(state["details"]) if isinstance(w, Gtk.Button) and w.get_tooltip_text() == tooltip)


def initial():
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT if hc else Adw.ColorScheme.FORCE_DARK)
    if hc:
        Gtk.Settings.get_default().set_property("gtk-font-name", "Sans 20")
    app.window.set_default_size(440, 850)
    app.window.queue.stopped = True
    state["details"] = app.window.details(job)
    state["file_patch"] = patch.object(Gtk.FileLauncher, "launch")
    state["file_mock"] = state["file_patch"].start()
    state["uri_patch"] = patch.object(Gtk.UriLauncher, "launch")
    state["uri_mock"] = state["uri_patch"].start()


def open_source():
    launch = control("Open Source Page")
    assert Gtk.test_accessible_has_property(launch, Gtk.AccessibleProperty.LABEL)
    launch.grab_focus()
    key("Return")


def open_file():
    state["uri_mock"].assert_called_once()
    control("Open Downloaded File").grab_focus()
    key("Return")


def missing_file():
    state["file_mock"].assert_called_once()
    media.unlink()
    app.window.open_saved_file(job, str(media))


def capture():
    state["file_mock"].assert_called_once()
    for title in ("Open Downloaded File", "Download Again", "Open Source Page"):
        okay, bounds = control(title).compute_bounds(app.window)
        assert okay and bounds.get_x() >= 0 and bounds.get_x() + bounds.get_width() <= app.window.get_width() + 1
    output = Path(__file__).resolve().parents[1] / ".qa"
    subprocess.run(["import", "-window", str(GdkX11.X11Surface.get_xid(app.window.get_surface())),
                    str(output / ("history-actions-hc.png" if hc else "history-actions-dark.png"))], check=True)
    control("Download Again").grab_focus()
    key("Return")


def cancel_confirmation():
    dialog = app.window.get_visible_dialog()
    assert isinstance(dialog, Adw.AlertDialog)
    assert dialog.get_default_response() == "cancel"
    key("Escape")


def confirm_again():
    assert len(store.jobs) == 1
    state["confirm"] = app.window.confirm_redownload(job, state["details"])


def download():
    confirm = state["confirm"]
    target = next(w for w in widgets(confirm) if isinstance(w, Gtk.Button) and w.get_label() == "Download")
    target.grab_focus()
    key("Return")


def finish():
    assert len(store.jobs) == 2
    assert store.jobs[0].status == "Completed"
    assert store.jobs[1].status == "Queued"
    assert app.window.stack.get_visible_child_name() == "downloads"
    assert len(Store(store.directory).jobs) == 2
    state["file_patch"].stop()
    state["uri_patch"].stop()
    app.window.queue.shutdown()
    app.window.actions.shutdown(wait=True)
    app.quit()


steps = iter([initial, open_source, open_file, missing_file, capture, cancel_confirmation, confirm_again, download, finish])
def advance():
    try:
        next(steps)()
    except StopIteration:
        return False
    except Exception:
        failures.append(traceback.format_exc())
        patch.stopall()
        app.quit()
        return False
    return True

GLib.timeout_add(1000, advance)
app.run([])
temporary.cleanup()
if failures:
    print("\n".join(failures))
    raise SystemExit(1)
print("History action UI checks passed")
