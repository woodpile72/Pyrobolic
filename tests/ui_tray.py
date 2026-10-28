# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Private-bus GTK lifecycle and StatusNotifier/dbusmenu integration checks."""
from dataclasses import replace
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time
import traceback
from unittest.mock import patch

sys.path.insert(0, os.environ.get("PYROBOLIC_TEST_ROOT", str(Path(__file__).resolve().parents[1])))
from pyrobolic.app import Application, Adw, Gio, GLib, Gtk, Preferences
from pyrobolic.core import Store, Job
from pyrobolic.tray import WATCHER, ITEM, MENU, interface_xml

temporary = tempfile.TemporaryDirectory()
store = Store(Path(temporary.name) / "state")
store.settings.output = str(Path(temporary.name) / "Downloads")
store.settings.background_mode = True
store.save()
app = Application(store)
failures = []
state = {}
connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
host_available = False
registrations = []


def register(_connection, _sender, _path, _interface, _method, args, invocation):
    registrations.append(args.unpack()[0])
    invocation.return_value(None)


info = interface_xml(WATCHER, [("RegisterStatusNotifierItem", ["s"], [])],
                     [("IsStatusNotifierHostRegistered", "b")])
object_id = connection.register_object("/StatusNotifierWatcher", info, register,
                                      lambda *_: GLib.Variant("b", host_available), None)


def own_host():
    state["owner"] = Gio.bus_own_name_on_connection(connection, WATCHER, Gio.BusNameOwnerFlags.NONE, None, None)


def hide(window):
    window.close()
    deadline = time.monotonic() + 2
    while window.get_visible() and time.monotonic() < deadline:
        GLib.MainContext.default().iteration(False)
        time.sleep(.005)
    assert not window.get_visible()


def bus_call(interface, method, args, path="/Menu"):
    def done(bus, result):
        try:
            state["reply"] = bus.call_finish(result).unpack()
        except Exception:
            failures.append(traceback.format_exc())
    state.pop("reply", None)
    connection.call(app.tray.name, path, interface, method, args, None,
                    Gio.DBusCallFlags.NONE, 2000, None, done)


def initial():
    assert app.window.get_visible()
    assert not app.tray.ready
    with patch.object(app.window, "request_quit") as quit:
        app.window.close_requested()
        quit.assert_called_once()
    own_host()


def hidden():
    assert app.tray.ready and app.background_held
    assert len(registrations) == 1
    window = app.window
    for _ in range(4):
        hide(window)
        assert not window.get_visible() and not window.closing
        assert not window.queue.stopped
        app.activate()
        assert app.window is window and window.get_visible()
    hide(window)
    bus_call(ITEM, "Activate", GLib.Variant("(ii)", (0, 0)), "/StatusNotifierItem")


def unavailable_host():
    global host_available
    assert not app.tray.ready and not registrations
    assert app.window.get_visible()
    host_available = True
    app.tray.probe()


def layout():
    assert app.window.get_visible()
    bus_call(MENU, "GetLayout", GLib.Variant("(iias)", (0, -1, [])))


def add():
    revision, root = state["reply"]
    assert revision == 1
    children = root[2]
    assert [child[1].get("label") for child in children] == ["Show Pyrobolic", "Add Download", None, "Quit"]
    assert children[2][1]["type"] == "separator"
    bus_call(MENU, "Event", GLib.Variant("(isvu)", (2, "clicked", GLib.Variant("i", 0), 0)))


def duplicate():
    dialog = app.window.download_dialog
    assert dialog and not dialog.closed
    app.tray_add()
    assert app.window.download_dialog is dialog
    assert not store.jobs
    dialog.close()
    hide(app.window)
    Gio.bus_unown_name(state.pop("owner"))


def lost():
    assert not app.tray.ready and app.window.get_visible() and not app.background_held
    assert not app.window.queue.stopped
    own_host()


def restart():
    assert app.tray.ready and len(registrations) == 2
    hide(app.window)
    app.window.queue.configure(replace(store.settings, background_mode=False))


def disabled():
    assert app.window.get_visible() and not app.tray.ready and not app.tray.objects
    prefs = Preferences(app.window)
    state["prefs"] = prefs
    prefs.present(app.window)
    assert not prefs.background.get_active() and not prefs.tray_style.get_sensitive()
    prefs.background.set_active(True)


def appearance():
    assert app.tray.ready and state["prefs"].tray_style.get_sensitive()
    state["prefs"].tray_style.set_selected(0)


def saved():
    assert Store(store.directory).settings.tray_appearance == "color"
    assert app.tray.appearance == "color"
    assert len(registrations) == 3
    prefs = state["prefs"]
    prefs.tray_style.set_selected(1)
    app.window.set_default_size(440, 850)
    Gtk.Settings.get_default().set_property("gtk-font-name", "Sans 20")
    assert prefs.tray_style.grab_focus()


