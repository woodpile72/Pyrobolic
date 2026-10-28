# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Run on a private Xvfb display. Screenshots are written to .qa/."""

from pathlib import Path
import subprocess
import sys
import tempfile
import traceback
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pyrobolic.app import Application, Adw, GLib, Preferences
from pyrobolic.core import Job, Store
import gi

gi.require_version("GdkX11", "4.0")
from gi.repository import GdkX11


root = Path(__file__).resolve().parents[1]
output = root / ".qa"
output.mkdir(exist_ok=True)
temporary = tempfile.TemporaryDirectory()
store = Store(Path(temporary.name))
store.jobs = [
    Job("https://example.org/1", "A long title: restored concert footage with an intentionally extended description to test the available space", temporary.name,
        status="Downloading", progress=0.48, detail="48%  ·  12.4 MiB/s  ·  1:28 remaining"),
    Job("https://example.org/2", "Live recording - Original audio", temporary.name, mode="audio", status="Queued"),
    Job("https://example.org/3", "Completed recording", temporary.name, status="Completed", progress=1),
    Job("https://example.org/4", "Unavailable recording", temporary.name, status="Failed", detail="This video is unavailable"),
]
app = Application(store)
state = {}
failures = []


def screenshot(name):
    surface = app.window.get_surface()
    xid = GdkX11.X11Surface.get_xid(surface)
    subprocess.run(["import", "-window", str(xid), str(output / f"{name}.png")], check=True)
    print(name, app.window.get_width(), app.window.get_height(), flush=True)


def wide():
    app.window.queue.stopped = True
    assert len(app.window.rows) == 4
    assert app.window.pause_button.get_sensitive()
    assert app.window.folder_button.get_tooltip_text() == "Open Download Folder"
    assert app.window.folder_button.grab_focus()
    original_output = store.settings.output
    with patch.object(app.window, "open_folder") as open_folder:
        app.window.folder_button.emit("clicked")
        open_folder.assert_called_once_with(original_output)
        store.settings.output = str(Path(temporary.name) / "changed-destination")
        app.window.folder_button.emit("clicked")
        open_folder.assert_called_with(store.settings.output)
    store.settings.output = original_output
    screenshot("downloads-wide")
    app.window.toggle_queue()
    app.window.set_default_size(390, 760)


def narrow():
    assert app.window.get_width() <= 400
    okay, bounds = app.window.folder_button.compute_bounds(app.window)
    assert okay and bounds.get_x() >= 0
    assert bounds.get_x() + bounds.get_width() <= app.window.get_width()
    screenshot("downloads-narrow")
    assert app.window.queue_banner.get_revealed()
    assert "Paused" in app.window.queue_banner.get_title()
    assert app.lookup_action("cancel-all").get_enabled()
    assert app.lookup_action("retry-failed").get_enabled()
    app.window.toggle_queue()
    for row in app.window.rows.values():
        if row.get_mapped():
            okay, bounds = row.command.compute_bounds(app.window)
            assert okay and bounds.get_x() + bounds.get_width() <= app.window.get_width()
    app.window.set_default_size(880, 700)
    dialog = app.window.new_download()
    dialog.url.set_text("https://example.org/playlist")
    dialog.inspected([
        {"url": "https://example.org/1", "title": "An independently recorded concert", "duration": 456, "source": "Example"},
        {"url": "https://example.org/2", "title": "A second recording with a long title that should wrap without pushing any controls out of its row", "duration": 235, "source": "Example"},
    ], None)
    state["dialog"] = dialog


def add_wide():
    screenshot("new-download-wide")
    dialog = state["dialog"]
    dialog.select_all.set_active(False)
    assert not dialog.download.get_sensitive()
    dialog.entries[0][1].set_active(True)
    assert dialog.download.get_sensitive()
    assert dialog.items_group.get_description() == "1 of 2 selected"
    dialog.item_search.set_text("second")
    dialog.filter_items()
    assert not dialog.item_rows[0].get_visible()
    assert dialog.item_rows[1].get_visible()
    dialog.select_all.set_active(True)
    assert all(check.get_active() for _, check in dialog.entries)
    dialog.item_search.set_text("no such recording")
    dialog.filter_items()
    assert dialog.no_matches.get_visible()
    assert dialog.download.get_sensitive()
    assert not dialog.select_all.get_sensitive()
    dialog.item_search.set_text("")
    dialog.filter_items()
    assert dialog.items_group.get_description() == "2 of 2 selected"
    dialog.mode.set_selected(1)
    assert dialog.audio_format.get_visible()
    assert not dialog.quality.get_visible()
    app.window.set_default_size(390, 760)


def add_narrow():
    screenshot("new-download-narrow")
    state["dialog"].force_close()
    app.window.set_default_size(880, 700)
    preferences = Preferences(app.window)
    preferences.present(app.window)
    state["preferences"] = preferences


