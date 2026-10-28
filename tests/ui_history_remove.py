# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""History row removal preserves files, handles failed saves and supports keyboard use."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import traceback
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pyrobolic.app import Application, Adw, GLib, Gtk
from pyrobolic.core import Job, Store, TERMINAL
import gi
gi.require_version("GdkX11", "4.0")
from gi.repository import GdkX11

temporary = tempfile.TemporaryDirectory()
root = Path(temporary.name)
store = Store(root / "state")
store.paused = True
media = root / "Fixture.mp4"
media.write_bytes(b"published file must survive removal")
jobs = [Job(f"https://example.org/{status}", "Long history title for narrow layouts",
            str(root), filename=f"Fixture {status}", output_stem=f"Fixture {status}", status=status)
        for status in ("Completed", "Failed", "Canceled", "Interrupted", "Queued")]
jobs[0].files = [str(media)]
store.jobs = jobs[:]
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


def initial():
    Adw.StyleManager.get_default().set_color_scheme(
        Adw.ColorScheme.FORCE_LIGHT if hc else Adw.ColorScheme.FORCE_DARK)
    if hc:
        Gtk.Settings.get_default().set_property("gtk-font-name", "Sans 20")
    app.window.set_default_size(440, 850)
    app.window.queue.stopped = True
    app.window.switch_view("history")


def capture():
    for job in jobs:
        row = app.window.rows[job.id]
        assert row.remove.get_visible() == (job.status in TERMINAL)
        assert row.details.get_next_sibling() == row.remove
        assert row.remove.get_icon_name() == "window-close-symbolic"
        assert Gtk.test_accessible_has_property(row.remove, Gtk.AccessibleProperty.LABEL)
        if job.status in TERMINAL:
            okay, bounds = row.remove.compute_bounds(app.window)
            assert okay and bounds.get_x() >= 0
            assert bounds.get_x() + bounds.get_width() <= app.window.get_width() + 1
    output = Path(__file__).resolve().parents[1] / ".qa"
    output.mkdir(exist_ok=True)
    subprocess.run(["import", "-window", str(GdkX11.X11Surface.get_xid(app.window.get_surface())),
                    str(output / ("history-remove-hc.png" if hc else "history-remove-dark.png"))], check=True)
    open_confirmation()


def open_confirmation():
    assert app.window.rows[jobs[0].id].remove.grab_focus()
    key("Return")


def cancel():
    dialog = app.window.get_visible_dialog()
    assert isinstance(dialog, Adw.AlertDialog)
    assert dialog.get_default_response() == "cancel"
    assert "Downloaded files are not removed." in dialog.get_body()
    key("Escape")


def prepare_failure():
    assert len(store.jobs) == 5 and media.exists()
    state["save_patch"] = patch.object(store, "save", side_effect=OSError("Fixture save failure"))
    state["save_patch"].start()
    open_confirmation()


def confirm():
    dialog = app.window.get_visible_dialog()
    remove = next(widget for widget in widgets(dialog)
                  if isinstance(widget, Gtk.Button) and widget.get_label() == "Remove")
    assert remove.grab_focus()
    key("Return")


def retry():
    state.pop("save_patch").stop()
    assert len(store.jobs) == 5 and jobs[0].id in app.window.rows
    assert app.window.queue.persistence_error == "Fixture save failure"
    app.window.queue.retry_save()
    open_confirmation()


def removed():
    assert app.window.get_visible()
    assert app.window.stack.get_visible_child_name() == "history"
    assert jobs[0].id not in app.window.rows
    assert len(store.jobs) == 4 and len(Store(store.directory).jobs) == 4
    assert media.read_bytes() == b"published file must survive removal"
    assert all(job.id in app.window.rows for job in jobs[1:])
    # The original Details action must still close only its own dialog.
    state["details"] = app.window.details(jobs[1])


def details_removal():
    app.window.confirm_job_removal(jobs[1], state["details"])


def finish():
    assert app.window.get_visible()
    assert jobs[1].id not in app.window.rows
    assert len(Store(store.directory).jobs) == 3
    assert media.read_bytes() == b"published file must survive removal"
    app.window.queue.shutdown()
    app.window.actions.shutdown(wait=True)
    app.quit()


steps = iter([initial, capture, cancel, prepare_failure, confirm, retry, confirm,
              removed, details_removal, confirm, finish])
def advance():
    try:
        next(steps)()
    except StopIteration:
        return False
    except Exception:
        failures.append(traceback.format_exc())
        patch.stopall()
        app.window.queue.shutdown()
        app.window.actions.shutdown(wait=True)
        app.quit()
        return False
    return True

GLib.timeout_add(1000, advance)
app.run([])
temporary.cleanup()
if failures:
    print("\n".join(failures))
    raise SystemExit(1)
print("History row removal checks passed")
