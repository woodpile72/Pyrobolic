# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Saved sort and appearance controls, stable rows and failed-save rollback."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import traceback
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pyrobolic.app import Application, Adw, GLib, Gtk, Preferences
from pyrobolic.core import Job, Store
import gi
gi.require_version("GdkX11", "4.0")
from gi.repository import GdkX11

temporary = tempfile.TemporaryDirectory()
store = Store(Path(temporary.name) / "state")
store.jobs = [Job(f"https://example.org/{i}", f"Source {i}", temporary.name, filename=f"Title {249-i:03}",
                  output_stem=f"Title {249-i:03}", status="Completed", created=float(i+1)) for i in range(250)]
store.save()
app = Application(store)
state, failures = {}, []
hc = bool(os.environ.get("ADW_DEBUG_HIGH_CONTRAST"))


def key(value):
    subprocess.run(["xdotool", "key", "--clearmodifiers", value], check=True)


def initial():
    app.window.set_default_size(440, 850)
    if hc:
        Gtk.Settings.get_default().set_property("gtk-font-name", "Sans 20")
    app.window.queue.stopped = True
    app.window.switch_view("history")
    assert app.window.stack.get_page(app.window.pages["history"]).get_title() == "History"
    assert app.window.stack.get_page(app.window.pages["downloads"]).get_title() == "Downloads"
    state["first_row"] = app.window.lists["history"].get_row_at_index(0)
    assert state["first_row"].job.id == store.jobs[-1].id


def open_menu():
    app.window.sort_button.grab_focus()
    key("space")


def choose_oldest():
    def descendants(widget):
        yield widget
        child = widget.get_first_child()
        while child:
            yield from descendants(child)
            child = child.get_next_sibling()
    popover = app.window.sort_button.get_popover()
    assert popover.get_visible()
    target = next(w for w in descendants(popover)
                  if w.find_property("text") and w.get_property("text") == "Oldest First")
    target.grab_focus()
    key("Return")


def oldest():
    assert store.settings.history_sort == "oldest"
    assert app.window.lists["history"].get_row_at_index(0).job.id == store.jobs[0].id
    assert len(app.window.rows) == 100
    app.window.show_download(store.jobs[-1].id)


def reveal_and_sort():
    assert app.window.history_limit == 250
    state["retained"] = app.window.rows[store.jobs[-1].id]
    app.window.change_history_sort("title")


def title_sorted():
    assert store.settings.history_sort == "title"
    assert app.window.lists["history"].get_row_at_index(0) is state["retained"]
    assert app.window.history_limit == 100
    assert Store(store.directory).settings.history_sort == "title"
    state["save_patch"] = patch.object(store, "save", side_effect=OSError("Simulated settings save failure"))
    state["save_patch"].start()
    app.window.change_history_sort("oldest")


def failed_sort():
    assert store.settings.history_sort == "title"
    assert app.window.sort_action.get_state().get_string() == "title"
    assert app.window.sort_action.get_enabled()
    state["save_patch"].stop()
    state["preferences"] = Preferences(app.window)
    state["preferences"].present(app.window)


def choose_dark():
    row = state["preferences"].appearance
    assert Gtk.test_accessible_has_property(row, Gtk.AccessibleProperty.LABEL)
    row.grab_focus()
    key("space")


def dark():
    key("End")
    key("Return")


def verify_dark():
    assert store.settings.appearance == "dark"
    assert Adw.StyleManager.get_default().get_color_scheme() == Adw.ColorScheme.FORCE_DARK
    assert Store(store.directory).settings.appearance == "dark"
    state["save_patch"] = patch.object(store, "save", side_effect=OSError("Simulated appearance save failure"))
    state["save_patch"].start()
    state["preferences"].appearance.set_selected(1)


def failed_appearance():
    assert store.settings.appearance == "dark"
    assert state["preferences"].appearance.get_selected() == 2
    assert state["preferences"].appearance.get_sensitive()
    state["save_patch"].stop()
    state["preferences"].appearance.set_selected(1)


def light():
    assert store.settings.appearance == "light"
    assert Adw.StyleManager.get_default().get_color_scheme() == Adw.ColorScheme.FORCE_LIGHT
    state["preferences"].appearance.set_selected(0)


def system():
    assert store.settings.appearance == "system"
    assert Adw.StyleManager.get_default().get_color_scheme() == Adw.ColorScheme.DEFAULT
    state["preferences"].close()


def capture():
    for widget in (app.window.search, app.window.sort_button):
        okay, bounds = widget.compute_bounds(app.window)
        assert okay and bounds.get_x() >= 0 and bounds.get_x() + bounds.get_width() <= app.window.get_width() + 1
    app.window.sort_button.grab_focus()


def finish():
    if app.window.bottom_switcher.get_reveal():
        okay, toast_bounds = app.window.toast.compute_bounds(app.window)
        visible, nav_bounds = app.window.bottom_switcher.compute_bounds(app.window)
        assert okay and visible and toast_bounds.get_y() + toast_bounds.get_height() <= nav_bounds.get_y() + 1
    output = Path(__file__).resolve().parents[1] / ".qa"
    subprocess.run(["import", "-window", str(GdkX11.X11Surface.get_xid(app.window.get_surface())),
                    str(output / ("history-sort-hc.png" if hc else "history-sort.png"))], check=True)
    app.window.queue.shutdown()
    app.window.actions.shutdown(wait=True)
    app.quit()


steps = iter([initial, open_menu, choose_oldest, oldest, reveal_and_sort, title_sorted, failed_sort, choose_dark,
              dark, verify_dark, failed_appearance, light, system, capture, finish])
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
print("History sort and appearance UI checks passed")
