# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Isolated recovery, persistence-error and large-history interaction checks."""

import json
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time
import traceback
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pyrobolic.app import Application, GLib, Gtk, RecoveryWindow, Window
from pyrobolic.core import Job, Process
import gi
gi.require_version("GdkX11", "4.0")
from gi.repository import GdkX11

temporary = tempfile.TemporaryDirectory()
directory = Path(temporary.name)
path = directory / "state.json"
damaged = b'{"settings":broken'
path.write_bytes(damaged)
app = Application(directory=directory)
output = Path(__file__).resolve().parents[1] / ".qa"
output.mkdir(exist_ok=True)
state, failures, heartbeats = {}, [], []


def capture(name):
    surface = app.window.get_surface()
    xid = GdkX11.X11Surface.get_xid(surface)
    subprocess.run(["import", "-window", str(xid), str(output / (name + ".png"))], check=True)


def error_visible():
    window = app.window
    assert isinstance(window, RecoveryWindow) and not window.busy
    assert window.page.get_title() == "Unable to Read Saved Data"
    assert path.read_bytes() == damaged
    assert window.retry.get_sensitive()
    window.retry.grab_focus()
    assert window.get_focus() is window.retry
    window.child_focus(Gtk.DirectionType.TAB_FORWARD)
    assert window.get_focus() is window.restore
    window.set_default_size(390, 600)


def fresh_confirmation():
    capture("recovery-narrow")
    window = app.window
    for control in (window.retry, window.restore, window.fresh):
        okay, bounds = control.compute_bounds(window)
        assert okay and 0 <= bounds.get_x() and bounds.get_x() + bounds.get_width() <= window.get_width()
    window.confirm_fresh()
    state["confirmation"] = window.get_visible_dialog()


def cancel_fresh():
    dialog = state["confirmation"]
    assert dialog.get_default_response() == "cancel"
    dialog.force_close()
    assert path.read_bytes() == damaged
    bad_source = directory / "invalid.json"
    bad_source.write_text("invalid replacement")
    app.window.load(recover=True, source=bad_source)


def invalid_restore():
    assert not app.window.busy
    assert app.window.page.get_title() == "Unable to Read Saved Data"
    assert path.read_bytes() == damaged
    app.window.confirm_fresh()
    dialog = app.window.get_visible_dialog()
    dialog.emit("response", "fresh")
    dialog.force_close()


def fresh_recovered():
    assert isinstance(app.window, Window)
    assert not app.window.store.jobs
    assert not app.window.pause_button.get_sensitive()
    assert next(directory.glob("state-damaged-*")).read_bytes() == damaged
    if app.window.get_visible_dialog():
        app.window.get_visible_dialog().force_close()
    app.window.queue.stopped = True
    state["save_patch"] = patch.object(app.window.store, "save", side_effect=OSError("Fixture: disk full"))
    state["save_patch"].start()
    app.window.toggle_queue()


def save_error():
    assert app.window.save_banner.get_revealed()
    assert "disk full" in app.window.save_banner.get_tooltip_text()
    app.window.set_default_size(390, 650)


def retry_saving():
    assert app.window.get_width() <= 400
    capture("save-error-narrow")
    state["save_patch"].stop()
    app.window.save_banner.emit("button-clicked")


def save_retried():
    assert not app.window.save_banner.get_revealed()
    assert json.loads(path.read_text())["paused"]
    store = app.window.store
    store.jobs = [Job(f"https://example.org/{i}", f"History item {i}", str(directory),
                      status="Completed", log=[f"History {i} entry {line}" for line in range(200)])
                  for i in range(10000)]
    state["active"] = [Job(f"https://example.org/active/{i}", f"Active {i}", str(directory), status="Downloading")
                       for i in range(6)]
    store.jobs.extend(state["active"])
    state["render_start"] = time.monotonic()
    with app.window.queue.lock:
        app.window.queue._notify(persist=False)


