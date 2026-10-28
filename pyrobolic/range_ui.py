# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
from gi.repository import Adw, Gtk
from .ranges import validate_range


class RangeDialog(Adw.Dialog):
    def __init__(self, parent, item):
        from .app import label, margins, scrolled
        super().__init__(title="Time Range", content_width=500, content_height=450)
        self.parent, self.item = parent, item
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        self.cancel = Gtk.Button(label="Cancel")
        self.cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(self.cancel)
        self.apply = Gtk.Button(label="Apply")
        self.apply.add_css_class("suggested-action")
        self.apply.connect("clicked", self.save)
        header.pack_end(self.apply)
        toolbar.add_top_bar(header)
        page = margins(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12))
        group = Adw.PreferencesGroup()
        page.append(group)
        self.start = Adw.SpinRow.new_with_range(0, 604800, 0.1)
        self.start.set_digits(1)
        self.start.set_title("Start (Seconds)")
        self.start.set_value(item.get("clip_start", 0))
        self.to_end = Adw.SwitchRow(title="Continue to End", active=item.get("clip_end") is None)
        self.end = Adw.SpinRow.new_with_range(0, 604800, 0.1)
        self.end.set_digits(1)
        self.end.set_title("End (Seconds)")
        self.end.set_value(item.get("clip_end") or item.get("duration") or 60)
        self.precise = Adw.SwitchRow(title="Precise Cuts", active=item.get("precise_cuts", False))
        for row in (self.start, self.to_end, self.end, self.precise):
            group.add(row)
            row.update_property([Gtk.AccessibleProperty.LABEL], [row.get_title()])
            row.connect("notify::active" if isinstance(row, Adw.SwitchRow) else "notify::value", self.changed)
        page.append(label("Keyframe cuts can include nearby frames. Precise cuts re-encode video, may reduce quality and take longer. Some sources do not support time ranges.", wrap=True, max_width_chars=40))
        self.error = label(wrap=True, css="error", max_width_chars=40)
        page.append(self.error)
        toolbar.set_content(scrolled(page))
        self.set_child(toolbar)
        self.set_focus(self.start)
        self.changed()

    def values(self):
        return self.start.get_value(), None if self.to_end.get_active() else self.end.get_value(), self.precise.get_active()

    def changed(self, *_):
        if not hasattr(self, "error"):
            return
        self.end.set_sensitive(not self.to_end.get_active())
        try:
            validate_range(*self.values())
            error = ""
        except ValueError as problem:
            error = str(problem)
        self.error.set_text(error)
        self.apply.set_sensitive(not error)

    def save(self, *_):
        if self.apply.get_sensitive() and not self.parent.closed:
            start, end, precise = self.values()
            self.item.update(clip_start=start, clip_end=end, precise_cuts=precise)
            self.parent.refresh_names()
            self.close()
