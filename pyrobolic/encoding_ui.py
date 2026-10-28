# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
from dataclasses import replace
from gi.repository import Adw, Gtk
from .encoding import (ENCODERS, ENCODER_LABELS, QUALITIES, QUALITY_LABELS,
                       validate_encoding, encoding_detail)


class EncodingDialog(Adw.Dialog):
    def __init__(self, parent):
        from .app import combo, label, margins, scrolled
        super().__init__(title="Video Encoding", content_width=520, content_height=420)
        self.parent = parent
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
        self.always = Adw.SwitchRow(title="Always Re-encode Video",
                                    subtitle="Encode matching containers too; may reduce quality")
        self.always.set_subtitle_lines(0)
        self.always.set_active(parent.media_preferences.reencode_video)
        self.always.update_property([Gtk.AccessibleProperty.LABEL], ["Always Re-encode Video"])
        self.always.connect("notify::active", self.changed)
        group.add(self.always)
        self.encoder = combo("Output Video Encoder", ENCODER_LABELS)
        self.encoder.set_selected(ENCODERS.index(parent.media_preferences.video_encoder))
        self.quality = combo("Encoding Quality", QUALITY_LABELS)
        self.quality.set_selected(QUALITIES.index(parent.media_preferences.video_encoding_quality))
        for row in (self.encoder, self.quality):
            row.set_use_subtitle(True)
            row.set_subtitle_lines(2)
            row.update_property([Gtk.AccessibleProperty.LABEL], [row.get_title()])
            row.connect("notify::selected", self.changed)
            group.add(row)
        self.threads = Adw.SpinRow.new_with_range(0, 64, 1)
        self.threads.set_title("Video Encoder Threads")
        self.threads.set_subtitle("0 uses the encoder default; not a total CPU limit")
        self.threads.set_subtitle_lines(0)
        self.threads.set_value(parent.media_preferences.video_encoding_threads)
        self.threads.update_property([Gtk.AccessibleProperty.LABEL], ["Video Encoder Threads"])
        self.threads.connect("notify::value", self.changed)
        group.add(self.threads)
        self.scope = label(wrap=True, max_width_chars=40)
        page.append(self.scope)
        page.append(label("Re-encoding can reduce quality and take longer. Smaller files and quality vary with the content. The selected encoder must be available in FFmpeg; it will not be silently replaced.", wrap=True, max_width_chars=40))
        self.details = label(selectable=True, wrap=True, max_width_chars=40)
        page.append(self.details)
        self.error = label(wrap=True, css="error", max_width_chars=40)
        page.append(self.error)
        toolbar.set_content(scrolled(page))
        self.set_child(toolbar)
        self.set_focus(self.encoder)
        self.changed()

    def draft(self):
        from .core import CONTAINERS
        return replace(self.parent.media_preferences, mode="video", video_processing="convert",
                       container=CONTAINERS[self.parent.container.get_selected()],
                       video_encoder=ENCODERS[self.encoder.get_selected()],
                       video_encoding_quality=QUALITIES[self.quality.get_selected()],
                       reencode_video=self.always.get_active(),
                       video_encoding_threads=int(self.threads.get_value()))

    def changed(self, *_):
        if not hasattr(self, "error"):
            return
        self.scope.set_text("Video will be re-encoded with these settings. Other streams are kept when the container matches."
                            if self.always.get_active() else
                            "Only used when converting to a different container. A file already in the target container is kept unchanged, even if its codec differs.")
        try:
            draft = self.draft()
            validate_encoding(draft)
            self.details.set_text(encoding_detail(draft))
            error = ""
        except ValueError as problem:
            error = str(problem)
            self.details.set_text("")
        self.error.set_text(error)
        self.apply.set_sensitive(not error)

    def save(self, *_):
        if not self.apply.get_sensitive() or self.parent.closed:
            return
        draft = self.draft()
        self.parent.media_preferences.video_encoder = draft.video_encoder
        self.parent.media_preferences.video_encoding_quality = draft.video_encoding_quality
        self.parent.media_preferences.video_encoding_threads = draft.video_encoding_threads
        self.parent.media_preferences.reencode_video = draft.reencode_video
        self.parent.refresh_names()
        self.close()