def large_history():
    assert len(app.window.rows) == 106
    assert app.window.pause_button.get_sensitive()
    state["initial_render_upper_ms"] = (time.monotonic() - state["render_start"]) * 1000
    state["history_updates"] = 0
    for row in app.window.rows.values():
        if row.job.status == "Completed":
            original = row.update
            def updated(job, original=original):
                state["history_updates"] += 1
                original(job)
            row.update = updated
    state["measuring"] = True
    state["cpu_start"] = time.process_time()
    state["wall_start"] = time.monotonic()
    def tick():
        if not state["measuring"]:
            return False
        heartbeats.append(time.monotonic())
        return True
    def progress():
        if not state["measuring"]:
            return False
        with app.window.queue.lock:
            for job in state["active"]:
                job.progress = (job.progress + .01) % 1
                app.window.queue._notify(persist=False, job=job)
        return True
    GLib.timeout_add(10, tick)
    GLib.timeout_add(30, progress)


def measuring():
    assert state["history_updates"] == 0


def benchmark_done():
    state["measuring"] = False
    assert state["history_updates"] == 0
    gaps = sorted((b - a) * 1000 for a, b in zip(heartbeats, heartbeats[1:]))
    p95 = gaps[int(len(gaps) * .95)]
    assert p95 < 100, p95
    report = {"history_records": 10000, "log_lines_each": 200, "active_jobs": 6,
              "progress_interval_ms": 30, "history_rows_updated_during_progress": state["history_updates"],
              "rendered_rows": len(app.window.rows), "heartbeat_p95_ms": p95,
              "heartbeat_max_ms": max(gaps), "wall_seconds": time.monotonic() - state["wall_start"],
              "cpu_seconds": time.process_time() - state["cpu_start"],
              "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
              "note": "Synthetic current-version GTK benchmark, not a Parabolic comparison or before/after benchmark"}
    (output / "reliability-performance.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)
    app.window.stack.set_visible_child_name("history")
    app.window.search.set_text("History item 9999")


def history_search():
    assert len(app.window.rows) == 7
    assert any(row.job.title == "History item 9999" for row in app.window.rows.values())
    app.window.search.set_text("")


def history_paging():
    assert len(app.window.rows) == 106
    app.window.more_history.grab_focus()
    app.window.more_history.emit("clicked")
    assert len(app.window.rows) == 206
    capture("history-paged-narrow")
    app.window.store.jobs.clear()
    process = Process()
    process.start([sys.executable, "-c", "import time; time.sleep(30)"])
    app.window.processes.add(process)
    state["close_process"] = process
    state["save_patch"].start()
    app.window.finish_close()
    assert app.window.close_requested()  # A repeated close cannot bypass worker cleanup.


def failed_close():
    assert state["close_process"].done.is_set()
    dialog = app.window.get_visible_dialog()
    assert dialog.get_heading() == "Unable to Save Changes"
    assert dialog.get_default_response() == "stay"
    dialog.emit("response", "stay")
    dialog.force_close()
    assert not app.window.closing
    assert app.window.get_sensitive()
    state["save_patch"].stop()
    app.window.run_action(app.window.queue.retry_save)


def clean_close():
    assert not app.window.queue.persistence_error
    state["closed_cleanly"] = True
    app.window.finish_close()


steps = iter([error_visible, fresh_confirmation, cancel_fresh, invalid_restore, fresh_recovered,
              save_error, retry_saving, save_retried, large_history, measuring, measuring, measuring,
              benchmark_done, history_search, history_paging, failed_close, clean_close])


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
if state.get("save_patch"):
    state["save_patch"].stop()
if isinstance(app.window, Window):
    app.window.actions.shutdown(wait=True)
temporary.cleanup()
if failures:
    print("\n".join(failures), file=sys.stderr)
    raise SystemExit(1)
assert state.get("closed_cleanly")
print("Reliability UI checks passed", flush=True)
