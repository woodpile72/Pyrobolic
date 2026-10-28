# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Explicit bulk import, followed by the ordinary download review."""

import threading
from dataclasses import replace

from .app import Adw, Gio, GLib, Gtk, Pango, action_row, button, label, margins, scrolled
from .batch import BatchInspection, edited_entry, parse_batch, read_batch, replace_import_line
from .transfer import has_inspection_options, transfer_summary, transfer_values


class ImportEntryDialog(Adw.Dialog):
    def __init__(self, parent, entry):
        super().__init__(title="Edit Import Entry", content_width=520, content_height=360)
        self.parent, self.entry, self.closed = parent, entry, False
        self.connect("closed", self.on_closed)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        self.cancel = Gtk.Button(label="Cancel")
        self.cancel.connect("clicked", lambda *_: self.close())
        self.apply = Gtk.Button(label="Apply", sensitive=False)
        self.apply.add_css_class("suggested-action")
        self.apply.connect("clicked", self.save)
        header.pack_start(self.cancel)
        header.pack_end(self.apply)
        toolbar.add_top_bar(header)
        content = margins(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12))
        group = Adw.PreferencesGroup()
        self.url = Adw.EntryRow(title="URL", text=entry.url)
        self.name = Adw.EntryRow(title="Name (Optional)", text=entry.name)
        self.destination = Adw.EntryRow(title="Folder (Optional)", text=entry.destination)
        self.browse = button("folder-open-symbolic", "Choose Entry Folder", self.choose_folder)
        self.destination.add_suffix(self.browse)
        self.reset = button("edit-undo-symbolic", "Use Default Download Folder", lambda: self.destination.set_text(""))
        self.destination.add_suffix(self.reset)
        for row in (self.url, self.name, self.destination):
            row.update_property([Gtk.AccessibleProperty.LABEL], [row.get_title()])
            row.connect("changed", self.changed)
            row.connect("entry-activated", self.save)
            group.add(row)
        content.append(group)
        self.error = label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, css="error")
        content.append(self.error)
        toolbar.set_content(scrolled(content))
        self.set_child(toolbar)
        self.set_default_widget(self.apply)
        self.set_focus(self.url)
        self.changed()

    def draft(self):
        if self.parent.closed or self.parent.worker:
            raise ValueError("The import is closed or inspection is running.")
        entry = edited_entry(self.parent.entries, self.entry, self.url.get_text(),
                             self.name.get_text(), self.destination.get_text())
        buffer = self.parent.text.get_buffer()
        text = replace_import_line(buffer.get_text(*buffer.get_bounds(), True), entry, self.parent.syntax)
        return entry, text

    def changed(self, *_):
        try:
            self.draft()
            self.error.set_text("")
            self.apply.set_sensitive(True)
        except (ValueError, RuntimeError) as error:
            self.error.set_text(str(error))
            self.apply.set_sensitive(False)

    def save(self, *_):
        if self.closed:
            return
        try:
            entry, text = self.draft()
            self.parent.apply_entry(self.entry, entry, text)
        except (ValueError, RuntimeError) as error:
            self.error.set_text(str(error))
            self.apply.set_sensitive(False)
            return
        self.close()

    def choose_folder(self):
        chooser = Gtk.FileDialog(title="Entry Folder")
        def ready(dialog, result):
            try:
                path = dialog.select_folder_finish(result).get_path()
                if path and not self.closed:
                    self.destination.set_text(path)
            except GLib.Error:
                pass
        chooser.select_folder(self.parent.window, None, ready)

    def on_closed(self, *_):
        self.closed = True


