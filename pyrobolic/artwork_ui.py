# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
from dataclasses import replace
from gi.repository import Adw, Gtk
from .artwork import MODES, LABELS, validate_artwork


class ArtworkDialog(Adw.Dialog):
    def __init__(self, parent):
        from .app import combo, label, margins, scrolled
        super().__init__(title="Artwork", content_width=520, content_height=380)
        self.parent = parent
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        self.cancel = Gtk.Button(label="Cancel")
        self.cancel.connect("clicked", lambda *_: self.close())
        self.apply = Gtk.Button(label="Apply")
        self.apply.add_css_class("suggested-action")
        self.apply.connect("clicked", self.save)
        header.pack_start(self.cancel)
        header.pack_end(self.apply)
        toolbar.add_top_bar(header)
        page = margins(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12))
        group = Adw.PreferencesGroup()
        self.mode = combo("Artwork", LABELS)
        self.mode.set_use_subtitle(True)
        self.mode.set_subtitle_lines(0)
        self.mode.set_selected(MODES.index(parent.media_preferences.artwork_mode))
        self.mode.update_property([Gtk.AccessibleProperty.LABEL], ["Artwork Output"])
        self.mode.connect("notify::selected", self.changed)
        group.add(self.mode)
        self.square = Adw.SwitchRow(title="Square Audio Artwork", active=parent.media_preferences.square_audio_artwork,
                                   subtitle="Crop the center and save as JPEG; applies to audio downloads")
        self.square.set_subtitle_lines(0)
        self.square.update_property([Gtk.AccessibleProperty.LABEL], ["Square Audio Artwork"])
        group.add(self.square)
        page.append(group)
        page.append(label("Save the source image beside the media file, embed it as cover art, or both. A source without artwork completes with a warning. Embedding requires a compatible output format.", wrap=True, max_width_chars=40))
        self.error = label(wrap=True, css="error", max_width_chars=40)
        page.append(self.error)
        if parent.authentication.active:
            self.mode.set_sensitive(False)
            self.square.set_sensitive(False)
            page.append(label("Artwork is disabled for authenticated downloads.", wrap=True, max_width_chars=40))
        toolbar.set_content(scrolled(page))
        self.set_child(toolbar)
        self.set_default_widget(self.apply)
        self.set_focus(self.mode if self.mode.get_sensitive() else self.cancel)
        self.changed()

    def changed(self, *_):
        from .core import AUDIO_FORMATS, CONTAINERS
        if not hasattr(self, "error"):
            return
        self.square.set_sensitive(self.mode.get_sensitive() and self.mode.get_selected() != 0)
        try:
            options = replace(self.parent.media_preferences,
                              artwork_mode=MODES[self.mode.get_selected()] if self.mode.get_sensitive() else "off",
                              mode="audio" if self.parent.mode.get_selected() else "video",
                              container=CONTAINERS[self.parent.container.get_selected()],
                              audio_format=AUDIO_FORMATS[self.parent.audio_format.get_selected()])
            validate_artwork(options)
            error = ""
        except ValueError as problem:
            error = str(problem)
        self.error.set_text(error)
        self.apply.set_sensitive(not error)

    def save(self, *_):
        if self.parent.closed or not self.apply.get_sensitive():
            return
        if not self.parent.authentication.active:
            self.parent.media_preferences.artwork_mode = MODES[self.mode.get_selected()]
            self.parent.media_preferences.square_audio_artwork = self.square.get_active()
            self.parent.refresh_names()
        self.close()
