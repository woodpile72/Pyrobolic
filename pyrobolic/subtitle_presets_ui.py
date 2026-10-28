# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
from gi.repository import Adw, Gtk

from .subtitle_presets import matching_name, preset_name, save_preset, validate_presets


class SubtitlePresetControls(Adw.PreferencesGroup):
    def __init__(self, editor):
        from .app import button, combo, label
        super().__init__(title="Saved Presets")
        self.editor = editor
        self.library = {name: dict(values) for name, values in editor.draft.subtitle_presets.items()}
        validate_presets(self.library)
        self.loading = False
        self.load = combo("Load Preset", ["Choose a Preset"])
        self.load.set_use_subtitle(True)
        self.load.set_subtitle_lines(2)
        self.load.update_property([Gtk.AccessibleProperty.LABEL], ["Load Subtitle Preset"])
        self.load.connect("notify::selected", self.selected)
        self.add(self.load)
        self.name = Adw.EntryRow(title="Preset Name")
        self.name.update_property([Gtk.AccessibleProperty.LABEL], ["Subtitle Preset Name"])
        self.name.connect("changed", lambda *_: self.refresh())
        self.save = button("document-save-symbolic", "Save Subtitle Preset", self.save_clicked)
        self.remove = button("edit-delete-symbolic", "Delete Subtitle Preset", self.remove_clicked)
        self.name.add_suffix(self.save)
        self.name.add_suffix(self.remove)
        self.add(self.name)
        self.error = label(wrap=True, css="error", max_width_chars=35)
        self.add(self.error)
        self.rebuild()

    def rebuild(self):
        self.loading = True
        self.names = sorted(self.library, key=str.casefold)
        self.load.set_model(Gtk.StringList.new(["Choose a Preset"] + self.names))
        self.load.set_selected(0)
        self.load.set_sensitive(bool(self.names))
        self.loading = False
        self.editor.draft.subtitle_presets = {key: dict(value) for key, value in self.library.items()}
        self.refresh()

    def selected(self, *_):
        index = self.load.get_selected()
        if self.loading or not 0 < index <= len(self.names):
            return
        name = self.names[index - 1]
        self.editor.load_subtitle_preset(self.library[name])
        self.name.set_text(name)
        self.load.set_selected(0)

    def refresh(self):
        if not hasattr(self, "error"):
            return
        name = self.name.get_text()
        try:
            save_preset(self.library, name, self.editor.draft, overwrite=True)
            error = ""
        except ValueError as problem:
            error = str(problem)
        self.error.set_text(error if name else "")
        self.save.set_sensitive(not error)
        self.remove.set_sensitive(matching_name(self.library, name.strip()) is not None)

    def confirm(self, heading, body, action, callback):
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("confirm", action)
        dialog.set_response_appearance("confirm", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        def responded(_dialog, response):
            if response == "confirm" and not self.editor.closed:
                callback()
        dialog.connect("response", responded)
        dialog.present(self.editor)

    def save_clicked(self):
        if not self.save.get_sensitive():
            return
        name = preset_name(self.name.get_text())
        def save():
            self.library = save_preset(self.library, name, self.editor.draft, overwrite=True)
            self.rebuild()
        if matching_name(self.library, name) is not None:
            self.confirm("Replace Subtitle Preset?", name, "Replace", save)
        else:
            save()

    def remove_clicked(self):
        name = matching_name(self.library, self.name.get_text().strip())
        if name is not None:
            def remove():
                self.library = {key: value for key, value in self.library.items() if key != name}
                self.rebuild()
            self.confirm("Delete Subtitle Preset?", name, "Delete", remove)
