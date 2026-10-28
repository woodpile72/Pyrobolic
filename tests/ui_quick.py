# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Quick-download interaction checks on a private display, with synthetic sources."""
from dataclasses import replace
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time
import traceback
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pyrobolic.app import Adw, Application, GLib, Gtk
from pyrobolic.core import Store
import gi
gi.require_version("GdkX11", "4.0")
from gi.repository import GdkX11

temporary = tempfile.TemporaryDirectory()
root = Path(temporary.name)
store = Store(root / "state")
store.settings.output = str(root / "media")
store.settings.mode, store.settings.audio_format = "audio", "opus"
store.settings.include_id = False
store.paused = True
app = Application(store)
state, failures, calls = {}, [], []
qa = Path(__file__).resolve().parents[1] / ".qa"
qa.mkdir(exist_ok=True)
hc = bool(os.environ.get("ADW_DEBUG_HIGH_CONTRAST"))


def fixture(url, process, *_):
    calls.append(url)
    if url.endswith("cancel"):
        state["canceled_process"] = process
        process.communicate([sys.executable, "-c", "import time; time.sleep(30)"], timeout=40)
        return []
    if url.endswith("failure"):
        raise RuntimeError("Synthetic inspection failure")
    items = [{"url": url, "title": "1.5M views · 46K reactions | Concert", "media_id": "42", "source": "Facebook",
              "from_collection": url.endswith("playlist")}]
    if url.endswith("multiple"):
        items.append({"url": "https://example.org/second", "title": "Second"})
    return items


mock = patch("pyrobolic.app.inspect_url", side_effect=fixture)
mock.start()


def key(widget, value):
    assert widget.grab_focus()
    xid = GdkX11.X11Surface.get_xid(app.window.get_surface())
    subprocess.run(["xdotool", "windowfocus", "--sync", str(xid), "key", "--clearmodifiers", value], check=True)


def screenshot(name):
    subprocess.run(["import", "-window", "root", str(qa / f"quick{'-hc' if hc else ''}-{name}.png")], check=True)


def open_dialog(name):
    state["dialog"] = app.window.new_download("https://example.org/" + name)
    return state["dialog"]


def menu_button(dialog):
    return dialog.quick_menu


def start():
    app.window.queue.stopped = True
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT if hc else Adw.ColorScheme.FORCE_DARK)
    if hc:
        Gtk.Settings.get_default().set_property("gtk-font-name", "Sans 20")
    app.window.set_default_size(420 if hc else 390, 760)
    app.window.get_display().get_clipboard().set("https://example.org/clipboard")
    state["dialog"] = app.window.new_download()


def clipboard():
    dialog = state["dialog"]
    assert dialog.url.get_text() == "https://example.org/clipboard"
    assert not calls and not store.jobs
    dialog.url.set_text("invalid")
    assert not dialog.download.get_sensitive() and not dialog.quick_button.get_sensitive()
    dialog.quick_download()
    assert not calls
    dialog.url.set_text("https://example.org/review")
    key(menu_button(dialog), "space")


def menu():
    dialog = state["dialog"]
    assert dialog.quick_popover.get_visible()
    screenshot("menu-narrow")
    for widget in (menu_button(dialog), dialog.quick_button):
        assert Gtk.test_accessible_has_property(widget, Gtk.AccessibleProperty.LABEL)
        container = dialog.quick_popover if widget == dialog.quick_button else app.window
        okay, rect = widget.compute_bounds(container)
        assert okay and rect.get_x() >= -1
        assert rect.get_x() + rect.get_width() <= container.get_width() + 1, rect
    surface = dialog.quick_popover.get_native().get_surface()
    assert surface.get_width() <= app.window.get_width()
    assert "opus" in dialog.quick_summary.get_text()
    assert store.settings.output in dialog.quick_summary.get_text()
    key(dialog.quick_button, "Escape")


def ordinary():
    dialog = state["dialog"]
    assert not dialog.quick_popover.get_visible()
    assert not calls
    key(dialog.url, "Return")


def reviewed():
    dialog = state["dialog"]
    if dialog.process:
        return False
    assert len(dialog.entries) == 1 and not store.jobs
    assert not dialog.quick_menu.get_visible()
    dialog.force_close()
    open_dialog("quick")


def quick_menu():
    key(menu_button(state["dialog"]), "space")