class BatchDialog(Adw.Dialog):
    def __init__(self, window, text=""):
        super().__init__(title="Import Downloads", content_width=600, content_height=520)
        self.window, self.closed, self.worker = window, False, None
        self.media_preferences = replace(window.store.settings)
        self.entries, self.page = [], 0
        self.rows = {}
        self.connect("closed", self.on_closed)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.primary = Gtk.Button(label="Inspect", sensitive=False)
        self.primary.add_css_class("suggested-action")
        self.primary.connect("clicked", lambda *_: self.review() if self.entries else self.inspect())
        header.pack_end(self.primary)
        self.transfer_button = button("network-workgroup-symbolic", "Transfer Options", self.edit_transfer)
        header.pack_end(self.transfer_button)
        toolbar.add_top_bar(header)
        content = margins(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12))
        self.input_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        line = Gtk.Box(spacing=6)
        line.append(label("URLs", css="heading", hexpand=True))
        self.open_button = button("document-open-symbolic", "Import Text File", self.choose_file)
        line.append(self.open_button)
        line.append(button("edit-paste-symbolic", "Paste URLs", self.paste))
        self.input_box.append(line)
        self.format = Adw.ComboRow(title="Import Format", use_subtitle=True,
                                  model=Gtk.StringList.new(["URLs or TSV (URL, name, folder)", "Parabolic TXT (URL | folder | name)"]))
        self.format.update_property([Gtk.AccessibleProperty.LABEL], ["Import Format"])
        self.input_box.append(self.format)
        self.text = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=12, bottom_margin=12,
                                 left_margin=12, right_margin=12, height_request=160, vexpand=True)
        self.text.update_property([Gtk.AccessibleProperty.LABEL], ["URLs and Optional Tab-Separated Names and Folders"])
        self.text.get_buffer().connect("changed", self.input_changed)
        self.input_box.append(scrolled(self.text))
        content.append(self.input_box)
        self.status = label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True)
        content.append(self.status)
        self.busy = Gtk.Box(spacing=8, visible=False)
        self.spinner = Gtk.Spinner(spinning=True)
        self.busy.append(self.spinner)
        self.busy.append(button("process-stop-symbolic", "Stop Inspection", self.stop))
        content.append(self.busy)
        self.results_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, visible=False)
        self.results = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, valign=Gtk.Align.START)
        self.results.add_css_class("boxed-list")
        self.results_scroll = scrolled(self.results)
        self.results_box.append(self.results_scroll)
        actions = Gtk.Box(spacing=6)
        actions.append(button("document-edit-symbolic", "Edit Import List", self.edit))
        self.retry = button("view-refresh-symbolic", "Retry Failed or Uninspected URLs", self.retry_failed)
        actions.append(self.retry)
        self.page_label = label(hexpand=True)
        self.page_label.set_xalign(0.5)
        actions.append(self.page_label)
        self.previous = button("go-previous-symbolic", "Previous Import Entries", lambda: self.change_page(-1))
        self.next = button("go-next-symbolic", "Next Import Entries", lambda: self.change_page(1))
        actions.append(self.previous)
        actions.append(self.next)
        self.results_box.append(actions)
        content.append(self.results_box)
        toolbar.set_content(content)
        self.set_child(toolbar)
        self.set_focus(self.text)
        self.text.get_buffer().set_text(text)

    def input_changed(self, *_):
        if not self.entries and not self.worker:
            self.primary.set_sensitive(bool(self.text.get_buffer().get_char_count()))

    def edit_transfer(self):
        from .transfer_ui import TransferOptionsDialog
        dialog = TransferOptionsDialog(self)
        dialog.present(self.window)
        return dialog

    def refresh_names(self):
        self.transfer_button.set_tooltip_text("Transfer Options: " + transfer_summary(self.media_preferences))

    def paste(self):
        def ready(clipboard, result):
            try:
                value = clipboard.read_text_finish(result)
                if value and not self.closed:
                    self.text.get_buffer().insert_at_cursor(value)
            except GLib.Error as error:
                self.status.set_text(str(error))
        self.text.get_clipboard().read_text_async(None, ready)

    def choose_file(self):
        dialog = Gtk.FileDialog(title="Import Downloads")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        text_filter = Gtk.FileFilter(name="Text Files")
        text_filter.add_mime_type("text/plain")
        text_filter.add_pattern("*.txt")
        text_filter.add_pattern("*.tsv")
        filters.append(text_filter)
        dialog.set_filters(filters)
        def ready(dialog, result):
            try:
                path = dialog.open_finish(result).get_path()
                if path and not self.closed:
                    self.load_file(path)
            except GLib.Error:
                pass
        dialog.open(self.window, None, ready)

    def load_file(self, path):
        self.primary.set_sensitive(False)
        self.input_box.set_sensitive(False)
        self.status.set_text("Reading text file")
        def work():
            try:
                value, error = read_batch(path), None
            except Exception as problem:
                value, error = None, str(problem)
            GLib.idle_add(self.file_loaded, value, error)
        threading.Thread(target=work, daemon=True).start()

    def file_loaded(self, value, error):
        if self.closed:
            return False
        self.input_box.set_sensitive(True)
        self.status.set_text(error or "")
        if value is not None:
            self.text.get_buffer().set_text(value)
        self.input_changed()
        return False

    def inspect(self):
        buffer = self.text.get_buffer()
        try:
            self.entries = parse_batch(buffer.get_text(*buffer.get_bounds(), True), self.syntax)
        except ValueError as error:
            self.status.set_text(str(error))
            return
        self.run_inspection()

    @property
    def syntax(self):
        return "parabolic" if self.format.get_selected() else "tsv"

    def run_inspection(self):
        if self.worker or self.closed:
            return
        self.input_box.set_visible(False)
        self.results_box.set_visible(False)
        self.busy.set_visible(True)
        self.primary.set_sensitive(False)
        self.primary.set_label("Inspecting")
        self.transfer_button.set_sensitive(False)
        self.status.set_text("Inspecting URLs")
        worker = self.worker = BatchInspection()
        transfer = replace(self.media_preferences)
        self.window.processes.add(worker)
        def progress(done, total):
            GLib.idle_add(self.progress, worker, done, total)
        def work():
            try:
                worker.run(self.entries, self.window.store.directory, progress,
                           **({"transfer": transfer} if has_inspection_options(transfer) else {}))
            finally:
                GLib.idle_add(self.inspected, worker)
        threading.Thread(target=work, daemon=True).start()

    def progress(self, worker, done, total):
        if not self.closed and self.worker is worker:
            self.status.set_text(f"Inspected {done} of {total} entries")
        return False

    def stop(self):
        if self.worker:
            self.worker.cancel()
            self.status.set_text("Stopping inspection")

    def inspected(self, worker):
        self.window.processes.discard(worker)
        self.worker = None
        if self.closed:
            return False
        self.busy.set_visible(False)
        self.transfer_button.set_sensitive(True)
        self.results_box.set_visible(True)
        self.refresh_summary()
        self.render()
        self.set_focus(self.primary if self.primary.get_sensitive() else self.retry)
        return False

    def refresh_summary(self):
        ready = sum(len(entry.items) for entry in self.entries)
        errors = sum(bool(entry.error) for entry in self.entries)
        waiting = sum(not entry.inspected and not entry.error for entry in self.entries)
        self.status.set_text(f"{ready} {'item' if ready == 1 else 'items'} ready \u00b7 "
                             f"{errors} {'entry' if errors == 1 else 'entries'} with errors \u00b7 {waiting} not inspected")
        self.primary.set_label("Review")
        self.primary.set_sensitive(bool(ready))
        self.retry.set_sensitive(any((e.inspected and e.error) or (not e.inspected and not e.error) for e in self.entries))

    def edit_entry(self, entry):
        if self.closed or self.worker:
            return None
        dialog = ImportEntryDialog(self, entry)
        dialog.present(self.window)
        return dialog

    def apply_entry(self, original, entry, text):
        index = next(i for i, value in enumerate(self.entries) if value is original)
        self.entries[index] = entry
        self.text.get_buffer().set_text(text)
        if row := self.rows.get(entry.line):
            self.update_row(row, entry)
        self.refresh_summary()

    def retry_failed(self):
        for entry in self.entries:
            if entry.inspected and entry.error:
                entry.error, entry.inspected = "", False
        self.run_inspection()

    def edit(self):
        self.entries = []
        self.page = 0
        self.results_box.set_visible(False)
        self.input_box.set_visible(True)
        self.primary.set_label("Inspect")
        self.input_changed()
        self.set_focus(self.text)

    def render(self):
        while child := self.results.get_first_child():
            self.results.remove(child)
        self.rows.clear()
        start = self.page * 100
        for entry in self.entries[start:start + 100]:
            row = action_row("")
            self.update_row(row, entry)
            row.set_subtitle_lines(0)
            row.edit_button = button("document-edit-symbolic", "Edit Import Entry",
                                     lambda row=row: self.edit_entry(row.entry))
            row.add_suffix(row.edit_button)
            row.add_suffix(button("document-properties-symbolic", "Import Entry Details",
                                   lambda row=row: self.details(row.entry)))
            self.rows[entry.line] = row
            self.results.append(row)
        self.page_label.set_text(f"{start + 1}-{min(start + 100, len(self.entries))} of {len(self.entries)}")
        self.previous.set_sensitive(self.page > 0)
        self.next.set_sensitive(start + 100 < len(self.entries))

    @staticmethod
    def update_row(row, entry):
        row.entry = entry
        row.set_title(f"Line {entry.line}: {entry.name or entry.url}")
        count = len(entry.items)
        row.set_subtitle(entry.error or (f"{count} {'item' if count == 1 else 'items'} ready"
                                        if entry.inspected else "Not inspected"))

    def details(self, entry):
        dialog = Adw.Dialog(title="Import Entry", content_width=500, content_height=320)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        text = "\n\n".join(filter(None, (entry.url, entry.name, entry.destination, entry.error)))
        toolbar.set_content(scrolled(margins(label(text, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True))))
        dialog.set_child(toolbar)
        dialog.present(self.window)

    def change_page(self, delta):
        self.page += delta
        self.render()

    def review(self):
        items = [item for entry in self.entries for item in entry.items]
        if self.worker or not items:
            return
        self.close()
        dialog = self.window.new_download(items[0]["url"])
        dialog.media_preferences = replace(dialog.media_preferences, **transfer_values(self.media_preferences))
        dialog.is_import = True
        dialog.set_title("Review Downloads")
        dialog.inspected(items, None)

    def on_closed(self, *_):
        self.closed = True
        self.stop()
