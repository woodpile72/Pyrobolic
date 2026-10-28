# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Optional StatusNotifierItem/dbusmenu adapter, using Gio without GTK 3.

All bus work is asynchronous. A dedicated bus name makes removal observable to
hosts without closing the application's shared D-Bus connection.
"""
import os
from pathlib import Path

import gi
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gio, GLib, GdkPixbuf


WATCHER = "org.kde.StatusNotifierWatcher"
ITEM = "org.kde.StatusNotifierItem"
MENU = "com.canonical.dbusmenu"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/Menu"


def interface_xml(name, methods, properties, signals=()):
    parts = [f'<node><interface name="{name}">']
    for method, inputs, outputs in methods:
        parts.append(f'<method name="{method}">')
        for direction, types in (("in", inputs), ("out", outputs)):
            parts.extend(f'<arg type="{kind}" direction="{direction}"/>' for kind in types)
        parts.append('</method>')
    parts.extend(f'<property name="{key}" type="{kind}" access="read"/>' for key, kind in properties)
    for signal, types in signals:
        parts.append(f'<signal name="{signal}">')
        parts.extend(f'<arg type="{kind}"/>' for kind in types)
        parts.append('</signal>')
    return Gio.DBusNodeInfo.new_for_xml(''.join(parts) + '</interface></node>').interfaces[0]


ITEM_INFO = interface_xml(ITEM, [
    ("Activate", ["i", "i"], []), ("SecondaryActivate", ["i", "i"], []),
    ("ContextMenu", ["i", "i"], []), ("Scroll", ["i", "s"], []),
], [(key, "s") for key in ("Category", "Id", "Title", "Status", "IconName", "IconThemePath",
                            "OverlayIconName", "AttentionIconName", "AttentionMovieName")] + [
    ("WindowId", "u"), ("ItemIsMenu", "b"), ("Menu", "o"),
    ("IconPixmap", "a(iiay)"), ("OverlayIconPixmap", "a(iiay)"),
    ("AttentionIconPixmap", "a(iiay)"), ("ToolTip", "(sa(iiay)ss)"),
], [("NewIcon", []), ("NewStatus", ["s"])])

MENU_INFO = interface_xml(MENU, [
    ("GetLayout", ["i", "i", "as"], ["u", "(ia{sv}av)"]),
    ("GetGroupProperties", ["ai", "as"], ["a(ia{sv})"]),
    ("GetProperty", ["i", "s"], ["v"]),
    ("Event", ["i", "s", "v", "u"], []),
    ("EventGroup", ["a(isvu)"], ["ai"]),
    ("AboutToShow", ["i"], ["b"]), ("AboutToShowGroup", ["ai"], ["ai", "ai"]),
], [("Version", "u"), ("TextDirection", "s"), ("Status", "s")],
    [("LayoutUpdated", ["u", "i"])])


class Tray:
    def __init__(self, show, add, quit, availability):
        self.actions = {1: show, 2: add, 4: quit}
        self.availability = availability
        self.ready = False
        self.enabled = False
        self.appearance = "symbolic"
        self.connection = None
        self.owner = 0
        self.watch = 0
        self.timer = 0
        self.objects = []
        self.generation = 0
        self.host = None
        self.registered = False
        self.pending = False
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self.pixmaps = {}

    def set_ready(self, ready):
        if self.ready != ready:
            self.ready = ready
            self.availability(ready)

    def configure(self, enabled, appearance):
        changed = self.appearance != appearance
        self.appearance = appearance
        if enabled == self.enabled:
            if changed and self.connection and self.objects:
                self.connection.emit_signal(None, ITEM_PATH, ITEM, "NewIcon", None)
            return
        self.enabled = enabled
        if not enabled:
            self.close()
            return
        self.generation += 1
        if not self.pixmaps:
            directory = Path(__file__).resolve().parent / "tray-icons"
            for style, prefix in (("symbolic", ""), ("color", "color-")):
                self.pixmaps[style] = []
                for size in (16, 24, 32, 48):
                    pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(directory / f"pyrobolic-tray-{prefix}fallback-{size}.png"))
                    rgba = pixbuf.get_pixels()
                    argb = bytearray()
                    for y in range(size):
                        for x in range(size):
                            offset = y * pixbuf.get_rowstride() + x * 4
                            argb.extend((rgba[offset + 3], *rgba[offset:offset + 3]))
                    self.pixmaps[style].append((size, size, bytes(argb)))
        generation = self.generation
        def connected(_source, result):
            try:
                connection = Gio.bus_get_finish(result)
            except GLib.Error:
                return
            if generation != self.generation or not self.enabled:
                return
            self.connection = connection
            self.objects = [connection.register_object(ITEM_PATH, ITEM_INFO, self.item_call, self.item_property, None),
                            connection.register_object(MENU_PATH, MENU_INFO, self.menu_call, self.menu_property, None)]
            self.owner = Gio.bus_own_name_on_connection(connection, self.name, Gio.BusNameOwnerFlags.NONE,
                                                       self.name_acquired, self.name_lost)
        Gio.bus_get(Gio.BusType.SESSION, None, connected)

    def name_acquired(self, connection, _name):
        if not self.enabled or self.watch:
            return
        self.watch = Gio.bus_watch_name_on_connection(connection, WATCHER, Gio.BusNameWatcherFlags.NONE,
                                                     self.host_appeared, self.host_lost)
        self.timer = GLib.timeout_add_seconds(2, self.probe)

    def name_lost(self, *_):
        self.set_ready(False)

    def host_appeared(self, _connection, _name, owner):
        self.generation += 1
        self.host = owner
        self.registered = False
        self.pending = False
        self.probe()

    def host_lost(self, *_):
        self.generation += 1
        self.host = None
        self.registered = False
        self.pending = False
        self.set_ready(False)

    def probe(self):
        if not self.enabled:
            return False
        if not self.host or self.pending:
            return True
        generation = self.generation
        self.pending = True
        def checked(connection, result):
            try:
                available = connection.call_finish(result).unpack()[0]
            except GLib.Error:
                available = False
            if generation != self.generation or not self.enabled:
                return
            self.pending = False
            if not available:
                self.registered = False
                self.set_ready(False)
            elif self.registered:
                self.set_ready(True)
            else:
                self.register(generation)
        self.connection.call(self.host, "/StatusNotifierWatcher", "org.freedesktop.DBus.Properties", "Get",
                             GLib.Variant("(ss)", (WATCHER, "IsStatusNotifierHostRegistered")),
                             GLib.VariantType.new("(v)"), Gio.DBusCallFlags.NO_AUTO_START, 1500, None, checked)
        return True

    def register(self, generation):
        self.pending = True
        def registered(connection, result):
            try:
                connection.call_finish(result)
                success = True
            except GLib.Error:
                success = False
            if generation != self.generation or not self.enabled:
                return
            self.pending = False
            self.registered = success
            self.set_ready(success)
        self.connection.call(self.host, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                             GLib.Variant("(s)", (self.name,)), None, Gio.DBusCallFlags.NO_AUTO_START,
                             1500, None, registered)

    def close(self):
        self.enabled = False
        self.generation += 1
        if self.timer:
            GLib.source_remove(self.timer)
        if self.watch:
            Gio.bus_unwatch_name(self.watch)
        if self.owner:
            Gio.bus_unown_name(self.owner)
        for identifier in self.objects:
            self.connection.unregister_object(identifier)
        self.objects = []
        self.timer = self.watch = self.owner = 0
        self.host = None
        self.pending = self.registered = False
        self.set_ready(False)

    def item_property(self, _connection, _sender, _path, _interface, name):
        icon = "io.github.woodpile72.Pyrobolic-tray-" + self.appearance
        strings = {"Category": "ApplicationStatus", "Id": "pyrobolic", "Title": "Pyrobolic",
                   "Status": "Active", "IconName": icon,
                   "IconThemePath": "" if self.appearance == "symbolic" else
                   str(Path(__file__).resolve().parent / "icons/hicolor/scalable/apps")}
        if name in strings:
            return GLib.Variant("s", strings[name])
        if name.endswith("Pixmap"):
            return GLib.Variant("a(iiay)", self.pixmaps.get(self.appearance, []) if name == "IconPixmap" else [])
        if name == "Menu":
            return GLib.Variant("o", MENU_PATH)
        if name == "ItemIsMenu":
            return GLib.Variant("b", False)
        if name == "WindowId":
            return GLib.Variant("u", 0)
        if name == "ToolTip":
            return GLib.Variant("(sa(iiay)ss)", (icon, [], "Pyrobolic", ""))
        return GLib.Variant("s", "")

    def invoke(self, identifier):
        action = self.actions.get(identifier)
        if action:
            def run():
                action()
                return False
            GLib.idle_add(run)

    def item_call(self, _connection, _sender, _path, _interface, method, parameters, invocation):
        invocation.return_value(None)
        if method in {"Activate", "SecondaryActivate"}:
            self.invoke(1)

    @staticmethod
    def menu_properties(identifier, requested=()):
        labels = {1: "Show Pyrobolic", 2: "Add Download", 4: "Quit"}
        if identifier == 0:
            values = {"children-display": GLib.Variant("s", "submenu")}
        elif identifier == 3:
            values = {"type": GLib.Variant("s", "separator")}
        elif identifier in labels:
            values = {"label": GLib.Variant("s", labels[identifier]), "enabled": GLib.Variant("b", True),
                      "visible": GLib.Variant("b", True)}
        else:
            raise ValueError("Unknown menu item")
        return {key: value for key, value in values.items() if not requested or key in requested}

    def menu_property(self, _connection, _sender, _path, _interface, name):
        return {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                "Status": GLib.Variant("s", "normal")}[name]

    def menu_call(self, _connection, _sender, _path, _interface, method, parameters, invocation):
        args = parameters.unpack()
        try:
            result = None
            if method == "GetLayout":
                parent, depth, requested = args
                children = [GLib.Variant("(ia{sv}av)", (i, self.menu_properties(i, requested), []))
                            for i in range(1, 5)] if parent == 0 and depth != 0 else []
                result = GLib.Variant("(u(ia{sv}av))", (1, (parent, self.menu_properties(parent, requested), children)))
            elif method == "GetGroupProperties":
                result = GLib.Variant("(a(ia{sv}))", ([(i, self.menu_properties(i, args[1])) for i in args[0]],))
            elif method == "GetProperty":
                result = GLib.Variant("(v)", (self.menu_properties(args[0])[args[1]],))
            elif method in {"Event", "EventGroup"}:
                events = [args] if method == "Event" else args[0]
                errors = []
                for identifier, event, _data, _timestamp in events:
                    if identifier not in range(5):
                        errors.append(identifier)
                    elif event == "clicked":
                        self.invoke(identifier)
                if method == "EventGroup":
                    result = GLib.Variant("(ai)", (errors,))
            elif method == "AboutToShow":
                result = GLib.Variant("(b)", (False,))
            elif method == "AboutToShowGroup":
                result = GLib.Variant("(aiai)", ([], [i for i in args[0] if i not in range(5)]))
            invocation.return_value(result)
        except (KeyError, ValueError):
            invocation.return_dbus_error("com.canonical.dbusmenu.Error", "Unknown menu item or property")