def quick_start():
    dialog = state["dialog"]
    key(dialog.quick_button, "space")
    dialog.quick_download()


def queued():
    dialog = state["dialog"]
    if dialog.process or dialog.saving:
        return False
    assert dialog.closed and len(store.jobs) == 1
    assert calls.count("https://example.org/quick") == 1
    job = store.jobs[0]
    assert job.mode == "audio" and job.audio_format == "opus"
    assert job.source_format is None and job.source_audio is None
    assert job.filename == "Concert", job.filename
    assert job.output == store.settings.output
    assert not job.include_id and job.output_stem
    saved = Store(store.directory).jobs[0]
    assert saved.filename == job.filename and saved.audio_format == "opus"
    job.status = "Failed"
    app.window.queue.retry(job.id)
    assert job.status == "Queued" and job.filename == "Concert"
    open_dialog("playlist").quick_download()


def playlist():
    dialog = state["dialog"]
    if dialog.process:
        return False
    assert len(dialog.entries) == 1 and len(store.jobs) == 1
    assert "Review" in dialog.message.get_text()
    dialog.force_close()
    open_dialog("multiple").quick_download()


def multiple():
    dialog = state["dialog"]
    if dialog.process:
        return False
    assert len(dialog.entries) == 2 and len(store.jobs) == 1
    dialog.force_close()
    open_dialog("failure").quick_download()


def inspection_failure():
    dialog = state["dialog"]
    if dialog.process:
        return False
    assert "Synthetic inspection failure" in dialog.message.get_text()
    assert dialog.quick_button.get_sensitive() and dialog.download.get_sensitive()
    assert len(store.jobs) == 1
    dialog.url.set_text("https://example.org/cancel")
    dialog.quick_download()


def cancel():
    process = state.get("canceled_process")
    if not process or not process.process:
        return False
    state["dialog"].force_close()


def canceled():
    process = state["canceled_process"]
    if not process.done.is_set() or state["dialog"].process:
        return False
    assert process.cancelled.is_set() and process.process.poll() is not None
    assert len(store.jobs) == 1
    store.settings = replace(store.settings, mode="audio", subtitle_mode="embed", subtitles=False)
    open_dialog("incompatible").quick_download()


def incompatible():
    dialog = state["dialog"]
    if dialog.process:
        return False
    assert not dialog.download.get_sensitive() and len(store.jobs) == 1
    assert dialog.format_error.get_visible() and "review" in dialog.message.get_text()
    dialog.force_close()
    store.settings = replace(store.settings, subtitle_mode="off")
    state["save_patch"] = patch.object(store, "save", side_effect=OSError("Synthetic save failure"))
    state["save_patch"].start()
    open_dialog("save").quick_download()


def save_failure():
    dialog = state["dialog"]
    if dialog.process or dialog.saving:
        return False
    assert not dialog.closed and len(store.jobs) == 1
    assert "Synthetic save failure" in dialog.message.get_text()
    assert dialog.entries and dialog.download.get_sensitive()
    state["save_patch"].stop()
    app.window.queue.retry_save()
    dialog.enqueue()
    dialog.enqueue()


def saved():
    if state["dialog"].saving:
        return False
    assert state["dialog"].closed and len(store.jobs) == 2
    assert len(Store(store.directory).jobs) == 2
    assert not app.window.processes
    app.window.queue.shutdown()
    app.window.actions.shutdown(wait=True)
    app.quit()


steps = iter([start, clipboard, menu, ordinary, reviewed, quick_menu, quick_start, queued, playlist,
              multiple, inspection_failure, cancel, canceled, incompatible, save_failure, saved])
current, deadline = next(steps), time.monotonic() + 15


def advance():
    global current, deadline
    try:
        assert time.monotonic() < deadline, f"Timed out in {current.__name__}"
        if current() is not False:
            current = next(steps)
            deadline = time.monotonic() + 15
    except StopIteration:
        return False
    except Exception:
        failures.append(traceback.format_exc())
        app.window.queue.shutdown()
        for process in list(app.window.processes):
            process.cancel()
            process.close()
        app.window.actions.shutdown(wait=True)
        app.quit()
        return False
    return True


GLib.timeout_add(700, advance)
app.run([])
mock.stop()
temporary.cleanup()
if failures:
    print("\n".join(failures))
    raise SystemExit(1)
print("Quick download UI checks passed")
