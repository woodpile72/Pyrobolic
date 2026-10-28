# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Isolated disclosure, reset, persistence and narrow-layout checks."""
from dataclasses import replace
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import traceback
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pyrobolic.app import Application, Preferences, Adw, GLib, Gtk
from pyrobolic.core import Store
from pyrobolic.auth import Authentication
from gi.repository import GdkPixbuf

temporary = tempfile.TemporaryDirectory()
store = Store(Path(temporary.name) / "state")
store.settings.output = str(Path(temporary.name) / "Downloads")
store.paused = True
app = Application(store)
state, failures = {}, []
hc = bool(os.environ.get("ADW_DEBUG_HIGH_CONTRAST"))


def shot(name):
    target = Path(__file__).resolve().parents[1] / ".qa"
    target.mkdir(exist_ok=True)
    subprocess.run(["import", "-window", "root", str(target / f"disclosure{'-hc' if hc else ''}-{name}.png")], check=True)


def initial():
    app.window.queue.stopped = True
    app.window.set_default_size(440, 850)
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT if hc else Adw.ColorScheme.FORCE_DARK)
    if hc:
        Gtk.Settings.get_default().set_property("gtk-font-name", "Sans 20")
    state["prefs"] = Preferences(app.window)
    state["prefs"].present(app.window)


def settings():
    prefs = state["prefs"]
    assert not prefs.advanced.get_expanded()
    assert not prefs.dependencies_button.get_mapped()
    rows = [prefs.destination, prefs.appearance, *prefs.completion_settings.values(), prefs.retention]
    positions = [row.compute_bounds(app.window)[1].get_y() for row in rows]
    assert positions == sorted(positions)
    assert all(row.get_mapped() for row in rows)
    assert Gtk.test_accessible_has_property(prefs.advanced, Gtk.AccessibleProperty.LABEL)
    shot("settings")
    assert prefs.advanced.grab_focus()
    subprocess.run(["xdotool", "key", "space"], check=True)


def open_download():
    assert state["prefs"].advanced.get_expanded()
    assert Store(store.directory).settings.settings_expanded
    state["prefs"].close()
    state["download"] = app.window.new_download("https://example.org/fixture")


def fail_setting():
    state["failure"] = patch.object(store, "save", side_effect=OSError("Synthetic full disk"))
    state["failure"].start()
    state["prefs"].completion_settings["notifications"].set_active(False)


def retry_setting():
    prefs = state["prefs"]
    assert prefs.settings_error.get_visible() and prefs.retry_settings.get_visible()
    assert store.settings.notifications
    state["failure"].stop()
    prefs.retry_settings_save()


def setting_saved():
    assert not Store(store.directory).settings.notifications
    assert not state["prefs"].settings_error.get_visible()
    assert not state["prefs"].retry_settings.get_visible()


def inspect():
    dialog = state["download"]
    assert not dialog.more.get_expanded()
    assert not dialog.auth_button.get_mapped()
    dialog.show_message("HTTP Error 403: Forbidden")
    assert not dialog.auth_help.get_visible()
    dialog.show_message("Sign in required")
    assert dialog.auth_help.get_visible()
    dialog.show_message("")
    dialog.more.set_expanded(True)


def review():
    dialog = state["download"]
    assert dialog.auth_button.get_mapped()
    pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 24, 24)
    pixbuf.fill(0x008844ff)
    _, data = pixbuf.save_to_bufferv("png", [], [])
    state["thumbnail"] = patch("pyrobolic.thumbnail.fetch_thumbnail", return_value=data)
    state["thumbnail"].start()
    dialog.inspected([{"url": "https://example.org/fixture", "title": "Test concert", "duration": 60,
                       "thumbnail": "https://example.org/image.png"}], None)
    dialog.more.set_expanded(False)


