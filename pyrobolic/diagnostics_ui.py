# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
import threading

from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from .core import Process
from .diagnostics import TOOLS, check_dependencies


class DependenciesDialog(Adw.Dialog):
    def __init__(self, window):
        from .app import button, label, margins, scrolled
        super().__init__(title="Dependencies", content_width=540, content_height=580)
        self.window = window
        self.closed = False
        self.processes = []
        self.results = []
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        self.dismiss = button("window-close-symbolic", "Close Dependencies", lambda *_: self.close())
        header.pack_end(self.dismiss)
        self.refresh = button("view-refresh-symbolic", "Check Dependencies Again", self.check)
        header.pack_end(self.refresh)
        toolbar.add_top_bar(header)
        page = margins(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12))
        self.status = label("Checking local tools", wrap=True)
        page.append(self.status)
        self.labels = {}
        for name in TOOLS:
            group = Adw.PreferencesGroup(title=name)
            value = label("Checking", selectable=True, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
            value.update_property([Gtk.AccessibleProperty.LABEL], [name + " Dependency Status"])
            group.add(value)
            page.append(group)
            self.labels[name] = value
        page.append(label("Version checks do not establish site compatibility or available codecs. No download history, cookies or browser profiles are read.",
                          wrap=True, max_width_chars=40))
        toolbar.set_content(scrolled(page))
        self.set_child(toolbar)
        self.set_focus(self.dismiss)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self.key_pressed)
        self.add_controller(keys)
        self.connect("closed", self.on_closed)
        self.check()

    def key_pressed(self, _controller, keyval, _keycode, _state):
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False

    def check(self, *_):
        if self.closed or self.window.closing or self.processes:
            return
        if self.refresh.has_focus():
            self.dismiss.grab_focus()
        self.refresh.set_sensitive(False)
        self.status.set_text("Checking local tools")
        self.results = []
        for value in self.labels.values():
            value.set_text("Checking")
        processes = [Process() for _ in TOOLS]
        self.processes = processes
        self.window.processes.update(processes)

        def work():
            results, error = [], ""
            try:
                results = check_dependencies(self.window.store.directory, processes)
            except InterruptedError:
                error = "Dependency check canceled"
            except Exception:
                error = "Unable to check dependencies. Try again."
            finally:
                for process in processes:
                    process.close()
            GLib.idle_add(self.finished, processes, results, error)

        threading.Thread(target=work, daemon=True, name="pyro-dependencies").start()

    def finished(self, processes, results, error):
        self.window.processes.difference_update(processes)
        self.processes = []
        if self.closed or self.window.closing:
            return False
        self.results = results
        self.refresh.set_sensitive(True)
        self.status.set_text(error or "Local checks complete")
        for result in results:
            self.labels[result.name].set_text("\n".join(value for value in
                (result.status, result.version, result.path, result.detail) if value))
        return False

    def on_closed(self, *_):
        self.closed = True
        for process in self.processes:
            process.cancel()