def snapshot():
    target = Path(__file__).resolve().parents[1] / ".qa"
    target.mkdir(exist_ok=True)
    if not os.environ.get("PYROBOLIC_TEST_ROOT"):
        subprocess.run(["import", "-window", "root", str(target / "tray-settings.png")], check=True)
    state["prefs"].close()
    assert app.tray.appearance == "symbolic"
    app.window.queue.set_paused(True)
    store.settings.concurrency = 1
    state["jobs"] = [Job(f"https://example.org/{i}", f"Fixture {i}", store.settings.output) for i in range(2)]
    def command(job, _engine):
        output = Path(job.output) / (job.title + ".mp4")
        code = ("import time,json; from pathlib import Path; time.sleep(1); "
                "print('PYRO_PROCESS:processing',flush=True); time.sleep(1); "
                f"Path({str(output)!r}).write_bytes(b'fixture'); "
                f"print('PYRO_FILE:'+json.dumps({str(output)!r}),flush=True)")
        return [sys.executable, "-c", code]
    state["engine"] = patch("pyrobolic.core.engine_path", return_value="fixture")
    state["command"] = patch("pyrobolic.core.download_arguments", side_effect=command)
    state["engine"].start()
    state["command"].start()
    app.window.queue.add(state["jobs"])
    hide(app.window)
    assert not app.window.get_visible() and store.paused
    assert [job.status for job in state["jobs"]] == ["Queued", "Queued"]
    app.window.queue.set_paused(False)


def downloading():
    assert not app.window.get_visible()
    assert state["jobs"][0].status == "Downloading"
    assert state["jobs"][1].status == "Queued"
    app.window.queue.pause_transfer(state["jobs"][0].id, True)


def paused():
    assert not app.window.get_visible()
    assert state["jobs"][0].transfer_paused
    app.window.queue.pause_transfer(state["jobs"][0].id, False)


def processing():
    assert not app.window.get_visible()
    assert state["jobs"][0].status == "Processing"


def next_job():
    assert not app.window.get_visible()
    assert state["jobs"][0].status == "Completed"


def finished():
    assert all(job.status == "Completed" for job in state["jobs"])
    assert all(job.files and all(Path(file).stat().st_size for file in job.files) for job in state["jobs"])
    assert not app.window.get_visible() and app.background_held
    state["engine"].stop()
    state["command"].stop()
    state["failure"] = patch.object(store, "save", side_effect=OSError("Fixture disk full"))
    state["failure"].start()
    app.activate_action("quit", None)


def failed_save():
    assert app.window.get_visible()
    dialog = app.window.get_visible_dialog()
    assert isinstance(dialog, Adw.AlertDialog) and dialog.get_default_response() == "stay"
    dialog.emit("response", "stay")
    dialog.close()
    state["failure"].stop()
    state.pop("failure")
    assert not app.window.closing and not app.window.queue.stopped
    assert app.tray.ready
    app.window.queue.retry_save()
    hide(app.window)
    bus_call(MENU, "Event", GLib.Variant("(isvu)", (4, "clicked", GLib.Variant("i", 0), 0)))


def reveal_settings():
    row = state["prefs"].tray_style
    scroller = row.get_ancestor(Gtk.ScrolledWindow)
    assert scroller is not None
    okay, bounds = row.compute_bounds(scroller)
    assert okay
    adjustment = scroller.get_vadjustment()
    adjustment.set_value(adjustment.get_value() + bounds.get_y() - 150)


steps = iter([initial, unavailable_host, hidden, layout, add, duplicate, lost, restart, disabled,
              appearance, saved, reveal_settings, snapshot, downloading, paused, processing, next_job,
              lambda: None, lambda: None, finished, failed_save])
started = time.monotonic()


def advance():
    try:
        next(steps)()
    except StopIteration:
        failures.append("Explicit Quit did not exit")
        app.quit()
        return False
    except Exception:
        failures.append(traceback.format_exc())
        app.quit()
        return False
    return True


def unhandled(kind, error, trace):
    failures.append("".join(traceback.format_exception(kind, error, trace)))
    app.quit()


sys.excepthook = unhandled
GLib.timeout_add(700, advance)
app.run([])
if "failure" in state:
    state["failure"].stop()
if "owner" in state:
    Gio.bus_unown_name(state["owner"])
connection.unregister_object(object_id)
temporary.cleanup()
assert not app.tray.ready and not app.background_held
if failures:
    print("\n".join(failures))
    raise SystemExit(1)
print(f"Tray host lifecycle, menu, settings, failed-save recovery and explicit quit passed ({time.monotonic() - started:.1f}s)")
