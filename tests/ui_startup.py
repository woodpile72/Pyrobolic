# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Synthetic startup feedback on a private display; no source network access."""

from pathlib import Path
import os
import subprocess
import sys
import tempfile
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pyrobolic.app import Adw, Application, GLib, Gtk
from pyrobolic.core import Job, Store, startup_detail
import gi
gi.require_version("GdkX11", "4.0")
from gi.repository import GdkX11

temporary = tempfile.TemporaryDirectory()
store = Store(Path(temporary.name) / "state")
store.paused = True
job = Job("https://example.org/fixture", "Startup feedback fixture", temporary.name,
          status="Downloading", detail=startup_detail(False, 65))
store.jobs = [job]
app = Application(store)
state, failures = {}, []
qa = Path(__file__).resolve().parents[1] / ".qa"
qa.mkdir(exist_ok=True)
hc = bool(os.environ.get("ADW_DEBUG_HIGH_CONTRAST"))


def screenshot(name):
    xid = GdkX11.X11Surface.get_xid(app.window.get_surface())
    subprocess.run(["import", "-window", str(xid), str(qa / f"startup{'-hc' if hc else ''}-{name}.png")], check=True)


def first():
    app.window.queue.stopped = True
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT if hc else Adw.ColorScheme.FORCE_DARK)
    if hc:
        Gtk.Settings.get_default().set_property("gtk-font-name", "Sans 20")
    app.window.set_default_size(420 if hc else 390, 700)
    state["row"] = app.window.rows[job.id]


def waiting():
    row = state["row"]
    assert "Inspecting source" in row.subtitle.get_text()
    assert "1:05 elapsed" in row.subtitle.get_text()
    assert row.progress.get_fraction() == 0
    assert row.command.get_sensitive()
    assert row.startup.get_visible() and not row.subtitle.get_visible()
    assert row.startup.get_text() == "Inspecting source\n1:05 elapsed"
    for widget in (row.command, row.details, row.startup):
        okay, bounds = widget.compute_bounds(app.window)
        assert okay and bounds.get_x() >= 0
        assert bounds.get_x() + bounds.get_width() <= app.window.get_width() + 1
    assert Gtk.test_accessible_has_property(row.command, Gtk.AccessibleProperty.LABEL)
    row.command.grab_focus()
    state["focus"] = app.window.get_focus()
    assert state["focus"] is not None
    screenshot("waiting-narrow")
    job.detail = startup_detail(True, 70)
    app.window.queue._notify(persist=False, job=job)


def selected():
    assert app.window.rows[job.id] is state["row"]
    assert app.window.get_focus() == state["focus"]
    assert "Starting transfer" in state["row"].subtitle.get_text()
    state["dialog"] = app.window.details(job)


def details():
    def labels(widget):
        if isinstance(widget, Gtk.Label):
            yield widget.get_text()
        child = widget.get_first_child()
        while child:
            yield from labels(child)
            child = child.get_next_sibling()
    assert any("No transfer progress reported yet" in text for text in labels(state["dialog"]))
    screenshot("details-narrow")
    state["dialog"].force_close()
    job.detail, job.progress = "50%", 0.5
    app.window.queue._notify(persist=False, job=job)


def transferring():
    assert app.window.rows[job.id] is state["row"]
    assert state["row"].progress.get_fraction() == 0.5
    assert "elapsed" not in state["row"].subtitle.get_text()
    assert not state["row"].startup.get_visible()
    app.window.queue.shutdown()
    app.window.actions.shutdown(wait=True)
    app.quit()


steps = iter([first, waiting, selected, details, transferring])


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


GLib.timeout_add(800, advance)
app.run([])
temporary.cleanup()
if failures:
    print("\n".join(failures))
    raise SystemExit(1)
print("Startup feedback UI checks passed")
