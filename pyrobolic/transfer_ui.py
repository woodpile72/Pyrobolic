# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
from dataclasses import replace

from gi.repository import Adw, Gtk

from .transfer import BROWSER_IDENTITIES, BROWSER_LABELS, validate_transfer


class TransferOptionsDialog(Adw.Dialog):
    def __init__(self, parent):
        from .app import combo, label, margins, scrolled
        super().__init__(title="Transfer Options", content_width=500, content_height=440)
        self.parent = parent
        self.draft = replace(parent.media_preferences)
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
        self.rate = Adw.SpinRow.new_with_range(0, 1048576, 1)
        self.rate.set_title("Rate Limit (KiB/s)")
        self.rate.set_subtitle("Per download; 0 means unlimited")
        self.rate.set_value(self.draft.rate_limit_kib)
        self.fragments = Adw.SpinRow.new_with_range(1, 16, 1)
        self.fragments.set_title("Concurrent Fragments")
        self.fragments.set_subtitle("Per download; applies to supported fragmented streams")
        self.fragments.set_value(self.draft.fragment_concurrency)
        for row in (self.rate, self.fragments):
            row.set_subtitle_lines(0)
            row.update_property([Gtk.AccessibleProperty.LABEL], [row.get_title()])
            row.connect("notify::value", self.changed)
            group.add(row)
        self.direct = Adw.SwitchRow(title="Direct Connection", subtitle="Bypass proxies for inspection and downloads; may expose your IP address",
                                    active=self.draft.direct_connection)
        self.direct.set_subtitle_lines(0)
        self.direct.update_property([Gtk.AccessibleProperty.LABEL], ["Direct Connection"])
        self.direct.connect("notify::active", self.changed)
        group.add(self.direct)
        self.proxy = Adw.EntryRow(title="Proxy URL", text=self.draft.proxy)
        self.proxy.update_property([Gtk.AccessibleProperty.LABEL], ["Proxy URL"])
        self.proxy.connect("changed", self.changed)
        group.add(self.proxy)
        self.identity = combo("Browser Identity", BROWSER_LABELS)
        self.identity.set_use_subtitle(True)
        self.identity.set_selected(BROWSER_IDENTITIES.index(self.draft.browser_identity))
        self.identity.update_property([Gtk.AccessibleProperty.LABEL], ["Browser Identity"])
        self.identity.connect("notify::selected", self.changed)
        group.add(self.identity)
        self.part_files = Adw.SwitchRow(title="Use .part Files", active=self.draft.use_part_files,
                                        subtitle="Resume incomplete downloads; otherwise retries restart them")
        self.part_files.set_subtitle_lines(0)
        self.part_files.update_property([Gtk.AccessibleProperty.LABEL], ["Use .part Files"])
        self.part_files.connect("notify::active", self.changed)
        group.add(self.part_files)
        page.append(label("With Direct Connection off, an empty proxy uses the system environment. A direct connection may expose your IP address to the source site. Authenticated proxies are not yet supported.",
                          wrap=True, max_width_chars=40))
        page.append(label("Browser identity can help with sites that block automated requests. It uses the engine's impersonation support, without opening a browser or reading its cookies. Unsupported identities fail explicitly; try updating the download engine.",
                          wrap=True, max_width_chars=40))
        self.error = label(wrap=True, css="error", max_width_chars=40)
        page.append(self.error)
        toolbar.set_content(scrolled(page))
        self.set_child(toolbar)
        self.set_focus(self.rate)
        self.changed()

    def changed(self, *_):
        if not hasattr(self, "error"):
            return
        self.draft.rate_limit_kib = int(self.rate.get_value())
        self.draft.fragment_concurrency = int(self.fragments.get_value())
        self.draft.direct_connection = self.direct.get_active()
        self.proxy.set_sensitive(not self.draft.direct_connection)
        self.draft.proxy = "" if self.draft.direct_connection else self.proxy.get_text().strip()
        self.draft.use_part_files = self.part_files.get_active()
        self.draft.browser_identity = BROWSER_IDENTITIES[self.identity.get_selected()]
        try:
            validate_transfer(self.draft)
            error = ""
        except ValueError as problem:
            error = str(problem)
        self.error.set_text(error)
        self.apply.set_sensitive(not error)

    def save(self, *_):
        if self.apply.get_sensitive() and not self.parent.closed:
            self.parent.media_preferences = replace(self.draft)
            self.parent.refresh_names()
            self.close()