def preferences_wide():
    screenshot("preferences-wide")
    app.window.set_default_size(390, 760)


def preferences_narrow():
    screenshot("preferences-narrow")
    state["preferences"].force_close()
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
    app.window.stack.set_visible_child_name("history")


def history_dark():
    screenshot("history-dark-narrow")
    app.window.search.set_text("unavailable")


def filtered():
    assert sum(row.get_visible() for row in app.window.rows.values() if row.job.status in {"Completed", "Failed"}) == 1
    app.window.queue.clear_history()
    assert len(app.window.queue.snapshot()) == 2
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
    app.window.set_default_size(880, 700)
    app.window.stack.set_visible_child_name("downloads")


def clipboard_open():
    screenshot("downloads-light")
    app.window.get_display().get_clipboard().set("https://example.org/from-clipboard")
    state["clipboard_dialog"] = app.window.new_download()


def clipboard_prefilled():
    dialog = state["clipboard_dialog"]
    assert dialog.url.get_text() == "https://example.org/from-clipboard"
    assert dialog.download.get_label() == "Continue"
    assert dialog.download.get_sensitive()
    assert not dialog.options.get_visible()
    screenshot("clipboard-url-light")
    dialog.force_close()
    dialog = app.window.new_download()
    dialog.url.set_text("https://example.org/typed-by-user")
    state["clipboard_dialog"] = dialog


def clipboard_keeps_input():
    dialog = state["clipboard_dialog"]
    assert dialog.url.get_text() == "https://example.org/typed-by-user"
    dialog.force_close()
    app.window.get_display().get_clipboard().set("ordinary non-URL text")
    state["clipboard_dialog"] = app.window.new_download()


def clipboard_ignores_non_urls():
    dialog = state["clipboard_dialog"]
    assert dialog.url.get_text() == ""
    assert not dialog.download.get_sensitive()
    dialog.force_close()
    app.window.download_dialog = None


def startup_clipboard():
    app.window.get_display().get_clipboard().set("https://example.org/startup")
    app.window.offer_clipboard()


def startup_verified():
    assert app.window.download_dialog.url.get_text() == "https://example.org/startup"
    app.window.download_dialog.force_close()
    app.about()


def about_verified():
    screenshot("about-branding")
    app.window.get_visible_dialog().force_close()
    app.window.store.jobs.clear()
    app.window.queue.set_paused(True)
    dialog = app.window.new_download("https://example.org/remember-options")
    dialog.inspected([{"url": "https://example.org/remember-options", "title": "Remembered options test"}], None)
    dialog.mode.set_selected(1)
    dialog.quality.set_selected(3)
    dialog.audio_format.set_selected(3)
    dialog.container.set_selected(2)
    dialog.output = str(Path(temporary.name) / "new-folder")
    state["submitted_dialog"] = dialog
    dialog.primary_action()


def restored_options():
    dialog = state["submitted_dialog"]
    assert len(app.window.queue.snapshot()) == 1
    assert app.window.pause_button.get_sensitive()
    assert app.window.queue_banner.get_revealed()
    assert app.window.pause_button.get_icon_name() == "pyrobolic-start-symbolic"
    assert app.window.store.settings.mode == "audio"
    assert app.window.store.settings.audio_format == "opus"
    assert app.window.store.settings.output == dialog.output
    assert Store(Path(temporary.name)).settings == app.window.store.settings
    dialog = app.window.new_download("https://example.org/next")
    assert dialog.mode.get_selected() == 1
    assert dialog.audio_format.get_selected() == 3
    assert dialog.quality.get_selected() == 3
    assert dialog.container.get_selected() == 2
    assert dialog.output == app.window.store.settings.output
    assert dialog.audio_format.get_visible()
    assert not dialog.container.get_visible()
    dialog.mode.set_selected(0)
    dialog.force_close()
    assert app.window.store.settings.mode == "audio"
    app.window.queue.cancel_all()


def canceled_and_done():
    assert app.window.queue.snapshot()[0].status == "Canceled"
    assert not app.lookup_action("cancel-all").get_enabled()
    assert not app.lookup_action("retry-failed").get_enabled()
    assert not app.window.pause_button.get_sensitive()
    assert not app.window.queue_banner.get_revealed()
    app.window.closing = True
    app.window.actions.shutdown(wait=True)
    app.quit()


steps = iter([wide, narrow, add_wide, add_narrow, preferences_wide, preferences_narrow, history_dark, filtered,
              clipboard_open, clipboard_prefilled, clipboard_keeps_input, clipboard_ignores_non_urls,
              startup_clipboard, startup_verified, about_verified, restored_options, canceled_and_done])


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


app.connect("activate", lambda *_: GLib.timeout_add(700, step))
app.run([])
temporary.cleanup()
if failures:
    print("\n".join(failures), file=sys.stderr)
    raise SystemExit(1)
print("UI smoke tests passed", flush=True)