def overrides():
    dialog = state["download"]
    assert dialog.mode.get_mapped() and dialog.name.get_mapped()
    def pictures(widget):
        child = widget.get_first_child()
        while child:
            if isinstance(child, Gtk.Picture):
                yield child
            yield from pictures(child)
            child = child.get_next_sibling()
    previews = list(pictures(dialog.item_rows[0]))
    assert len(previews) == 1 and previews[0].get_paintable() is not None
    state["thumbnail"].stop()
    shot("download")
    dialog.name.set_text("My concert")
    dialog.include_id.set_active(False)
    state["output"] = dialog.output
    item = dialog.entries[0][0]
    item.update(clip_start=10, clip_end=20)
    dialog.media_preferences = replace(dialog.media_preferences, rate_limit_kib=512,
                                        proxy="http://private.example:8080", subtitles=True)
    dialog.refresh_summary()
    assert "Time range" in dialog.summary.get_text()
    assert "Rate limit" in dialog.summary.get_text()
    assert "private.example" not in dialog.summary.get_text()
    dialog.apply_reset_options()
    assert dialog.name.get_text() == "My concert"
    assert not dialog.include_id.get_active()
    assert dialog.output == state["output"]
    assert dialog.entries[0][1].get_active()
    assert "clip_start" not in item
    assert dialog.media_preferences.rate_limit_kib == 0
    assert not dialog.media_preferences.subtitles
    dialog.more.set_expanded(True)


def playlist_reset():
    dialog = state["download"]
    dialog.url.set_text("https://example.org/playlist")
    dialog.inspected([{"url": f"https://example.org/{i}", "title": f"Concert {i}"} for i in range(3)], None)
    dialog.entries[1][1].set_active(False)
    dialog.number_items.set_active(True)
    item = dialog.entries[0][0]
    item.update(filename="Edited concert", destination=str(Path(temporary.name) / "Other"),
                clip_start=10, clip_end=20)
    dialog.authentication = Authentication(protect_url=True)
    dialog.reset_download_options()
    confirmation = app.window.get_visible_dialog()
    assert isinstance(confirmation, Adw.AlertDialog)


def respond(label):
    def buttons(widget):
        child = widget.get_first_child()
        while child:
            if isinstance(child, Gtk.Button):
                yield child
            yield from buttons(child)
            child = child.get_next_sibling()
    dialog = app.window.get_visible_dialog()
    button = next(button for button in buttons(dialog) if (button.get_label() or "").replace("_", "") == label)
    assert button.grab_focus()
    subprocess.run(["xdotool", "key", "Return"], check=True)


def playlist_reset_confirm():
    dialog = state["download"]
    assert app.window.get_visible_dialog() == dialog
    assert dialog.authentication.active and dialog.entries[0][0]["clip_start"] == 10
    dialog.reset_download_options()


def check_reset():
    dialog = state["download"]
    assert app.window.get_visible_dialog() == dialog
    assert not dialog.authentication.active
    assert [selected.get_active() for _, selected in dialog.entries] == [True, False, True]
    assert dialog.number_items.get_active()
    item = dialog.entries[0][0]
    assert item["filename"] == "Edited concert"
    assert item["destination"].endswith("/Other")
    assert "clip_start" not in item
    assert dialog.url.get_text() == "https://example.org/playlist"
    assert dialog.transfer_row.get_activatable_widget().grab_focus()


def finish():
    dialog = state["download"]
    assert Store(store.directory).settings.download_expanded
    assert Store(store.directory).settings.settings_expanded
    assert store.settings.rate_limit_kib == 0
    shot("advanced")
    dialog.close()
    app.window.queue.shutdown()
    app.window.actions.shutdown(wait=True)
    app.quit()


steps = iter([initial, settings, fail_setting, retry_setting, setting_saved,
              open_download, inspect, review, overrides, playlist_reset, lambda: respond("Cancel"),
              playlist_reset_confirm, lambda: respond("Reset"), check_reset, finish])
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

GLib.timeout_add(850, advance)
app.run([])
patch.stopall()
temporary.cleanup()
if failures:
    print("\n".join(failures))
    raise SystemExit(1)
print("Disclosure UI checks passed")
