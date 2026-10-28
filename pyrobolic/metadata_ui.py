# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
from gi.repository import Adw, Gtk
from .metadata import POLICIES, POLICY_LABELS
from .sponsorblock import MODES, LABELS, CATEGORIES, validate as validate_sponsorblock
from dataclasses import replace


class MetadataDialog(Adw.Dialog):
    def __init__(self, parent):
        from .app import combo, label, margins, scrolled
        super().__init__(title="Chapters and Source Data", content_width=520, content_height=620)
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
        self.description = Adw.SwitchRow(title="Description File", subtitle="Save the source description beside the media; may include links or personal information",
                                         active=parent.media_preferences.save_description)
        self.description.set_subtitle_lines(0)
        self.description.update_property([Gtk.AccessibleProperty.LABEL], ["Description File"])
        group.add(self.description)
        self.chapters = combo("Chapter Markers", POLICY_LABELS)
        self.source_info = combo("Matroska Source Data", POLICY_LABELS)
        for row, field in ((self.chapters, "chapter_policy"), (self.source_info, "source_info_policy")):
            row.set_selected(POLICIES.index(getattr(parent.media_preferences, field)))
            row.set_use_subtitle(True)
            row.set_subtitle_lines(0)
            row.update_property([Gtk.AccessibleProperty.LABEL], [row.get_title()])
            group.add(row)
        self.split = Adw.SwitchRow(title="Save Separate Chapter Files", subtitle="Keep the full media file and numbered chapter files",
                                  active=parent.media_preferences.split_chapters)
        self.split.set_subtitle_lines(0)
        self.titles = Adw.SwitchRow(title="Chapter Titles in Filenames",
                                   subtitle="Include shortened source titles after the chapter numbers",
                                   active=parent.media_preferences.chapter_titles)
        self.titles.set_subtitle_lines(0)
        self.precise = Adw.SwitchRow(title="Precise Video Cuts", subtitle="Re-encodes video; takes longer and may reduce quality",
                                    active=parent.media_preferences.precise_chapters)
        self.precise.set_subtitle_lines(0)
        for row in (self.split, self.titles, self.precise):
            row.update_property([Gtk.AccessibleProperty.LABEL], [row.get_title()])
            group.add(row)
        self.split.connect("notify::active", self.changed)
        sponsor = Adw.PreferencesGroup(title="SponsorBlock", description="Uses community-submitted timestamps and contacts the SponsorBlock service only when enabled. Remove Segments changes the saved media; video cuts may re-encode and reduce quality.")
        page.append(sponsor)
        self.sponsor_mode = combo("SponsorBlock", LABELS)
        self.sponsor_mode.set_use_subtitle(True)
        self.sponsor_mode.set_subtitle_lines(0)
        self.sponsor_mode.update_property([Gtk.AccessibleProperty.LABEL], ["SponsorBlock"])
        self.sponsor_mode.set_selected(MODES.index(parent.media_preferences.sponsorblock))
        sponsor.add(self.sponsor_mode)
        self.sponsor_categories = {}
        for key, title in CATEGORIES.items():
            row = Adw.SwitchRow(title=title, active=key in parent.media_preferences.sponsorblock_categories.split(","))
            row.set_title_lines(0)
            row.update_property([Gtk.AccessibleProperty.LABEL], [title])
            sponsor.add(row)
            row.connect("notify::active", self.changed)
            self.sponsor_categories[key] = row
        self.sponsor_mode.connect("notify::selected", self.changed)
        self.chapters.connect("notify::selected", self.changed)
        self.precise.set_visible(not parent.mode.get_selected())
        page.append(label("Without precise cuts, video chapter boundaries follow nearby keyframes. Splitting requires the entire media; unavailable chapters leave the full download with a warning.", wrap=True, max_width_chars=40))
        self.error = label(wrap=True, css="error", max_width_chars=40)
        page.append(self.error)
        page.append(label("Chapter markers require source chapters and a compatible output format. No chapters are invented. Do Not Add does not strip chapters already present in the source file.", wrap=True, max_width_chars=40))
        page.append(label("Source data attaches the site's metadata JSON to MKV or MKA files. It can include source addresses and personal information. It does not create a separate file. Follow Metadata Setting preserves the existing behavior.", wrap=True, max_width_chars=40))
        if parent.authentication.active:
            group.set_sensitive(False)
            page.append(label("Adding source chapters, descriptions and source data is disabled for authenticated downloads.", wrap=True, max_width_chars=40))
        toolbar.set_content(scrolled(page))
        self.set_child(toolbar)
        self.set_default_widget(self.apply)
        self.set_focus(self.chapters if group.get_sensitive() else self.cancel)
        self.changed()

    def changed(self, *_):
        self.precise.set_sensitive(self.split.get_active())
        self.titles.set_sensitive(self.split.get_active())
        error = ""
        if self.split.get_active() and not self.parent.authentication.active:
            if any(item.get("clip_start", 0) or item.get("clip_end") is not None for item, _ in self.parent.entries):
                error = "Clear the time range before splitting into chapter files."
        mode = MODES[self.sponsor_mode.get_selected()]
        for row in self.sponsor_categories.values():
            row.set_visible(mode != "off")
        try:
            draft = replace(self.parent.media_preferences, sponsorblock=mode,
                            sponsorblock_categories=self.selected_categories(),
                            chapter_policy=POLICIES[self.chapters.get_selected()], split_chapters=self.split.get_active())
            draft.auth_ref = "active" if self.parent.authentication.active else None
            draft.mode = "audio" if self.parent.mode.get_selected() else "video"
            validate_sponsorblock(draft)
            if mode != "off" and any(item.get("clip_start", 0) or item.get("clip_end") is not None for item, _ in self.parent.entries):
                error = "Clear the time range before enabling SponsorBlock."
        except ValueError as problem:
            error = str(problem)
        self.error.set_text(error)
        self.apply.set_sensitive(not error)

    def selected_categories(self):
        return ",".join(key for key, row in self.sponsor_categories.items() if row.get_active()) or (
            "sponsor" if self.sponsor_mode.get_selected() == 0 else "")

    def save(self, *_):
        if self.parent.closed or not self.apply.get_sensitive():
            return
        self.parent.media_preferences.sponsorblock = MODES[self.sponsor_mode.get_selected()]
        self.parent.media_preferences.sponsorblock_categories = self.selected_categories()
        if not self.parent.authentication.active:
            self.parent.media_preferences.save_description = self.description.get_active()
            self.parent.media_preferences.chapter_policy = POLICIES[self.chapters.get_selected()]
            self.parent.media_preferences.source_info_policy = POLICIES[self.source_info.get_selected()]
            self.parent.media_preferences.split_chapters = self.split.get_active()
            self.parent.media_preferences.chapter_titles = self.titles.get_active()
            self.parent.media_preferences.precise_chapters = self.precise.get_active()
        self.parent.refresh_names()
        self.close()
