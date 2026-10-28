# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Native GTK 4 / libadwaita interface."""

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from . import __version__
from .core import (ACTIVE, AUDIO_FORMATS, CONTAINERS, QUALITIES, VIDEO_PROCESSING, TERMINAL, Job, Process,
                   Queue, Settings, Store, data_directory, engine_path, inspect_url, validate_url)
from .updater import UpdateControl
from .runtime import update_support, youtube_url, installed_directory
from .naming import output_stem, suggest_name, validate_name
from .formats import format_label, make_choice, source_choices, output_extension, remux_problem
from .preferences import (VIDEO_CODECS, AUDIO_CODECS, FRAME_RATES, AUDIO_RATES, AUDIO_ENCODING_QUALITIES, SUBTITLE_MODES,
                          SUBTITLE_SOURCES, SUBTITLE_FORMATS, SUBTITLE_LANGUAGE_SCOPES, preference_values, validate_preferences,
                          preference_summary, subtitle_summary, subtitle_mode, language_codes, subtitle_catalog)
from .core import validate_output_options
from .batch import export_m3u, numbered_name
from .transfer import has_inspection_options, transfer_summary
from .ranges import range_summary
from .encoding import encoding_summary
from .metadata import metadata_summary
from .artwork import artwork_summary
from .history import RETENTION_SECONDS, CLEAR_LABELS, retention_seconds


APP_ID = "io.github.woodpile72.Pyrobolic"

ICONS = {
    "list-add-symbolic": "pyrobolic-add-symbolic",
    "edit-paste-symbolic": "pyrobolic-paste-link-symbolic",
    "folder-open-symbolic": "pyrobolic-open-folder-symbolic",
    "folder-download-symbolic": "pyrobolic-download-symbolic",
    "document-open-recent-symbolic": "pyrobolic-history-symbolic",
    "process-stop-symbolic": "pyrobolic-cancel-symbolic",
    "view-refresh-symbolic": "pyrobolic-refresh-symbolic",
    "software-update-available-symbolic": "pyrobolic-refresh-symbolic",
    "audio-x-generic-symbolic": "pyrobolic-audio-symbolic",
    "video-x-generic-symbolic": "pyrobolic-videos-symbolic",
}


def button(icon, tooltip, callback):
    widget = Gtk.Button(icon_name=ICONS.get(icon, icon), tooltip_text=tooltip, valign=Gtk.Align.CENTER)
    widget.connect("clicked", lambda _button: callback())
    widget.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
    return widget


def label(text="", css=None, **kwargs):
    widget = Gtk.Label(label=text, xalign=0, **kwargs)
    if css:
        widget.add_css_class(css)
    return widget


def margins(widget, value=18):
    for edge in ("top", "bottom", "start", "end"):
        getattr(widget, f"set_margin_{edge}")(value)
    return widget


def scrolled(child):
    scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
    scroll.set_child(child)
    return scroll


def action_row(title, subtitle=""):
    return Adw.ActionRow(title=title, subtitle=subtitle, use_markup=False,
                         title_lines=2, subtitle_lines=2)


def combo(title, values):
    return Adw.ComboRow(title=title, model=Gtk.StringList.new(values))


def display_title(job):
    return job.filename or suggest_name(job.title, job.url)


def job_summary(job):
    if job.transfer_paused:
        return "Transfer Paused"
    if job.status == "Completed":
        parts = ["Completed"]
        if job.total_bytes is not None:
            parts.append(GLib.format_size(job.total_bytes))
        if job.completed_at is not None:
            date = GLib.DateTime.new_from_unix_local(int(job.completed_at))
            if date:
                parts.append(date.format("%x %X"))
        if job.warnings:
            parts.append("Review Details")
        return "  \u00b7  ".join(parts)
    return f"{job.status}  \u00b7  {job.detail}" if job.status.casefold() != job.detail.casefold() else job.status


class FilenameDialog(Adw.Dialog):
    def __init__(self, parent, item):
        super().__init__(title="Edit Filename", content_width=480, content_height=280)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        self.done = Gtk.Button(label="Done")
        self.done.add_css_class("suggested-action")
        header.pack_start(cancel)
        header.pack_end(self.done)
        toolbar.add_top_bar(header)
        page = margins(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12))
        group = Adw.PreferencesGroup()
        self.name = Adw.EntryRow(title="Name", text=item["filename"])
        self.name.add_suffix(button("edit-undo-symbolic", "Reset Filename",
                                    lambda: self.name.set_text(item["suggested_name"])))
        group.add(self.name)
        self.preview = action_row("Filename")
        group.add(self.preview)
        self.position = None
        if len(parent.entries) > 1:
            self.position = Adw.SpinRow.new_with_range(1, len(parent.entries), 1)
            self.position.set_title("Position in Download Order")
            self.position.update_property([Gtk.AccessibleProperty.LABEL], ["Position in Download Order"])
            self.position.set_value(next(i + 1 for i, (entry, _) in enumerate(parent.entries) if entry is item))
            group.add(self.position)
        self.destination = item.get("destination", "")
        if parent.is_import and len(parent.entries) > 1:
            self.folder = action_row("Save To", self.destination or parent.output)
            def choose():
                dialog = Gtk.FileDialog(title="Download Folder")
                dialog.set_initial_folder(Gio.File.new_for_path(self.destination or parent.output))
                def ready(dialog, result):
                    try:
                        path = dialog.select_folder_finish(result).get_path()
                        if path:
                            self.destination = path
                            self.folder.set_subtitle(path)
                    except GLib.Error:
                        pass
                dialog.select_folder(parent.window, None, ready)
            def reset_folder():
                self.destination = ""
                self.folder.set_subtitle(parent.output)
            self.folder.add_suffix(button("edit-undo-symbolic", "Use Default Folder", reset_folder))
            self.folder.add_suffix(button("folder-open-symbolic", "Choose Item Folder", choose))
            group.add(self.folder)
        page.append(group)
        self.error = label(wrap=True, css="error")
        page.append(self.error)
        toolbar.set_content(scrolled(page))
        self.set_child(toolbar)
        def changed(*_):
            try:
                validate_name(self.name.get_text())
                self.preview.set_subtitle(parent.filename_preview(dict(item, filename=self.name.get_text())))
                self.error.set_text("")
                self.done.set_sensitive(True)
            except ValueError as error:
                self.preview.set_subtitle("")
                self.error.set_text(str(error))
                self.done.set_sensitive(False)
        def save(*_):
            if self.done.get_sensitive():
                item["filename"] = self.name.get_text()
                if parent.is_import:
                    item["destination"] = self.destination
                if self.position is not None:
                    parent.move_item(item, int(self.position.get_value()) - 1)
                parent.refresh_names()
                self.close()
        self.name.connect("changed", changed)
        self.name.connect("entry-activated", save)
        self.done.connect("clicked", save)
        self.set_focus(self.name)
        changed()


class SourceFormatDialog(Adw.Dialog):
    def __init__(self, parent, item):
        super().__init__(title="Source Formats", content_width=560, content_height=420)
        self.parent, self.item = parent, item
        self.mode = "audio" if parent.mode.get_selected() else "video"
        self.process = None
        self.closed = False
        self.formats = []
        self.primary_formats = []
        self.audio_formats = []
        self.connect("closed", self.on_closed)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        self.cancel = Gtk.Button(label="Cancel")
        self.cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(self.cancel)
        self.apply = Gtk.Button(label="Apply", sensitive=False)
        self.apply.add_css_class("suggested-action")
        self.apply.connect("clicked", self.save)
        header.pack_end(self.apply)
        self.set_default_widget(self.apply)
        toolbar.add_top_bar(header)
        page = margins(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12))
        page.append(label(item["filename"], css="heading", wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR))
        self.spinner = Gtk.Spinner()
        page.append(self.spinner)
        self.group = Adw.PreferencesGroup()
        self.primary = Adw.ComboRow(title="Audio Source" if self.mode == "audio" else "Video Source",
                                    enable_search=True, use_subtitle=True)
        self.audio = Adw.ComboRow(title="Audio Source", enable_search=True, use_subtitle=True)
        for row in (self.primary, self.audio):
            row.update_property([Gtk.AccessibleProperty.LABEL], [row.get_title()])
            row.set_expression(Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
            factory = Gtk.SignalListItemFactory()
            factory.connect("setup", lambda _factory, entry: entry.set_child(
                label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, max_width_chars=42)))
            factory.connect("bind", lambda _factory, entry: entry.get_child().set_text(entry.get_item().get_string()))
            row.set_list_factory(factory)
            self.group.add(row)
        self.primary.connect("notify::selected", self.selected)
        self.audio.connect("notify::selected", self.selected)
        page.append(self.group)
        self.summary = label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True)
        page.append(self.summary)
        self.error = label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, css="error")
        page.append(self.error)
        self.retry = Gtk.Button(label="Retry", halign=Gtk.Align.CENTER, visible=False)
        self.retry.connect("clicked", lambda *_: self.load())
        page.append(self.retry)
        toolbar.set_content(scrolled(page))
        self.set_child(toolbar)
        self.set_focus(self.primary)
        if item.get("formats_loaded"):
            self.loaded(item.get("formats", []), None)
        else:
            self.load()

    def load(self):
        if self.process or self.closed:
            return
        self.group.set_visible(False)
        self.apply.set_sensitive(False)
        self.retry.set_visible(False)
        self.error.set_text("")
        self.spinner.set_visible(True)
        self.spinner.start()
        self.set_focus(self.cancel)
        process = self.process = Process()
        self.parent.window.processes.add(process)
        authentication = self.parent.authentication
        transfer = replace(self.parent.media_preferences)
        def work():
            try:
                if authentication.active:
                    from .auth import AuthenticationError, public_url
                    if public_url(self.item["url"]) != self.parent.auth_origin:
                        raise AuthenticationError("Inspect this other site separately with explicit authentication.")
                entries = inspect_url(self.item["url"], process, self.parent.window.store.directory, single=True,
                                      **({"transfer": transfer} if has_inspection_options(transfer) else {}),
                                      **({"authentication": authentication} if authentication.active else {}))
                if len(entries) != 1:
                    raise ValueError("This item did not resolve to a single video")
                result, error = entries[0]["formats"], None
            except Exception as failure:
                from .auth import safe_auth_error
                result, error = [], safe_auth_error(failure) if authentication.active else str(failure)
            GLib.idle_add(self.loaded, result, error, process)
        threading.Thread(target=work, daemon=True).start()

    def loaded(self, formats, error, process=None):
        if process:
            self.parent.window.processes.discard(process)
        self.process = None
        if self.closed:
            return False
        self.spinner.stop()
        self.spinner.set_visible(False)
        if error:
            self.error.set_text(error)
            self.retry.set_visible(True)
            self.retry.grab_focus()
            return False
        self.item.update(formats=formats, formats_loaded=True)
        self.formats = formats
        self.primary_formats = source_choices(formats, self.mode)
        self.audio_formats = [f for f in reversed(formats) if f["kind"] == "audio"]
        self.primary.set_model(Gtk.StringList.new(["Automatic"] + [format_label(f) for f in self.primary_formats]))
        self.audio.set_model(Gtk.StringList.new(["No Audio"] + [format_label(f) for f in self.audio_formats]))
        self.audio.set_selected(1 if self.audio_formats else 0)
        previous = self.parent.source_choice(self.item)
        if previous:
            self.primary.set_selected(next((i + 1 for i, f in enumerate(self.primary_formats)
                                            if f["id"] == previous["primary"]), 0))
            self.audio.set_selected(next((i + 1 for i, f in enumerate(self.audio_formats)
                                          if f["id"] == previous["audio"]), 0))
        self.group.set_visible(True)
        self.selected()
        self.primary.grab_focus()
        return False

    def choice(self):
        index = self.primary.get_selected()
        primary = self.primary_formats[index - 1] if 0 < index <= len(self.primary_formats) else None
        extra = self.audio.get_selected()
        audio = (self.audio_formats[extra - 1]["id"] if primary and primary["kind"] == "video" and
                 self.mode == "video" and 0 < extra <= len(self.audio_formats) else None)
        return make_choice(self.formats, self.mode, primary["id"] if primary else None, audio)

    def selected(self, *_):
        index = self.primary.get_selected()
        primary = self.primary_formats[index - 1] if 0 < index <= len(self.primary_formats) else None
        self.audio.set_visible(bool(primary and primary["kind"] == "video" and self.mode == "video"))
        choice = self.choice()
        self.summary.set_text(choice["summary"] if choice else "Automatic")
        if not self.primary_formats:
            self.summary.set_text("No detailed source formats reported")
        self.apply.set_sensitive(self.process is None)

    def save(self, *_):
        if not self.apply.get_sensitive() or self.parent.closed:
            return
        self.item.setdefault("source_choices", {})[self.mode] = self.choice()
        self.parent.refresh_names()
        self.close()

    def on_closed(self, *_):
        self.closed = True
        if self.process:
            self.process.cancel()


class MediaOptionsDialog(Adw.Dialog):
    def __init__(self, parent, subtitles=False):
        super().__init__(title="Subtitles" if subtitles else "Source Preferences", content_width=520, content_height=520)
        self.parent = parent
        self.subtitles = subtitles
        self.rows = {}
        self.choices = {}
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
        self.set_default_widget(self.apply)
        page = margins(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12))
        group = Adw.PreferencesGroup()
        page.append(group)
        self.draft = replace(parent.media_preferences)
        self.loading_preset = False
        self.closed = False
        self.worker = None
        self.connect("closed", self.on_closed)
        if subtitles:
            self.draft.subtitle_mode = subtitle_mode(self.draft)
            self.add_choice(group, "subtitle_mode", "Subtitle Output", SUBTITLE_MODES,
                            ["Off", "Sidecar Files", "Embedded Only", "Embedded and Sidecar Files"])
            self.add_choice(group, "subtitle_source", "Subtitle Sources", SUBTITLE_SOURCES,
                            ["Manual Only", "Manual or Auto-Generated", "Auto-Generated Only"])
            self.add_choice(group, "subtitle_language_scope", "Languages", SUBTITLE_LANGUAGE_SCOPES,
                            ["Selected Languages", "All Available"])
            self.catalog_items = [item for item, selected in parent.entries if selected.get_active()]
            self.available_source = None
            self.available_codes = []
            self.available = Adw.ComboRow(title="Reported Languages", enable_search=True, use_subtitle=True,
                                         subtitle_lines=2, model=Gtk.StringList.new([]), visible=False)
            self.available.update_property([Gtk.AccessibleProperty.LABEL], ["Reported Subtitle Languages"])
            self.available.set_expression(Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
            factory = Gtk.SignalListItemFactory()
            factory.connect("setup", lambda _factory, entry: entry.set_child(
                label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, max_width_chars=40)))
            factory.connect("bind", lambda _factory, entry: entry.get_child().set_text(entry.get_item().get_string()))
            self.available.set_list_factory(factory)
            self.add_language_button = button("list-add-symbolic", "Add Subtitle Language", self.add_language)
            self.available.add_suffix(self.add_language_button)
            group.add(self.available)
            self.languages = Adw.EntryRow(title="Language Codes (en, es, de)", text=self.draft.subtitle_languages)
            self.languages.update_property([Gtk.AccessibleProperty.LABEL], ["Subtitle Language Codes"])
            self.languages.connect("changed", self.changed)
            group.add(self.languages)
            self.add_choice(group, "subtitle_format", "Subtitle Format", SUBTITLE_FORMATS,
                            ["Original", "SRT", "ASS", "WebVTT", "LRC"])
            self.add_choice(group, "subtitle_missing", "Missing Languages", ["warn", "fail"],
                            ["Continue with a Notice", "Fail the Download"])
            self.availability = label(
                wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True, css="dim-label")
            page.append(self.availability)
            self.coverage = label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True)
            self.coverage.update_property([Gtk.AccessibleProperty.LABEL], ["Requested Subtitle Availability"])
            page.append(self.coverage)
            self.check_languages = Gtk.Button(label="Check Selected Items")
            self.check_languages.connect("clicked", lambda *_: self.inspect_languages())
            page.append(self.check_languages)
            self.inspection_status = label(wrap=True, selectable=True)
            page.append(self.inspection_status)
            from .subtitle_presets_ui import SubtitlePresetControls
            self.presets = SubtitlePresetControls(self)
            page.append(self.presets)
        else:
            self.add_choice(group, "source_quality", "Source Quality", ["best", "smallest"],
                            ["Best Available", "Lower Resolution and Bitrate"])
            self.add_choice(group, "preferred_video_codec", "Preferred Video Codec", VIDEO_CODECS,
                            ["No Preference", "H.264", "H.265 / HEVC", "VP9", "AV1"])
            self.add_choice(group, "preferred_audio_codec", "Preferred Audio Codec", AUDIO_CODECS,
                            ["No Preference", "AAC", "Opus", "MP3", "Vorbis"])
            self.add_choice(group, "preferred_fps", "Preferred Frame Rate", FRAME_RATES,
                            ["No Preference", "Up to 24 fps", "Up to 30 fps", "Up to 60 fps"])
            self.add_choice(group, "preferred_abr", "Preferred Audio Bitrate", AUDIO_RATES,
                            ["No Preference"] + [f"Up to {rate} kb/s" for rate in AUDIO_RATES[1:]])
            self.add_choice(group, "upscaled_sources", "AI-Upscaled Sources", ["allow", "exclude"],
                            ["Allow", "Exclude Reported AI Upscales"])
            for key in ("preferred_video_codec", "preferred_fps", "upscaled_sources"):
                self.rows[key].set_visible(self.draft.mode == "video")
            note = ("Source preferences allow fallback, not re-encoding. Audio codec takes priority over bitrate. "
                    "Exact stream choices override these preferences." if self.draft.mode == "audio" else
                    "Source preferences allow fallback, not re-encoding. Codecs take priority over frame rate and bitrate. "
                    "Exact stream choices override these preferences. Maximum resolution remains a strict limit for automatic selection.")
            page.append(label(note,
                              wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, css="dim-label"))
            page.append(label("AI-upscale exclusion is a strict filter on reported source labels, not image analysis. "
                              "Unlabelled sources remain eligible. Exact stream choices override this filter.",
                              wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, css="dim-label", visible=self.draft.mode == "video"))
        self.error = label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, css="error")
        page.append(self.error)
        toolbar.set_content(scrolled(page))
        self.set_child(toolbar)
        self.set_focus(next(iter(self.rows.values())))
        self.changed()

    def add_choice(self, group, key, title, values, labels):
        row = combo(title, labels)
        row.set_use_subtitle(True)
        row.set_subtitle_lines(2)
        row.update_property([Gtk.AccessibleProperty.LABEL], [title])
        row.set_selected(values.index(getattr(self.draft, key)))
        self.rows[key], self.choices[key] = row, values
        row.connect("notify::selected", self.changed)
        group.add(row)

    def changed(self, *_):
        if not hasattr(self, "error") or self.loading_preset:
            return
        for key, row in self.rows.items():
            setattr(self.draft, key, self.choices[key][row.get_selected()])
        if self.subtitles:
            self.draft.subtitle_languages = self.languages.get_text()
            enabled = self.draft.subtitle_mode != "off"
            self.languages.set_sensitive(enabled)
            self.available.set_sensitive(enabled)
            self.refresh_language_choices()
            selected = self.draft.subtitle_language_scope == "selected"
            self.languages.set_visible(selected)
            self.available.set_visible(selected and bool(self.available_codes))
            note = ("All available tracks may include many translated captions. Live chat is excluded." if not selected else
                    "Requested languages are checked during download. Regional variants are included.")
            if self.draft.subtitle_source == "manual-auto":
                note += " Manual tracks take precedence."
            elif self.draft.subtitle_source == "auto":
                note += " Manual tracks are not substituted for missing generated captions."
            _, known = subtitle_catalog(self.catalog_items, self.draft.subtitle_source)
            if len(self.catalog_items) > 1:
                note += f" Availability checked for {known} of {len(self.catalog_items)} selected items. Counts do not guarantee later availability."
            self.availability.set_text(note)
            from .preferences import subtitle_coverage
            coverage = subtitle_coverage(self.catalog_items, self.draft)
            self.coverage.set_text(coverage)
            self.coverage.set_visible(bool(coverage))
            self.check_languages.set_visible(known < len(self.catalog_items))
            for key, row in self.rows.items():
                if key != "subtitle_mode":
                    row.set_sensitive(enabled)
        try:
            validate_preferences(self.draft)
            error = ""
        except ValueError as problem:
            error = str(problem)
        self.error.set_text(error)
        self.apply.set_sensitive(not error and self.worker is None)
        if self.subtitles:
            self.presets.refresh()

    def load_subtitle_preset(self, values):
        self.loading_preset = True
        try:
            for key, row in self.rows.items():
                row.set_selected(self.choices[key].index(values[key]))
            self.languages.set_text(values["subtitle_languages"])
        finally:
            self.loading_preset = False
        self.changed()

    def refresh_language_choices(self):
        source = self.draft.subtitle_source
        if self.available_source == source:
            return
        index = self.available.get_selected()
        previous = self.available_codes[index] if index < len(self.available_codes) else None
        tracks, known = subtitle_catalog(self.catalog_items, source)
        ordered = sorted(tracks.items(), key=lambda entry: (entry[1]["name"].casefold(), entry[0]))
        self.available_codes = [code for code, _track in ordered]
        names = [f"{track['name']} · {code} · {'Manual' if track['sources'] == {'manual'} else 'Auto-Generated' if track['sources'] == {'generated'} else 'Manual / Generated'}"
                 + (f" · {track['count']}/{len(self.catalog_items)} items" if len(self.catalog_items) > 1 else "")
                 for code, track in ordered]
        self.available.set_model(Gtk.StringList.new(names))
        if previous in self.available_codes:
            self.available.set_selected(self.available_codes.index(previous))
        self.available_source = source

    def inspect_languages(self):
        from .batch import BatchInspection, ImportEntry
        if self.worker or self.closed:
            return
        pending = [item for item in self.catalog_items if not item.get("subtitles_loaded", item.get("formats_loaded", False))]
        entries = [ImportEntry(i + 1, item["url"]) for i, item in enumerate(pending)]
        worker = self.worker = BatchInspection()
        self.parent.window.processes.add(worker)
        self.check_languages.set_sensitive(False)
        self.apply.set_sensitive(False)
        self.inspection_status.set_text("Checking subtitle availability")
        authentication = self.parent.authentication
        transfer = replace(self.parent.media_preferences)
        def progress(done, total):
            def report():
                if not self.closed:
                    self.inspection_status.set_text(f"Checked {done} of {total} items")
            GLib.idle_add(report)
        def work():
            def inspect(url, process, directory):
                if authentication.active:
                    from .auth import AuthenticationError, public_url
                    if public_url(url) != self.parent.auth_origin:
                        raise AuthenticationError("Inspect this other site separately with explicit authentication.")
                return inspect_url(url, process, directory, single=True,
                                   **({"transfer": transfer} if has_inspection_options(transfer) else {}),
                                   **({"authentication": authentication} if authentication.active else {}))
            try:
                worker.run(entries, self.parent.window.store.directory, progress, inspect=inspect)
            finally:
                GLib.idle_add(finished)
        def finished():
            self.parent.window.processes.discard(worker)
            self.worker = None
            if self.closed or self.parent.closed:
                return
            for item, entry in zip(pending, entries):
                if entry.inspected and not entry.error and entry.items:
                    item["subtitle_tracks"] = entry.items[0].get("subtitle_tracks", [])
                    item["subtitles_loaded"] = True
            failures = sum(bool(entry.error) for entry in entries)
            self.inspection_status.set_text(f"{failures} item(s) could not be checked. Retry is available." if failures else "Availability updated")
            self.check_languages.set_sensitive(True)
            self.available_source = None
            self.changed()
        threading.Thread(target=work, daemon=True).start()

    def on_closed(self, *_):
        self.closed = True
        if self.worker:
            self.worker.cancel()

    def add_language(self):
        index = self.available.get_selected()
        if index >= len(self.available_codes):
            return
        try:
            current = language_codes(self.languages.get_text()) if self.languages.get_text().strip() else []
        except ValueError:
            self.languages.grab_focus()
            return
        code = self.available_codes[index]
        if code.casefold() not in {value.casefold() for value in current}:
            current.append(code)
        self.languages.set_text(",".join(current))
        self.languages.grab_focus()

    def save(self, *_):
        if not self.apply.get_sensitive() or self.parent.closed:
            return
        self.parent.media_preferences = replace(self.draft)
        self.parent.refresh_names()
        self.close()


class ItemSelection:
    """Keep selections off-widget so a large review only needs one page of rows."""
    def __init__(self, changed):
        self.active = True
        self.changed = changed
        self.widget = None

    def get_active(self):
        return self.active

    def set_active(self, value):
        if value == self.active:
            return
        self.active = value
        if self.widget:
            self.widget.set_active(value)
        self.changed()


class DownloadDialog(Adw.Dialog):
    def __init__(self, window, initial_url=None):
        super().__init__(title="New Download", content_width=560, content_height=160)
        self.window = window
        from .auth import Authentication
        self.authentication = Authentication()
        self.auth_origin = ""
        self.credential_task = None
        self.process = None
        self.closed = False
        self.saving = False
        self.quick_requested = False
        self.entries = []
        self.item_rows = []
        self.thumbnail_cancel = None
        self.item_page = 0
        self.is_import = False
        self.bulk_selecting = False
        self.media_preferences = replace(window.store.settings)
        self.output = window.store.settings.output
        self.connect("closed", self.on_closed)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        self.cancel = Gtk.Button(label="Cancel")
        self.cancel.connect("clicked", self.cancel_submission)
        header.pack_start(self.cancel)
        self.download = Gtk.Button(label="Continue", sensitive=False)
        self.download.add_css_class("suggested-action")
        self.download.connect("clicked", self.primary_action)
        self.quick_popover = Gtk.Popover(halign=Gtk.Align.END)
        quick_box = margins(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12), 12)
        self.quick_summary = label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, max_width_chars=26)
        summary_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                            propagate_natural_height=True, max_content_height=240)
        summary_scroll.set_child(self.quick_summary)
        quick_box.append(summary_scroll)
        self.quick_button = Gtk.Button(label="Quick Download")
        self.quick_button.set_tooltip_text("Download with saved settings; playlists open for review")
        self.quick_button.update_property([Gtk.AccessibleProperty.LABEL], ["Quick Download with Saved Settings"])
        self.quick_button.connect("clicked", lambda *_: self.quick_download())
        quick_box.append(self.quick_button)
        self.quick_transfer = Gtk.Button()
        transfer_label = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        transfer_label.append(Gtk.Image(icon_name="network-workgroup-symbolic"))
        transfer_label.append(label("Transfer Options"))
        self.quick_transfer.set_child(transfer_label)
        self.quick_transfer.update_property([Gtk.AccessibleProperty.LABEL], ["Transfer Options"])
        self.quick_transfer.connect("clicked", lambda *_: self.edit_transfer())
        quick_box.append(self.quick_transfer)
        self.quick_popover.set_child(quick_box)
        self.quick_menu = Gtk.MenuButton(icon_name="pan-down-symbolic", tooltip_text="Download Options",
                                          popover=self.quick_popover, sensitive=False)
        self.quick_menu.update_property([Gtk.AccessibleProperty.LABEL], ["Download Options"])
        actions = Gtk.Box()
        actions.add_css_class("linked")
        actions.append(self.download)
        actions.append(self.quick_menu)
        header.pack_end(actions)
        toolbar.add_top_bar(header)
        self.set_child(toolbar)
        page = margins(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18))
        toolbar.set_content(scrolled(page))
        urlbox = Gtk.Box(spacing=6)
        self.url = Gtk.Entry(placeholder_text="URL", hexpand=True, input_purpose=Gtk.InputPurpose.URL)
        self.url.update_property([Gtk.AccessibleProperty.LABEL], ["Media URL"])
        self.url.connect("activate", self.primary_action)
        self.url.connect("changed", self.url_changed)
        urlbox.append(self.url)
        self.paste_button = button("edit-paste-symbolic", "Paste URL", self.paste)
        urlbox.append(self.paste_button)
        self.auth_button = button("dialog-password-symbolic", "Authentication", self.edit_authentication)
        self.auth_button.set_sensitive(False)
        urlbox.append(self.auth_button)
        page.append(urlbox)
        self.message = label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True)
        self.message.set_visible(False)
        page.append(self.message)
        self.spinner = Gtk.Spinner(halign=Gtk.Align.CENTER, visible=False)
        self.spinner.update_property([Gtk.AccessibleProperty.LABEL], ["Inspecting Source"])
        page.append(self.spinner)
        options = Adw.PreferencesGroup(title="Format")
        self.options = options
        options.set_visible(False)
        self.mode = combo("Media", ["Video", "Audio"])
        self.quality = combo("Maximum Resolution", ["Best Available", "2160p", "1440p", "1080p", "720p", "480p"])
        self.audio_format = combo("Audio Format", ["Original", "MP3", "M4A", "Opus", "FLAC", "WAV", "Ogg (Vorbis)"])
        self.container = combo("Video Container", ["Automatic", "MP4", "MKV", "WebM", "MOV", "AVI"])
        self.processing = combo("Video Processing", ["Remux (Original Codecs)", "Convert When Needed"])
        self.processing.update_property([Gtk.AccessibleProperty.LABEL], ["Video Processing"])
        self.processing.set_use_subtitle(True)
        self.processing.set_subtitle_lines(2)
        self.mode.connect("notify::selected", self.mode_changed)
        settings = window.store.settings
        self.mode.set_selected(1 if settings.mode == "audio" else 0)
        self.quality.set_selected(QUALITIES.index(settings.quality))
        self.audio_format.set_selected(AUDIO_FORMATS.index(settings.audio_format))
        self.container.set_selected(CONTAINERS.index(settings.container))
        self.processing.set_selected(VIDEO_PROCESSING.index(settings.video_processing))
        self.mode_changed()
        options.add(self.mode)
        options.add(self.quality)
        options.add(self.container)
        options.add(self.audio_format)
        self.audio_encoding = combo("Audio Conversion Quality", ["Best Variable Bitrate", "128 kb/s", "192 kb/s", "256 kb/s", "320 kb/s"])
        self.audio_encoding.set_use_subtitle(True)
        self.audio_encoding.set_subtitle_lines(2)
        self.audio_encoding.set_selected(AUDIO_ENCODING_QUALITIES.index(settings.audio_encoding_quality))
        self.audio_encoding.update_property([Gtk.AccessibleProperty.LABEL], ["Audio Conversion Quality"])
        self.audio_encoding.set_tooltip_text("Used when converting audio; matching source audio may be kept without conversion")
        options.add(self.audio_encoding)
        options.add(self.processing)
        self.encoding_row = action_row("Video Encoding", encoding_summary(settings))
        self.encoding_button = button("go-next-symbolic", "Edit Video Encoding", self.edit_encoding)
        self.encoding_row.add_suffix(self.encoding_button)
        self.encoding_row.set_activatable_widget(self.encoding_button)
        options.add(self.encoding_row)
        self.conversion_note = label("Conversion may reduce quality and take longer. Files already in the target format are kept as-is.",
                                     wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, css="dim-label", visible=False)
        options.add(self.conversion_note)
        self.format_error = label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, css="error", visible=False)
        options.add(self.format_error)
        self.source_row = action_row("Source Formats", "Automatic")
        self.source_button = button("go-next-symbolic", "Choose Source Formats",
                                    lambda: self.edit_formats(self.entries[0][0]) if self.entries else None)
        self.source_row.add_suffix(self.source_button)
        self.source_row.set_activatable_widget(self.source_button)
        options.add(self.source_row)
        self.preferences_row = action_row("Source Preferences", preference_summary(settings))
        edit_preferences = button("go-next-symbolic", "Edit Source Preferences", self.edit_preferences)
        self.preferences_row.add_suffix(edit_preferences)
        self.preferences_row.set_activatable_widget(edit_preferences)
        options.add(self.preferences_row)
        self.subtitles_row = action_row("Subtitles", subtitle_summary(settings))
        edit_subtitles = button("go-next-symbolic", "Edit Subtitles", lambda: self.edit_preferences(True))
        self.subtitles_row.add_suffix(edit_subtitles)
        self.subtitles_row.set_activatable_widget(edit_subtitles)
        options.add(self.subtitles_row)
        self.transfer_row = action_row("Transfer Options", transfer_summary(settings))
        edit_transfer = button("go-next-symbolic", "Edit Transfer Options", self.edit_transfer)
        self.transfer_row.add_suffix(edit_transfer)
        self.transfer_row.set_activatable_widget(edit_transfer)
        options.add(self.transfer_row)
        self.range_row = action_row("Time Range", "Entire Media")
        edit_range = button("go-next-symbolic", "Edit Time Range", self.edit_range)
        self.range_row.add_suffix(edit_range)
        self.range_row.set_activatable_widget(edit_range)
        options.add(self.range_row)
        self.metadata = Adw.SwitchRow(title="Embed Metadata", active=settings.metadata)
        options.add(self.metadata)
        self.metadata_row = action_row("Chapters, SponsorBlock and Source Data", "")
        self.metadata_button = button("go-next-symbolic", "Edit Chapters, SponsorBlock and Source Data", self.edit_metadata)
        self.metadata_row.add_suffix(self.metadata_button)
        self.metadata_row.set_activatable_widget(self.metadata_button)
        options.add(self.metadata_row)
        self.artwork_row = action_row("Artwork", artwork_summary(settings))
        self.artwork_button = button("go-next-symbolic", "Edit Artwork", self.edit_artwork)
        self.artwork_row.add_suffix(self.artwork_button)
        self.artwork_row.set_activatable_widget(self.artwork_button)
        options.add(self.artwork_row)
        page.append(options)
        destination = Adw.PreferencesGroup()
        self.destination_group = destination
        destination.set_visible(False)
        self.destination = action_row("Save To", self.output)
        browse = button("folder-open-symbolic", "Choose Download Folder", self.choose_folder)
        self.destination.add_suffix(browse)
        self.destination.set_activatable_widget(browse)
        destination.add(self.destination)
        self.name = Adw.EntryRow(title="Name", visible=False)
        self.name.add_suffix(button("edit-undo-symbolic", "Reset Filename", self.reset_name))
        self.name.connect("changed", self.name_changed)
        destination.add(self.name)
        self.include_id = Adw.SwitchRow(title="Include Video ID", active=settings.include_id)
        self.include_id.connect("notify::active", self.refresh_names)
        destination.add(self.include_id)
        self.number_items = Adw.SwitchRow(title="Number Selected Files", visible=False)
        self.number_items.connect("notify::active", self.refresh_names)
        destination.add(self.number_items)
        self.filename_row = action_row("Filename")
        self.filename_row.set_visible(False)
        destination.add(self.filename_row)
        self.name_error = label(wrap=True, css="error", visible=False)
        destination.add(self.name_error)
        for widget in (self.mode, self.audio_format, self.container, self.processing, self.audio_encoding):
            widget.connect("notify::selected", self.refresh_names)
        page.append(destination)
        self.items_group = Adw.PreferencesGroup(title="Items", visible=False)
        self.select_all = Gtk.CheckButton(active=True, tooltip_text="Select All Items")
        self.select_all.update_property([Gtk.AccessibleProperty.LABEL], ["Select All Items"])
        self.select_all_handler = self.select_all.connect("toggled", self.toggle_all)
        self.items_group.set_header_suffix(self.select_all)
        self.items = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.items.add_css_class("boxed-list")
        self.item_search = Gtk.SearchEntry(placeholder_text="Search Playlist", margin_bottom=8)
        self.item_search.connect("search-changed", self.filter_items)
        self.items_group.add(self.item_search)
        self.order = combo("Item Order", ["Original", "Reversed", "Custom"])
        self.order.connect("notify::selected", self.order_changed)
        self.items_group.add(self.order)
        self.item_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                         propagate_natural_height=True, max_content_height=220)
        self.item_scroll.set_child(self.items)
        self.items_group.add(self.item_scroll)
        self.item_pager = Gtk.Box(spacing=6, margin_top=8, visible=False)
        self.item_previous = button("go-previous-symbolic", "Previous Items", lambda: self.change_item_page(-1))
        self.item_next = button("go-next-symbolic", "Next Items", lambda: self.change_item_page(1))
        self.item_page_label = label(hexpand=True)
        self.item_page_label.set_xalign(0.5)
        self.item_pager.append(self.item_previous)
        self.item_pager.append(self.item_page_label)
        self.item_pager.append(self.item_next)
        self.items_group.add(self.item_pager)
        self.no_matches = label("No Matching Items", css="dim-label", visible=False, margin_top=8)
        self.items_group.add(self.no_matches)
        page.append(self.items_group)
        page.reorder_child_after(self.items_group, self.spinner)
        page.reorder_child_after(destination, self.items_group)
        self.setup_disclosures(page, options, destination, urlbox)
        self.set_focus(self.url)
        self.refresh_quick_summary()
        if initial_url:
            self.url.set_text(initial_url)
        else:
            self.prefill_clipboard()

    def setup_disclosures(self, page, options, destination, urlbox):
        self.media_group = Adw.PreferencesGroup(visible=False)
        options.remove(self.mode)
        self.media_group.add(self.mode)
        page.append(self.media_group)
        page.reorder_child_after(self.media_group, self.items_group)
        page.reorder_child_after(destination, self.media_group)
        self.summary = label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True)
        self.summary.update_property([Gtk.AccessibleProperty.LABEL], ["Effective Download Choices"])
        page.append(self.summary)
        page.reorder_child_after(self.summary, destination)
        options.remove(self.format_error)
        page.append(self.format_error)
        page.reorder_child_after(self.format_error, self.summary)
        self.auth_help = Gtk.Button(label="Authentication", halign=Gtk.Align.START, visible=False)
        self.auth_help.connect("clicked", lambda *_: self.offer_authentication())
        page.append(self.auth_help)
        page.reorder_child_after(self.auth_help, self.message)
        self.more = Gtk.Expander(label="More Options", expanded=self.window.store.settings.download_expanded)
        self.more.update_property([Gtk.AccessibleProperty.LABEL], ["More Options"])
        page.append(self.more)
        advanced = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=12)
        self.more.set_child(advanced)
        self.advanced_media = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, visible=False)
        advanced.append(self.advanced_media)
        page.remove(options)
        options.set_title("Format and Conversion")
        self.advanced_media.append(options)
        sources = Adw.PreferencesGroup(title="Source and Time Range")
        captions = Adw.PreferencesGroup(title="Captions and Source Data")
        naming = Adw.PreferencesGroup(title="File Naming")
        for section, rows in ((sources, (self.source_row, self.preferences_row, self.range_row)),
                              (captions, (self.subtitles_row, self.metadata, self.metadata_row, self.artwork_row))):
            for row in rows:
                options.remove(row)
                section.add(row)
            self.advanced_media.append(section)
        for row in (self.include_id, self.number_items):
            destination.remove(row)
            naming.add(row)
        self.advanced_media.append(naming)
        self.connection_group = Adw.PreferencesGroup(title="Connection and Authentication")
        options.remove(self.transfer_row)
        self.connection_group.add(self.transfer_row)
        urlbox.remove(self.auth_button)
        self.authentication_row = action_row("Authentication")
        self.authentication_row.add_suffix(self.auth_button)
        self.authentication_row.set_activatable_widget(self.auth_button)
        self.connection_group.add(self.authentication_row)
        advanced.append(self.connection_group)
        self.reset_options = Gtk.Button(label="Reset Download Options", halign=Gtk.Align.START)
        self.reset_options.connect("clicked", lambda *_: self.reset_download_options())
        advanced.append(self.reset_options)
        self.more.connect("notify::expanded", self.disclosure_changed)
        self.quality.connect("notify::selected", self.refresh_names)
        self.metadata.connect("notify::active", self.refresh_names)

    def disclosure_changed(self, widget, _property):
        expanded = widget.get_expanded()
        self.resize_review()
        def finished(error):
            if error and not self.closed:
                self.show_message(error)
        self.window.run_action(lambda: self.window.queue.configure(
            replace(self.window.store.settings, download_expanded=expanded)), finished)

    def resize_review(self):
        self.set_content_height(720 if self.more.get_expanded() else 600 if self.entries else 240)
        if self.message.get_visible():
            self.show_message(self.message.get_text())

    def effective_options(self):
        return replace(self.media_preferences, output=self.output, mode="audio" if self.mode.get_selected() else "video",
                       quality=QUALITIES[self.quality.get_selected()], audio_format=AUDIO_FORMATS[self.audio_format.get_selected()],
                       container=CONTAINERS[self.container.get_selected()], video_processing=VIDEO_PROCESSING[self.processing.get_selected()],
                       metadata=self.metadata.get_active(), include_id=self.include_id.get_active())

    def refresh_summary(self):
        if not hasattr(self, "summary"):
            return
        from .disclosure import download_summary
        self.summary.set_text(download_summary(self.effective_options(),
                              [item for item, selected in self.entries if selected.get_active()], self.authentication.active))
        self.authentication_row.set_subtitle("Enabled for this source" if self.authentication.active else "Not enabled")

    def offer_authentication(self):
        self.more.set_expanded(True)
        self.edit_authentication()

    def reset_download_options(self):
        if self.process or self.saving:
            return
        if self.authentication.active:
            dialog = Adw.AlertDialog(heading="Reset Download Options?",
                body="Authentication will be cleared. Future submissions will no longer protect this source address in the desktop keyring. The URL, filename, destination and selection will be kept.")
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("reset", "Reset")
            dialog.set_default_response("cancel")
            dialog.set_close_response("cancel")
            dialog.connect("response", lambda _, response: self.apply_reset_options() if response == "reset" else None)
            dialog.present(self)
        else:
            self.apply_reset_options()

    def apply_reset_options(self):
        if self.process or self.saving:
            return
        from .auth import Authentication
        defaults = Settings()
        self.media_preferences = replace(self.media_preferences, subtitles=False, **preference_values(defaults))
        self.authentication, self.auth_origin = Authentication(), ""
        for item, _selection in self.entries:
            for key in ("source_choices", "clip_start", "clip_end", "precise_cuts"):
                item.pop(key, None)
        self.quality.set_selected(0)
        self.audio_format.set_selected(0)
        self.container.set_selected(0)
        self.processing.set_selected(0)
        self.audio_encoding.set_selected(0)
        self.metadata.set_sensitive(True)
        self.metadata.set_tooltip_text(None)
        self.metadata.set_active(defaults.metadata)
        self.refresh_names()
        self.refresh_summary()

    def on_closed(self, *_):
        self.closed = True
        if self.thumbnail_cancel:
            self.thumbnail_cancel.set()
        if self.process:
            self.process.cancel()
        from .auth import Authentication
        self.authentication = Authentication()

    def cancel_submission(self, *_):
        if self.saving and self.credential_task:
            self.credential_task.cancel()
            self.cancel.set_sensitive(False)
            self.show_message("Canceling authentication save...")
        elif not self.saving:
            self.close()

    def edit_authentication(self):
        if self.process or self.saving or self.entries:
            return None
        try:
            validate_url(self.url.get_text())
        except ValueError:
            return None
        from .auth_ui import AuthenticationDialog
        dialog = AuthenticationDialog(self)
        dialog.present(self.window)
        return dialog

    def edit_preferences(self, subtitles=False):
        self.media_preferences.mode = "audio" if self.mode.get_selected() else "video"
        dialog = MediaOptionsDialog(self, subtitles)
        dialog.present(self.window)
        return dialog

    def edit_transfer(self):
        self.quick_popover.popdown()
        from .transfer_ui import TransferOptionsDialog
        dialog = TransferOptionsDialog(self)
        dialog.present(self.window)
        return dialog

    def edit_encoding(self):
        from .encoding_ui import EncodingDialog
        dialog = EncodingDialog(self)
        dialog.present(self.window)
        return dialog

    def edit_metadata(self):
        from .metadata_ui import MetadataDialog
        dialog = MetadataDialog(self)
        dialog.present(self.window)
        return dialog

    def edit_artwork(self):
        from .artwork_ui import ArtworkDialog
        dialog = ArtworkDialog(self)
        dialog.present(self.window)
        return dialog

    def edit_range(self):
        from .range_ui import RangeDialog
        if len(self.entries) != 1:
            return None
        dialog = RangeDialog(self, self.entries[0][0])
        dialog.present(self.window)
        return dialog

    def url_changed(self, *_):
        if self.thumbnail_cancel:
            self.thumbnail_cancel.set()
        self.show_message("")
        self.quick_requested = False
        self.quick_menu.set_visible(True)
        self.download.set_label("Continue")
        try:
            validate_url(self.url.get_text())
            valid = True
        except ValueError:
            valid = False
        from .auth import Authentication, public_url
        if self.authentication.active and valid and public_url(self.url.get_text()) != self.auth_origin:
            self.authentication = Authentication()
            self.auth_origin = ""
        self.auth_button.set_sensitive(valid and self.process is None and not self.saving)
        title = "Authentication (Enabled)" if self.authentication.active else "Authentication"
        self.auth_button.set_tooltip_text(title)
        self.auth_button.update_property([Gtk.AccessibleProperty.LABEL], [title])
        self.refresh_quick_summary()
        if not self.authentication.active and not self.metadata.get_sensitive():
            self.metadata.set_active(self.window.store.settings.metadata)
            self.metadata.set_sensitive(True)
            self.metadata.set_tooltip_text(None)
        self.download.set_sensitive(valid and self.process is None and not self.saving)
        self.quick_button.set_sensitive(valid and self.process is None and not self.saving)
        self.quick_menu.set_sensitive(self.quick_button.get_sensitive())
        self.entries.clear()
        self.item_rows.clear()
        self.item_page = 0
        self.order.set_selected(0)
        self.item_search.set_text("")
        while child := self.items.get_first_child():
            self.items.remove(child)
        self.items_group.set_visible(False)
        self.options.set_visible(False)
        self.media_group.set_visible(False)
        self.advanced_media.set_visible(False)
        self.destination_group.set_visible(False)
        self.refresh_summary()
        self.resize_review()

    def refresh_quick_summary(self):
        from .disclosure import download_summary
        options = self.effective_options()
        summary = download_summary(options, authenticated=self.authentication.active)
        metadata = "Off (Authenticated Download)" if self.authentication.active else "On" if options.metadata else "Off"
        self.quick_summary.set_text(summary + "\nMetadata: " + metadata +
                                    ("\nAudio format: " + options.audio_format if options.mode == "audio" else "") +
                                    "\n" + self.output)
        self.refresh_summary()

    def primary_action(self, *_):
        if self.saving:
            return
        if self.entries:
            self.enqueue()
        else:
            self.inspect()

    def quick_download(self):
        if self.closed or self.saving or self.process or self.entries or not self.quick_button.get_sensitive():
            return
        self.quick_popover.popdown()
        self.inspect(quick=True)

    def prefill_clipboard(self):
        initial = self.url.get_text()
        def ready(clipboard, result):
            try:
                value = clipboard.read_text_finish(result)
                url = validate_url(value or "")
            except (GLib.Error, ValueError):
                return
            if not self.closed and not initial and self.url.get_text() == initial:
                self.url.set_text(url)
        self.url.get_clipboard().read_text_async(None, ready)

    def mode_changed(self, *_):
        audio = self.mode.get_selected() == 1
        self.quality.set_visible(not audio)
        self.container.set_visible(not audio)
        self.audio_format.set_visible(audio)
        self.processing.set_visible(not audio and self.container.get_selected() != 0)
        if hasattr(self, "source_row"):
            self.refresh_names()

    def paste(self):
        def ready(clipboard, result):
            try:
                text = clipboard.read_text_finish(result)
                if text and not self.closed:
                    if len(text.strip().splitlines()) > 1 or "\t" in text:
                        self.connect("closed", lambda *_: self.window.new_batch(text))
                        self.close()
                    else:
                        self.url.set_text(text.strip())
            except GLib.Error as error:
                self.show_message(str(error))
        self.url.get_clipboard().read_text_async(None, ready)

    def show_message(self, text):
        self.message.set_text(text)
        self.message.set_visible(bool(text))
        if hasattr(self, "auth_help"):
            from .disclosure import authentication_needed
            self.auth_help.set_visible(bool(text) and not self.entries and authentication_needed(text))
        if text and not self.entries:
            width = max(160, (self.get_child().get_width() or self.get_content_width()) - 36)
            _minimum, natural, _min_baseline, _nat_baseline = self.message.measure(Gtk.Orientation.VERTICAL, width)
            self.set_content_height(max(300, min(720, natural + 180)))

    def choose_folder(self):
        dialog = Gtk.FileDialog(title="Download Folder")
        dialog.set_initial_folder(Gio.File.new_for_path(self.output))
        def ready(dialog, result):
            try:
                folder = dialog.select_folder_finish(result).get_path()
                if folder and not self.closed:
                    self.output = folder
                    self.destination.set_subtitle(folder)
                    self.refresh_summary()
            except GLib.Error:
                pass
        dialog.select_folder(self.window, None, ready)

    def inspect(self, quick=False):
        if self.closed or self.saving or self.process:
            return
        try:
            url = validate_url(self.url.get_text())
        except ValueError as error:
            self.show_message(str(error))
            return
        self.url_changed()
        self.quick_requested = quick
        self.show_message("")
        self.spinner.set_visible(True)
        if youtube_url(url) and not installed_directory(self.window.store.directory):
            self.show_message("Installing private YouTube download support from the official yt-dlp and Deno releases. You can cancel; other applications are not changed.")
        self.spinner.start()
        self.connection_group.set_sensitive(False)
        self.reset_options.set_sensitive(False)
        for widget in (self.url, self.download, self.paste_button, self.quick_button, self.quick_menu, self.auth_button):
            widget.set_sensitive(False)
        self.process = Process()
        process = self.process
        self.window.processes.add(process)
        authentication = self.authentication
        transfer = replace(self.media_preferences)
        def work():
            try:
                result = inspect_url(url, process, self.window.store.directory,
                                     **({"transfer": transfer} if has_inspection_options(transfer) else {}),
                                     **({"authentication": authentication} if authentication.active else {}))
                GLib.idle_add(self.inspected, result, None)
            except Exception as error:
                from .auth import safe_auth_error
                GLib.idle_add(self.inspected, None, safe_auth_error(error) if authentication.active else str(error))
        threading.Thread(target=work, daemon=True).start()

    def inspected(self, entries, error):
        self.window.processes.discard(self.process)
        self.process = None
        quick = self.quick_requested
        self.quick_requested = False
        if self.closed:
            return False
        self.spinner.stop()
        self.spinner.set_visible(False)
        self.connection_group.set_sensitive(True)
        self.reset_options.set_sensitive(True)
        for widget in (self.url, self.paste_button):
            widget.set_sensitive(True)
        if error:
            self.auth_button.set_sensitive(True)
            self.download.set_label("Continue")
            self.download.set_sensitive(True)
            self.quick_button.set_sensitive(True)
            self.quick_menu.set_sensitive(True)
            self.show_message(error)
            return False
        self.quick_menu.set_visible(False)
        self.auth_button.set_sensitive(False)
        if self.authentication.active:
            self.metadata.set_active(False)
            self.metadata.set_sensitive(False)
            self.metadata.set_tooltip_text("Source metadata is disabled for authenticated downloads")
        self.bulk_selecting = True
        self.select_all.set_active(True)
        for item in entries:
            item = dict(item)
            item["original_order"] = len(self.entries)
            item["suggested_name"] = suggest_name(item["title"], item["url"], item.get("source", ""))
            item["filename"] = item.get("import_name") or item["suggested_name"]
            self.entries.append((item, ItemSelection(self.update_selection)))
        self.bulk_selecting = False
        self.render_items()
        self.items_group.set_title("Imported Items" if self.is_import else "Playlist" if len(entries) > 1 else "Media")
        self.item_scroll.set_policy(Gtk.PolicyType.NEVER,
                                    Gtk.PolicyType.AUTOMATIC if len(entries) > 1 else Gtk.PolicyType.NEVER)
        self.item_scroll.set_min_content_height(220 if len(entries) > 1 else -1)
        self.item_scroll.set_max_content_height(220 if len(entries) > 1 else -1)
        self.item_search.set_visible(len(entries) > 1)
        self.item_search.set_placeholder_text("Search Items")
        self.order.set_visible(len(entries) > 1)
        self.number_items.set_visible(len(entries) > 1)
        self.items_group.set_visible(True)
        self.select_all.set_visible(len(entries) > 1)
        self.options.set_visible(True)
        self.advanced_media.set_visible(True)
        self.media_group.set_visible(True)
        self.source_row.set_visible(len(entries) == 1)
        self.range_row.set_visible(len(entries) == 1)
        self.destination_group.set_visible(True)
        self.name.set_visible(len(entries) == 1)
        self.filename_row.set_visible(len(entries) == 1)
        if len(entries) == 1:
            self.name.set_text(self.entries[0][0]["filename"])
            if self.entries[0][0].get("destination"):
                self.output = self.entries[0][0].pop("destination")
                self.destination.set_subtitle(self.output)
        elif any(item.get("destination") for item, _ in self.entries):
            self.destination.set_title("Default Folder (some items have their own)")
        self.refresh_names()
        self.download.set_label("Download")
        self.resize_review()
        self.filter_items()
        if quick:
            if len(entries) != 1 or any(item.get("from_collection") for item in entries):
                self.show_message("Playlist found. Review items before downloading.")
            elif self.download.get_sensitive():
                self.enqueue()
            else:
                self.show_message("The saved settings need review for this media.")
        return False

    def render_items(self):
        for _, selection in self.entries:
            selection.widget = None
        while child := self.items.get_first_child():
            self.items.remove(child)
        self.item_rows = [None] * len(self.entries)
        matching = [i for i, (item, _) in enumerate(self.entries) if self.matches(item)]
        self.item_page = min(self.item_page, max(0, (len(matching) - 1) // 100))
        start = self.item_page * 100
        indices = range(len(self.entries)) if len(self.entries) <= 100 else matching[start:start + 100]
        for index in indices:
            item, selection = self.entries[index]
            duration = item.get("duration")
            subtitle = item.get("source", "")
            if duration is not None:
                seconds = int(duration)
                subtitle = f"{seconds // 60}:{seconds % 60:02d}  {subtitle}"
            row = action_row(item["title"], subtitle)
            if len(self.entries) == 1:
                row.set_size_request(-1, 84)
                row.set_title_lines(0)
                if item.get("thumbnail") and not self.authentication.active:
                    from .thumbnail import load_thumbnail
                    if self.thumbnail_cancel:
                        self.thumbnail_cancel.set()
                    preview = Gtk.Picture(width_request=96, height_request=60, can_shrink=True,
                                          content_fit=Gtk.ContentFit.CONTAIN, visible=False)
                    preview.update_property([Gtk.AccessibleProperty.LABEL], ["Video Thumbnail"])
                    row.add_prefix(preview)
                    def thumbnail_ready(pixbuf, preview=preview):
                        if not self.closed:
                            preview.set_paintable(Gdk.Texture.new_for_pixbuf(pixbuf))
                            preview.set_visible(True)
                    self.thumbnail_cancel = load_thumbnail(item["thumbnail"], replace(self.media_preferences), thumbnail_ready)
            check = Gtk.CheckButton(active=selection.get_active(), valign=Gtk.Align.CENTER)
            selection.widget = check
            check.update_property([Gtk.AccessibleProperty.LABEL], [f"Download {item['title']}"])
            check.connect("toggled", lambda widget, selection=selection: selection.set_active(widget.get_active()))
            if len(self.entries) > 1:
                row.add_prefix(check)
            if len(self.entries) > 1:
                row.add_suffix(button("document-edit-symbolic", "Edit Filename",
                                      lambda item=item: self.edit_name(item)))
                row.add_suffix(button("emblem-system-symbolic", "Choose Source Formats",
                                      lambda item=item: self.edit_formats(item)))
            row.set_tooltip_text(item["title"])
            if len(self.entries) > 1:
                row.set_activatable_widget(check)
            self.items.append(row)
            self.item_rows[index] = row
        self.item_pager.set_visible(len(matching) > 100)
        self.item_previous.set_sensitive(self.item_page > 0)
        self.item_next.set_sensitive(start + 100 < len(matching))
        self.item_page_label.set_text(f"{start + 1}-{min(start + 100, len(matching))} of {len(matching)}")

    def change_item_page(self, delta):
        self.item_page += delta
        self.render_items()
        self.refresh_names()
        self.item_scroll.get_vadjustment().set_value(0)

    def order_changed(self, *_):
        if self.order.get_selected() == 2:
            return
        self.entries.sort(key=lambda entry: entry[0]["original_order"], reverse=self.order.get_selected() == 1)
        self.item_page = 0
        self.render_items()
        self.refresh_names()

    def move_item(self, item, position):
        index = next(i for i, (entry, _) in enumerate(self.entries) if entry is item)
        if index == position:
            return
        self.entries.insert(position, self.entries.pop(index))
        self.order.set_selected(2)
        self.item_page = 0
        self.render_items()
        self.refresh_names()

    def effective_name(self, item):
        if self.number_items.get_active() and len(self.entries) > 1 and item.get("number"):
            return numbered_name(item["filename"], item["number"], self.selected_count)
        return item["filename"]

    def filename_preview(self, item):
        stem = output_stem(self.effective_name(item), item.get("media_id", ""), item["url"], self.include_id.get_active())
        extension = output_extension("audio" if self.mode.get_selected() else "video",
                                     AUDIO_FORMATS[self.audio_format.get_selected()], CONTAINERS[self.container.get_selected()])
        return stem + ("." + extension if extension else "")

    def edit_name(self, item):
        editor = FilenameDialog(self, item)
        editor.present(self.window)
        return editor

    def source_choice(self, item):
        return item.get("source_choices", {}).get("audio" if self.mode.get_selected() else "video")

    def edit_formats(self, item):
        editor = SourceFormatDialog(self, item)
        editor.present(self.window)
        return editor

    def reset_name(self):
        if len(self.entries) == 1:
            self.name.set_text(self.entries[0][0]["suggested_name"])

    def name_changed(self, *_):
        if len(self.entries) == 1:
            self.entries[0][0]["filename"] = self.name.get_text()
            self.refresh_names()

    def refresh_names(self, *_):
        self.media_preferences.mode = "audio" if self.mode.get_selected() else "video"
        self.media_preferences.audio_encoding_quality = AUDIO_ENCODING_QUALITIES[self.audio_encoding.get_selected()]
        self.audio_encoding.set_visible(bool(self.mode.get_selected()) and AUDIO_FORMATS[self.audio_format.get_selected()] in {"mp3", "m4a", "opus", "vorbis"})
        if self.container.get_selected() == 0:
            self.processing.set_selected(0)
        self.processing.set_visible(not self.mode.get_selected() and self.container.get_selected() != 0)
        self.conversion_note.set_visible(not self.mode.get_selected() and self.processing.get_selected() == 1)
        self.conversion_note.set_text("Re-encoding may reduce quality and take longer." if self.media_preferences.reencode_video else
                                     "Conversion may reduce quality and take longer. Matching containers are kept unchanged.")
        self.encoding_row.set_visible(self.conversion_note.get_visible())
        self.encoding_row.set_subtitle(encoding_summary(self.media_preferences))
        self.metadata_row.set_subtitle(metadata_summary(self.media_preferences))
        if self.media_preferences.sponsorblock != "off":
            from .sponsorblock import summary as sponsor_summary
            self.metadata_row.set_subtitle(sponsor_summary(self.media_preferences) + " · " + metadata_summary(self.media_preferences))
        self.artwork_row.set_subtitle(artwork_summary(self.media_preferences))
        automatic = ((self.audio_format.get_selected() == 0) if self.mode.get_selected()
                     else self.container.get_selected() == 0)
        self.filename_row.set_title("Filename (automatic extension)" if automatic else "Filename")
        self.preferences_row.set_subtitle(preference_summary(self.media_preferences))
        self.subtitles_row.set_subtitle(subtitle_summary(self.media_preferences))
        self.transfer_row.set_subtitle(transfer_summary(self.media_preferences))
        if hasattr(self, "summary"):
            self.refresh_quick_summary()
        if len(self.entries) == 1:
            item = self.entries[0][0]
            self.range_row.set_subtitle(range_summary(item.get("clip_start", 0), item.get("clip_end"), item.get("precise_cuts", False)))
        self.update_selection()
        self.refresh_summary()

    def refresh_item_labels(self):
        self.selected_count = sum(check.get_active() for _, check in self.entries)
        position = 0
        for item, check in self.entries:
            position += bool(check.get_active())
            item["number"] = position if check.get_active() else None
        error = ""
        for (item, check), row in zip(self.entries, self.item_rows):
            try:
                preview = self.filename_preview(item)
            except ValueError as problem:
                preview = ""
                if check.get_active():
                    error = f"{item['filename']}: {problem}"
            if row is None:
                continue
            row.set_title(item["filename"])
            if len(self.entries) > 1:
                choice = self.source_choice(item)
                row.set_subtitle(preview + (" · " + choice["summary"] if choice else "") +
                                 (" · " + item["destination"] if item.get("destination") else ""))
            else:
                self.filename_row.set_subtitle(preview)
                choice = self.source_choice(item)
                self.source_row.set_subtitle(choice["summary"] if choice else "Automatic")
        self.name_error.set_text(error)
        self.name_error.set_visible(bool(error))

    def matches(self, item):
        query = self.item_search.get_text().casefold()
        return query in item["title"].casefold() or query in item["filename"].casefold()

    def filter_items(self, *_):
        self.item_page = 0
        if len(self.entries) > 100:
            self.render_items()
        for (item, _check), row in zip(self.entries, self.item_rows):
            if row:
                row.set_visible(self.matches(item))
        self.update_selection()

    def update_selection(self):
        if self.bulk_selecting or not self.entries:
            return
        self.refresh_item_labels()
        automatic_items = any(check.get_active() and not self.source_choice(item) for item, check in self.entries)
        self.preferences_row.set_sensitive(automatic_items)
        self.preferences_row.set_subtitle(preference_summary(self.media_preferences) if automatic_items else
                                          "Exact source choices take precedence")
        self.quality.set_visible(not self.mode.get_selected() and automatic_items)
        count = sum(check.get_active() for _, check in self.entries)
        matching = [check for item, check in self.entries if self.matches(item)]
        selected = sum(check.get_active() for check in matching)
        hidden = count - selected
        self.items_group.set_description((f"{count} of {len(self.entries)} selected" +
                                         (f" · {hidden} hidden by search" if hidden else "")) if len(self.entries) > 1 else "")
        self.item_scroll.set_visible(bool(matching))
        self.no_matches.set_visible(not matching)
        valid_names = True
        for item, check in self.entries:
            if check.get_active():
                try:
                    validate_name(self.effective_name(item))
                except ValueError:
                    valid_names = False
        self.download.set_sensitive(count > 0 and valid_names and not self.saving)
        problem = ""
        self.media_preferences.mode = "audio" if self.mode.get_selected() else "video"
        container = CONTAINERS[self.container.get_selected()]
        try:
            validate_output_options(replace(self.media_preferences, container=container,
                                            video_processing=VIDEO_PROCESSING[self.processing.get_selected()]))
        except ValueError as error:
            problem = str(error)
        if not problem and not self.mode.get_selected():
            if self.processing.get_selected() == 0:
                problem = next((p for item, check in self.entries if check.get_active() and
                                (p := remux_problem(item.get("formats", []), self.source_choice(item), container))), "")
        self.format_error.set_text(problem)
        self.format_error.set_visible(bool(problem))
        if problem:
            self.download.set_sensitive(False)
        self.select_all.handler_block(self.select_all_handler)
        try:
            self.select_all.set_active(bool(matching) and selected == len(matching))
            self.select_all.set_inconsistent(0 < selected < len(matching))
            self.select_all.set_sensitive(bool(matching))
            name = "Select Matching Items" if self.item_search.get_text() else "Select All Items"
            self.select_all.set_tooltip_text(name)
            self.select_all.update_property([Gtk.AccessibleProperty.LABEL], [name])
        finally:
            self.select_all.handler_unblock(self.select_all_handler)
        self.refresh_summary()

    def toggle_all(self, *_):
        selected = self.select_all.get_active()
        self.bulk_selecting = True
        try:
            for item, check in self.entries:
                if self.matches(item):
                    check.set_active(selected)
        finally:
            self.bulk_selecting = False
        self.update_selection()

    def enqueue(self, *_):
        if self.saving:
            return
        settings = self.window.store.settings
        remembered = replace(settings, **preference_values(self.media_preferences), output=self.output,
                             subtitle_presets=self.media_preferences.subtitle_presets,
                             metadata=self.metadata.get_active(),
                             mode="audio" if self.mode.get_selected() else "video",
                             quality=QUALITIES[self.quality.get_selected()],
                             audio_format=AUDIO_FORMATS[self.audio_format.get_selected()],
                             container=CONTAINERS[self.container.get_selected()],
                             video_processing=VIDEO_PROCESSING[self.processing.get_selected()],
                             include_id=self.include_id.get_active())
        jobs = [Job(url=item["url"], title=item["title"], output=item.get("destination") or self.output, **preference_values(remembered),
                    mode=remembered.mode, quality=remembered.quality,
                    audio_format=remembered.audio_format, container=remembered.container,
                    video_processing=remembered.video_processing,
                    metadata=remembered.metadata, subtitles=remembered.subtitles,
                    source_format=(self.source_choice(item) or {}).get("primary"),
                    source_audio=(self.source_choice(item) or {}).get("audio"),
                    source_summary=(self.source_choice(item) or {}).get("summary", ""),
                    clip_start=item.get("clip_start", 0), clip_end=item.get("clip_end"), precise_cuts=item.get("precise_cuts", False),
                    filename=self.effective_name(item), media_id=item.get("media_id", ""), include_id=remembered.include_id)
                for item, check in self.entries if check.get_active()]
        # Browser impersonation belongs to these jobs, not to unrelated future sources.
        remembered.browser_identity = settings.browser_identity
        if not jobs or not self.download.get_sensitive():
            return
        self.saving = True
        self.set_can_close(False)
        self.download.set_sensitive(False)
        self.download.set_label("Saving")
        def current_ui_state():
            return replace(remembered, settings_expanded=self.window.store.settings.settings_expanded,
                           download_expanded=self.window.store.settings.download_expanded)
        def saved(error):
            self.saving = False
            self.cancel.set_sensitive(True)
            self.set_can_close(True)
            self.download.set_sensitive(True)
            self.download.set_label("Download")
            if error:
                self.show_message(error)
            else:
                self.window.stack.set_visible_child_name("downloads")
                self.close()
        if self.authentication.active:
            from .auth import CredentialOperation, enqueue_authenticated, safe_auth_error
            task = CredentialOperation()
            self.credential_task = task
            authentication, origin = self.authentication, self.auth_origin
            self.window.processes.add(task)
            # Authentication must not turn off the user's general metadata preference.
            remembered.metadata = settings.metadata
            def work():
                try:
                    enqueue_authenticated(self.window.queue, jobs, current_ui_state(), authentication, task.process, origin)
                except Exception as problem:
                    raise RuntimeError(safe_auth_error(problem)) from None
                finally:
                    task.done.set()
            def finished(error):
                self.window.processes.discard(task)
                self.credential_task = None
                saved(error)
            self.window.run_action(work, finished)
        else:
            self.window.run_action(lambda: self.window.queue.add(jobs, current_ui_state()), saved)


class JobRow(Gtk.ListBoxRow):
    def __init__(self, window, job):
        super().__init__(activatable=False)
        self.window = window
        self.job = job
        box = margins(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8), 12)
        line = Gtk.Box(spacing=12)
        self.icon = Gtk.Image(pixel_size=24)
        line.append(self.icon)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        self.title = label(ellipsize=Pango.EllipsizeMode.END)
        self.title.add_css_class("heading")
        self.subtitle = label(css="dim-label", ellipsize=Pango.EllipsizeMode.END)
        texts.append(self.title)
        texts.append(self.subtitle)
        line.append(texts)
        self.pause = button("media-playback-pause-symbolic", "Pause Transfer", self.toggle_transfer)
        line.append(self.pause)
        self.command = button("process-stop-symbolic", "Cancel Download", self.perform)
        line.append(self.command)
        self.details = button("document-properties-symbolic", "Download Details", lambda: window.details(self.job))
        line.append(self.details)
        self.remove = button("window-close-symbolic", "Remove from History (Keep Files)",
                             lambda: window.confirm_job_removal(self.job, window))
        line.append(self.remove)
        box.append(line)
        self.startup = label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, visible=False)
        box.append(self.startup)
        self.progress = Gtk.ProgressBar()
        box.append(self.progress)
        self.set_child(box)
        self.update(job)

    def update(self, job):
        self.job = job
        self.title.set_text(display_title(job))
        self.title.set_tooltip_text(display_title(job))
        summary = job_summary(job)
        if self.subtitle.get_text() != summary:
            self.subtitle.set_text(summary)
            self.subtitle.set_tooltip_text(summary)
        waiting = not job.transfer_paused and job.status == "Downloading" and job.detail.startswith(("Inspecting source  ·", "Starting transfer  ·"))
        self.subtitle.set_visible(not waiting and not job.transfer_paused)
        self.startup.set_visible(waiting or job.transfer_paused)
        if job.transfer_paused:
            self.startup.set_text("Transfer Paused")
        elif waiting:
            self.startup.set_text("\n".join(job.detail.split("  ·  ")[:2]))
        self.progress.set_fraction(job.progress)
        self.progress.set_visible(job.status in ACTIVE)
        self.icon.set_from_icon_name(ICONS["audio-x-generic-symbolic" if job.mode == "audio" else "video-x-generic-symbolic"])
        if job.status == "Completed":
            icon, title = "folder-open-symbolic", "Open Download Folder"
        elif job.status in TERMINAL:
            icon, title = "view-refresh-symbolic", "Retry Download"
        else:
            icon, title = "process-stop-symbolic", "Cancel Download"
        self.command.set_icon_name(ICONS.get(icon, icon))
        self.command.set_tooltip_text(title)
        self.command.update_property([Gtk.AccessibleProperty.LABEL], [title])
        self.command.set_sensitive(job.status != "Canceling")
        self.remove.set_visible(job.status in TERMINAL)
        self.pause.set_visible(job.status == "Downloading" or job.transfer_paused)
        self.pause.set_sensitive(job.status != "Canceling")
        title = "Resume Transfer" if job.transfer_paused else "Pause Transfer"
        self.pause.set_icon_name("media-playback-start-symbolic" if job.transfer_paused else "media-playback-pause-symbolic")
        self.pause.set_tooltip_text(title)
        self.pause.update_property([Gtk.AccessibleProperty.LABEL], [title])

    def toggle_transfer(self):
        identifier, paused = self.job.id, not self.job.transfer_paused
        self.window.run_action(lambda: self.window.queue.pause_transfer(identifier, paused))

    def perform(self):
        if self.job.status == "Completed":
            self.window.open_folder(self.job.output)
        elif self.job.status in TERMINAL:
            self.window.run_action(lambda: self.window.queue.retry(self.job.id))
            self.window.stack.set_visible_child_name("downloads")
        else:
            self.window.run_action(lambda: self.window.queue.cancel(self.job.id))


class Preferences(Adw.PreferencesDialog):
    def __init__(self, window):
        super().__init__(title="Preferences")
        self.window = window
        self.failed_settings = {}
        page = Adw.PreferencesPage()
        self.add(page)
        appearance = Adw.PreferencesGroup(title="Appearance")
        page.add(appearance)
        self.appearance = combo("Color Scheme", ["System", "Light", "Dark"])
        self.appearance.update_property([Gtk.AccessibleProperty.LABEL], ["Color Scheme"])
        self.appearance.set_selected(["system", "light", "dark"].index(window.store.settings.appearance))
        self.appearance.connect("notify::selected", self.appearance_changed)
        appearance.add(self.appearance)
        self.remember_window = Adw.SwitchRow(title="Remember Window Size", active=window.store.settings.remember_window,
                                            subtitle="Includes maximized state")
        self.remember_window.update_property([Gtk.AccessibleProperty.LABEL], ["Remember Window Size"])
        self.remember_window.connect("notify::active", self.remember_window_changed)
        appearance.add(self.remember_window)
        group = Adw.PreferencesGroup(title="Downloads")
        page.add(group)
        self.destination = action_row("Download Folder", window.store.settings.output)
        browse = button("folder-open-symbolic", "Choose Download Folder", self.choose_folder)
        self.destination.add_suffix(browse)
        self.destination.set_activatable_widget(browse)
        group.add(self.destination)
        concurrency = Adw.SpinRow.new_with_range(1, 10, 1)
        concurrency.set_title("Simultaneous Downloads")
        concurrency.set_value(window.store.settings.concurrency)
        concurrency.connect("notify::value", lambda row, _: self.setting("concurrency", int(row.get_value())))
        group.add(concurrency)
        for key, title in (("metadata", "Embed Metadata"),):
            row = Adw.SwitchRow(title=title, active=getattr(window.store.settings, key))
            row.connect("notify::active", lambda row, _, key=key: self.setting(key, row.get_active()))
            group.add(row)
        awake = Adw.SwitchRow(title="Prevent Suspend While Downloading", active=window.store.settings.prevent_suspend)
        awake.connect("notify::active", lambda row, _: self.setting("prevent_suspend", row.get_active()))
        group.add(awake)
        completion = Adw.PreferencesGroup(title="When Downloads Finish")
        page.add(completion)
        self.completion_settings = {}
        for key, title in (("notifications", "Show Notifications"),
                           ("open_folder_on_completion", "Open Folder When Finished")):
            row = Adw.SwitchRow(title=title, active=getattr(window.store.settings, key))
            row.connect("notify::active", lambda row, _, key=key: self.setting(key, row.get_active()))
            completion.add(row)
            self.completion_settings[key] = row
        storage = Adw.PreferencesGroup(title="Retained Files")
        page.add(storage)
        cleanup = action_row("Abandoned Downloads", "Temporary files for completed or removed downloads; checked automatically every minute")
        cleanup.set_subtitle_lines(0)
        cleanup_button = button("edit-clear-all-symbolic", "Remove Abandoned Files", self.clean_abandoned)
        cleanup.add_suffix(cleanup_button)
        cleanup.set_activatable_widget(cleanup_button)
        storage.add(cleanup)
        history = Adw.PreferencesGroup(title="History")
        page.add(history)
        self.retention = combo("Auto-clear History", CLEAR_LABELS)
        self.retention.set_use_subtitle(True)
        self.retention.set_selected(RETENTION_SECONDS.index(retention_seconds(window.store.settings)))
        self.retention.set_tooltip_text("Only successful downloads are cleared. Downloaded files are not removed.")
        self.retention.update_property([Gtk.AccessibleProperty.LABEL], ["Auto-clear Successful Downloads from History"])
        self.retention.connect("notify::selected", self.retention_changed)
        history.add(self.retention)
        authentication = Adw.PreferencesGroup(title="Authentication")
        page.add(authentication)
        credentials = action_row("Saved Authentication", "Manage desktop keyring entries")
        manage = button("dialog-password-symbolic", "Manage Saved Authentication", self.manage_authentication)
        credentials.add_suffix(manage)
        credentials.set_activatable_widget(manage)
        authentication.add(credentials)
        engine = Adw.PreferencesGroup(title="Download Engine")
        page.add(engine)
        self.engine_row = action_row("Download Support", "Checking installed version")
        self.update = button("software-update-available-symbolic", "Update Download Support", self.install)
        self.engine_row.add_suffix(self.update)
        self.cancel_update = button("process-stop-symbolic", "Cancel Engine Update", self.stop_update)
        self.cancel_update.set_visible(False)
        self.engine_row.add_suffix(self.cancel_update)
        engine.add(self.engine_row)
        self.progress = Gtk.ProgressBar(visible=False, margin_top=8)
        engine.add(self.progress)
        self.engine_message = label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, margin_top=8)
        engine.add(self.engine_message)
        dependencies = action_row("Dependencies", "Local versions and executable paths")
        self.dependencies_button = button("go-next-symbolic", "Check Dependencies", self.check_dependencies)
        dependencies.add_suffix(self.dependencies_button)
        dependencies.set_activatable_widget(self.dependencies_button)
        engine.add(dependencies)
        # Keep everyday choices visible; group the less common settings locally.
        for section in (appearance, group, completion, storage, history, authentication, engine):
            page.remove(section)
        everyday = Adw.PreferencesGroup()
        self.everyday = everyday
        page.add(everyday)
        group.remove(self.destination)
        appearance.remove(self.appearance)
        everyday.add(self.destination)
        everyday.add(self.appearance)
        for row in self.completion_settings.values():
            completion.remove(row)
            everyday.add(row)
        history.remove(self.retention)
        everyday.add(self.retention)
        self.background = Adw.SwitchRow(title="Keep running when the window closes",
                                        active=window.store.settings.background_mode)
        everyday.add(self.background)
        self.tray_style = Adw.ComboRow(title="Tray icon appearance",
                                      model=Gtk.StringList.new(["Color", "Symbolic"]),
                                      use_subtitle=True,
                                      selected=0 if window.store.settings.tray_appearance == "color" else 1,
                                      sensitive=self.background.get_active())
        everyday.add(self.tray_style)
        def background_changed(row, _property):
            self.tray_style.set_sensitive(row.get_active())
            self.setting("background_mode", row.get_active())
        self.background.connect("notify::active", background_changed)
        self.tray_style.connect("notify::selected", lambda row, _property:
                               self.setting("tray_appearance", ("color", "symbolic")[row.get_selected()]))
        self.settings_error = label(wrap=True, css="error", visible=bool(window.queue.persistence_error))
        self.settings_error.set_text(window.queue.persistence_error)
        everyday.add(self.settings_error)
        self.retry_settings = Gtk.Button(label="Retry Saving", halign=Gtk.Align.START,
                                         visible=bool(window.queue.persistence_error))
        self.retry_settings.connect("clicked", self.retry_settings_save)
        everyday.add(self.retry_settings)
        self.engine_status = Adw.PreferencesGroup(visible=False)
        page.add(self.engine_status)
        engine.remove(self.progress)
        engine.remove(self.engine_message)
        self.engine_status.add(self.progress)
        self.engine_status.add(self.engine_message)
        self.engine_attention = Gtk.Button(label="Review Download Engine", halign=Gtk.Align.START)
        self.engine_attention.connect("clicked", lambda *_: self.advanced.set_expanded(True))
        self.engine_status.add(self.engine_attention)
        self.engine_message.connect("notify::label", lambda *_: self.engine_status.set_visible(bool(self.engine_message.get_text())))
        advanced_group = Adw.PreferencesGroup()
        page.add(advanced_group)
        self.advanced = Gtk.Expander(label="Advanced Settings", expanded=window.store.settings.settings_expanded)
        self.advanced.update_property([Gtk.AccessibleProperty.LABEL], ["Advanced Settings"])
        advanced_group.add(self.advanced)
        advanced_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=12)
        self.advanced.set_child(advanced_box)
        appearance.set_title("Window")
        for section in (group, appearance, storage, authentication, engine):
            advanced_box.append(section)
        self.advanced.connect("notify::expanded", self.disclosure_changed)
        self.version()

    def disclosure_changed(self, widget, _property):
        self.setting("settings_expanded", widget.get_expanded())

    def check_dependencies(self, *_):
        from .diagnostics_ui import DependenciesDialog
        dialog = DependenciesDialog(self.window)
        dialog.present(self.window)
        return dialog

    def restore_retention(self):
        self.retention.handler_block_by_func(self.retention_changed)
        try:
            self.retention.set_selected(RETENTION_SECONDS.index(retention_seconds(self.window.store.settings)))
        finally:
            self.retention.handler_unblock_by_func(self.retention_changed)
        self.retention.set_sensitive(True)

    def retention_changed(self, row, _property):
        seconds = RETENTION_SECONDS[row.get_selected()]
        row.set_sensitive(False)
        def apply():
            def finished(error):
                self.restore_retention()
                if error:
                    self.window.toast.add_toast(Adw.Toast(title=error))
            self.window.run_action(lambda: self.window.queue.set_history_clear(seconds), finished)
        if not seconds:
            apply()
            return
        interval = CLEAR_LABELS[RETENTION_SECONDS.index(seconds)]
        body = ("Successful downloads will be cleared immediately." if seconds == -1 else
                f"Successful downloads will be cleared after {interval.lower()}. Older completed records will be cleared now.")
        dialog = Adw.AlertDialog(heading="Auto-clear Successful Downloads?",
                                 body=body + " Downloaded files are not removed. Failed, canceled and interrupted records are kept.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("apply", "Apply")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("apply", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda _dialog, response: apply() if response == "apply" else self.restore_retention())
        dialog.present(self)

    def clean_abandoned(self):
        dialog = Adw.AlertDialog(heading="Remove Abandoned Files?",
                                 body="Remove recognized temporary files for completed or removed downloads in known download folders. Downloaded files and partials for queued, running, failed, canceled or interrupted downloads are kept. Other folders are not scanned.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        def respond(_dialog, response):
            if response != "remove":
                return
            result = []
            def work():
                result.extend(self.window.queue.clean_abandoned())
            def finished(error):
                message = error or f"Removed {result[0]} temporary folders; skipped {result[1]} folders requiring review"
                self.window.toast.add_toast(Adw.Toast(title=message))
            self.window.run_action(work, finished)
        dialog.connect("response", respond)
        dialog.present(self)

    def manage_authentication(self):
        from .auth_ui import AuthenticationManager
        dialog = AuthenticationManager(self.window)
        dialog.present(self.window)
        return dialog

    def setting(self, key, value):
        def finished(error):
            if error:
                self.failed_settings[key] = value
            else:
                self.failed_settings.pop(key, None)
            message = error or ("Some settings could not be saved." if self.failed_settings else "")
            self.settings_error.set_text(message)
            self.settings_error.set_visible(bool(message))
            self.retry_settings.set_visible(bool(message))
        self.window.run_action(lambda: self.window.queue.configure(replace(self.window.store.settings, **{key: value})), finished)

    def retry_settings_save(self, *_):
        self.retry_settings.set_sensitive(False)
        pending = self.failed_settings.copy()
        def finished(error):
            message = error or self.window.queue.persistence_error
            if not message:
                for key, value in pending.items():
                    if self.failed_settings.get(key) == value:
                        self.failed_settings.pop(key, None)
            self.settings_error.set_text(message or "")
            self.settings_error.set_visible(bool(message))
            self.retry_settings.set_visible(bool(message))
            self.retry_settings.set_sensitive(True)
        def save():
            if pending:
                self.window.queue.configure(replace(self.window.store.settings, **pending))
            else:
                self.window.queue.retry_save()
        self.window.run_action(save, finished)

    def appearance_changed(self, row, _property):
        value = ["system", "light", "dark"][row.get_selected()]
        row.set_sensitive(False)
        def finished(error):
            row.set_sensitive(True)
            if error:
                self.window.toast.add_toast(Adw.Toast(title=error))
                index = ["system", "light", "dark"].index(self.window.store.settings.appearance)
                row.handler_block_by_func(self.appearance_changed)
                try:
                    row.set_selected(index)
                finally:
                    row.handler_unblock_by_func(self.appearance_changed)
        self.window.run_action(lambda: self.window.queue.configure(
            replace(self.window.store.settings, appearance=value)), finished)

    def remember_window_changed(self, row, _property):
        value = row.get_active()
        row.set_sensitive(False)
        def finished(error):
            row.set_sensitive(True)
            if error:
                row.handler_block_by_func(self.remember_window_changed)
                row.set_active(self.window.store.settings.remember_window)
                row.handler_unblock_by_func(self.remember_window_changed)
                self.window.toast.add_toast(Adw.Toast(title=error))
        self.window.run_action(lambda: self.window.queue.configure(replace(self.window.store.settings, remember_window=value)), finished)

    def choose_folder(self):
        dialog = Gtk.FileDialog(title="Download Folder")
        dialog.set_initial_folder(Gio.File.new_for_path(self.window.store.settings.output))
        def ready(dialog, result):
            try:
                folder = dialog.select_folder_finish(result).get_path()
                if folder:
                    self.setting("output", folder)
                    self.destination.set_subtitle(folder)
            except GLib.Error:
                pass
        dialog.select_folder(self.window, None, ready)

    def version(self):
        if self.window.closing:
            return
        process = Process()
        self.window.processes.add(process)
        def work():
            try:
                path = engine_path(self.window.store.directory)
                value = process.communicate([path, "--version"], timeout=20).strip()
                managed = Path(path).is_relative_to(self.window.store.directory)
                text = f"{value} ({'Managed by Pyrobolic' if managed else 'System'})"
                support = installed_directory(self.window.store.directory)
                if support:
                    versions = json.loads((support / "versions.json").read_text())
                    text += " · " + versions["deno"]
            except Exception as error:
                text = str(error)
                GLib.idle_add(self.engine_message.set_text, text)
            GLib.idle_add(self.window.processes.discard, process)
            GLib.idle_add(self.engine_row.set_subtitle, text)
        threading.Thread(target=work, daemon=True).start()

    def install(self):
        if self.window.updating:
            self.engine_message.set_text("An update is already in progress.")
            return
        self.window.updating = True
        self.update_control = UpdateControl()
        self.window.processes.add(self.update_control)
        self.update.set_sensitive(False)
        self.cancel_update.set_visible(True)
        self.cancel_update.set_sensitive(True)
        self.progress.set_visible(True)
        self.engine_message.set_text("Installing private yt-dlp and Deno support from their official releases. Other applications are not changed.")
        self.set_can_close(False)
        def progress(value):
            GLib.idle_add(self.progress.set_fraction, value)
        def work():
            try:
                text = update_support(self.window.store.directory, progress, self.update_control)
            except Exception as error:
                text = str(error)
            GLib.idle_add(self.installed, text)
        threading.Thread(target=work, daemon=True).start()

    def stop_update(self):
        self.update_control.cancel()
        self.cancel_update.set_sensitive(False)
        self.engine_message.set_text("Canceling update; waiting for any current network read to finish")

    def installed(self, text):
        self.window.processes.discard(self.update_control)
        self.window.updating = False
        self.cancel_update.set_visible(False)
        self.update.set_sensitive(True)
        self.progress.set_visible(False)
        self.engine_message.set_text(text)
        self.set_can_close(True)
        self.version()
        return False


class Window(Adw.ApplicationWindow):
    def __init__(self, application, store):
        settings = store.settings
        super().__init__(application=application, title="Pyrobolic",
                         default_width=settings.window_width if settings.remember_window else 880,
                         default_height=settings.window_height if settings.remember_window else 650)
        if settings.remember_window and settings.window_maximized:
            self.maximize()
        self.set_size_request(360, 360)
        self.store = store
        self.appearance = None
        self.apply_appearance()
        self.refresh_lock = threading.Lock()
        self.refresh_pending = False
        self.closing = False
        self.updating = False
        self.download_dialog = None
        self.batch_dialog = None
        self.actions = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pyro-state")
        self.processes = set()
        self.queue = Queue(store, self.changed)
        self.rows = {}
        self.jobs = {}
        self.paused = store.paused
        self.history_limit = 100
        self.inhibit_cookie = 0
        self.inhibit_attempted = False
        self.connect("destroy", lambda *_: self.release_inhibition())
        self.connect("close-request", self.close_requested)
        self.toast = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        self.set_content(toolbar)
        header = Adw.HeaderBar()
        self.new_button = button("list-add-symbolic", "New Download", self.new_download)
        header.pack_start(self.new_button)
        self.pause_button = button("pyrobolic-pause-symbolic", "Pause Queued Downloads", self.toggle_queue)
        header.pack_start(self.pause_button)
        menu = Gio.Menu()
        imports = Gio.Menu()
        imports.append("_Import Downloads", "app.import-downloads")
        menu.append_section(None, imports)
        queue_menu = Gio.Menu()
        queue_menu.append("_Retry Failed Downloads", "app.retry-failed")
        queue_menu.append("_Cancel All Downloads", "app.cancel-all")
        queue_menu.append("Clear _History", "app.clear-history")
        menu.append_section(None, queue_menu)
        app_menu = Gio.Menu()
        for title, action in (("_Preferences", "app.preferences"), ("_About Pyrobolic", "app.about")):
            app_menu.append(title, action)
        menu.append_section(None, app_menu)
        self.quit_menu = Gio.Menu()
        menu.append_section(None, self.quit_menu)
        self.menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", tooltip_text="Main Menu", menu_model=menu)
        self.menu_button.update_property([Gtk.AccessibleProperty.LABEL], ["Main Menu"])
        header.pack_end(self.menu_button)
        self.folder_button = button("folder-open-symbolic", "Open Download Folder",
                                    lambda: self.open_folder(self.store.settings.output))
        header.pack_end(self.folder_button)
        toolbar.add_top_bar(header)
        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        titles = Gtk.Stack(hhomogeneous=False)
        titles.add_named(switcher, "switcher")
        titles.add_named(Adw.WindowTitle(title="Pyrobolic"), "title")
        header.set_title_widget(titles)
        bottom = Adw.ViewSwitcherBar(stack=self.stack, reveal=False)
        self.view_switcher, self.bottom_switcher = switcher, bottom
        self.minimum_width_pending = False
        bottom.connect("notify::reveal", self.queue_minimum_width)
        self.connect("notify::visible-dialog", self.queue_minimum_width)
        self.get_dialogs().connect("items-changed", self.queue_minimum_width)
        settings = Gtk.Settings.get_default()
        self.font_settings_handlers = [settings.connect(signal, self.queue_minimum_width) for signal in
                                       ("notify::gtk-font-name", "notify::gtk-xft-dpi")]
        self.connect("destroy", lambda *_: [settings.disconnect(handler) for handler in self.font_settings_handlers])
        toolbar.add_bottom_bar(bottom)
        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 550sp"))
        breakpoint.add_setter(titles, "visible-child-name", "title")
        breakpoint.add_setter(bottom, "reveal", True)
        self.add_breakpoint(breakpoint)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.queue_banner = Adw.Banner(title="Queue Paused", button_label="Resume")
        self.queue_banner.connect("button-clicked", lambda *_: self.run_action(lambda: self.queue.set_paused(False)))
        content.append(self.queue_banner)
        self.save_banner = Adw.Banner(title="Unable to Save Changes", button_label="Retry")
        self.save_banner.connect("button-clicked", lambda *_: self.run_action(self.queue.retry_save))
        content.append(self.save_banner)
        self.stack.set_vexpand(True)
        content.append(self.stack)
        self.toast.set_child(content)
        toolbar.set_content(self.toast)
        self.lists = {}
        self.pages = {}
        for name, title, icon, empty in (("downloads", "Downloads", "folder-download-symbolic", "No Downloads"),
                                          ("history", "History", "document-open-recent-symbolic", "No History")):
            view = Gtk.Stack()
            status = Adw.StatusPage(title=empty, icon_name=ICONS.get(icon, icon))
            if name == "downloads":
                add = Gtk.Button(label="New Download", halign=Gtk.Align.CENTER)
                add.add_css_class("suggested-action")
                add.add_css_class("pill")
                add.connect("clicked", lambda *_: self.new_download())
                status.set_child(add)
            view.add_named(status, "empty")
            box = margins(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12))
            if name == "history":
                self.search = Gtk.SearchEntry(placeholder_text="Search History")
                self.search.update_property([Gtk.AccessibleProperty.LABEL], ["Search History"])
                self.search.connect("search-changed", self.search_history)
                searchbar = Gtk.Box(spacing=6)
                self.search.set_hexpand(True)
                searchbar.append(self.search)
                sort_menu = Gio.Menu()
                for value, sort_title in (("newest", "Newest First"), ("oldest", "Oldest First"), ("title", "Title")):
                    item = Gio.MenuItem.new(sort_title, None)
                    item.set_action_and_target_value("win.history-sort", GLib.Variant("s", value))
                    sort_menu.append_item(item)
                self.sort_action = Gio.SimpleAction.new_stateful("history-sort", GLib.VariantType.new("s"),
                                                               GLib.Variant("s", self.store.settings.history_sort))
                self.sort_action.connect("activate", lambda _action, value: self.change_history_sort(value.get_string()))
                self.add_action(self.sort_action)
                self.sort_button = Gtk.MenuButton(icon_name="view-sort-descending-symbolic", tooltip_text="Sort History", menu_model=sort_menu)
                self.sort_button.update_property([Gtk.AccessibleProperty.LABEL], ["Sort History"])
                searchbar.append(self.sort_button)
                box.append(searchbar)
                self.history_no_matches = label("No Matching Downloads", css="dim-label", visible=False)
                box.append(self.history_no_matches)
            listing = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, valign=Gtk.Align.START)
            listing.add_css_class("boxed-list")
            listing.set_sort_func(lambda a, b, history=name == "history":
                                  self.compare_jobs(a.job, b.job, history))
            box.append(listing)
            if name == "history":
                self.more_history = Gtk.Button(label="Load More", halign=Gtk.Align.CENTER)
                self.more_history.connect("clicked", self.load_more_history)
                box.append(self.more_history)
            clamp = Adw.Clamp(maximum_size=900, tightening_threshold=650)
            clamp.set_child(box)
            scroll = scrolled(clamp)
            if name == "history":
                self.history_scroll = scroll
            view.add_named(scroll, "list")
            self.lists[name] = listing
            self.pages[name] = view
            self.stack.add_titled_with_icon(view, name, title, ICONS.get(icon, icon))
        self.refresh()
        self.update_minimum_width()
        self.retention_timer = GLib.timeout_add_seconds(60, self.prune_history)
        self.connect("unrealize", self.stop_retention_timer)
        self.prune_history()

    def prune_history(self):
        if self.closing:
            return True
        def maintain():
            if retention_seconds(self.store.settings):
                self.queue.prune_history()
            self.queue.clean_abandoned()
        self.run_action(maintain)
        return True

    def stop_retention_timer(self, *_):
        if getattr(self, "retention_timer", 0):
            GLib.source_remove(self.retention_timer)
            self.retention_timer = 0

    def destroy(self):
        self.stop_retention_timer()
        super().destroy()

    def queue_minimum_width(self, *_):
        if not self.minimum_width_pending:
            self.minimum_width_pending = True
            GLib.idle_add(self.update_minimum_width)

    def update_minimum_width(self):
        self.minimum_width_pending = False
        if not self.closing:
            minimum = round(Adw.LengthUnit.to_px(Adw.LengthUnit.SP, 360, Gtk.Settings.get_default()))
            if self.bottom_switcher.get_reveal():
                # Breakpoint windows do not propagate their child's minimum size.
                content_minimum = self.get_content().measure(Gtk.Orientation.HORIZONTAL, -1)[0]
                decoration = max(0, self.get_allocation().width - self.get_width())
                minimum = max(minimum, content_minimum + decoration)
                dialogs = self.get_dialogs()
                for index in range(dialogs.get_n_items()):
                    dialog = dialogs.get_item(index)
                    child = dialog.get_child()
                    if child:
                        minimum = max(minimum, child.measure(Gtk.Orientation.HORIZONTAL, -1)[0] + decoration)
            self.set_size_request(max(360, minimum), 360)
        return False

    def run_action(self, operation, callback=None):
        if self.closing:
            return
        def work():
            error = None
            try:
                operation()
            except Exception as failure:
                error = str(failure)
            GLib.idle_add(finished, error)
        def finished(error):
            if callback:
                callback(error)
            elif error and not self.closing:
                self.toast.add_toast(Adw.Toast(title=error))
            return False
        self.actions.submit(work)

    def changed(self):
        with self.refresh_lock:
            if not self.refresh_pending and not self.closing:
                self.refresh_pending = True
                GLib.idle_add(self.refresh)

    def refresh(self):
        with self.refresh_lock:
            self.refresh_pending = False
        if self.closing or self.get_application() is None:
            return False
        changes = self.queue.changes()
        if changes is None:
            with self.refresh_lock:
                if not self.refresh_pending:
                    self.refresh_pending = True
                    GLib.timeout_add(50, self.refresh)
            return False
        full, jobs, self.paused, error = changes
        self.apply_appearance()
        application = self.get_application()
        application.sync_background(self)
        if bool(self.quit_menu.get_n_items()) != self.store.settings.background_mode:
            self.quit_menu.remove_all()
            if self.store.settings.background_mode:
                self.quit_menu.append("_Quit", "app.quit")
        self.save_banner.set_revealed(bool(error))
        self.save_banner.set_tooltip_text(error or None)
        self.save_banner.update_property([Gtk.AccessibleProperty.DESCRIPTION], [error])
        if full:
            self.jobs = {job.id: job for job in jobs}
            self.render_jobs()
            self.completion_feedback()
        else:
            for job in jobs:
                self.jobs[job.id] = job
                if row := self.rows.get(job.id):
                    row.update(job)
        self.sync_inhibition()
        return False

    def apply_appearance(self):
        value = self.store.settings.appearance
        if self.appearance != value:
            schemes = {"system": Adw.ColorScheme.DEFAULT, "light": Adw.ColorScheme.FORCE_LIGHT,
                       "dark": Adw.ColorScheme.FORCE_DARK}
            Adw.StyleManager.get_default().set_color_scheme(schemes[value])
            self.appearance = value

    def history_key(self, job):
        if self.store.settings.history_sort == "title":
            return (display_title(job).casefold(), -job.created, job.id)
        return ((job.created if self.store.settings.history_sort == "oldest" else -job.created), job.id)

    def compare_jobs(self, a, b, history):
        first, second = (self.history_key(a), self.history_key(b)) if history else ((a.created, a.id), (b.created, b.id))
        return (first > second) - (first < second)

    def change_history_sort(self, order):
        self.sort_action.set_enabled(False)
        def finished(error):
            self.sort_action.set_enabled(True)
            if error:
                self.toast.add_toast(Adw.Toast(title=error))
                return
            self.sort_action.set_state(GLib.Variant("s", order))
            self._anchor_generation = getattr(self, "_anchor_generation", 0) + 1
            self.history_limit = 100
            self.history_scroll.get_vadjustment().set_value(0)
            self.lists["history"].invalidate_sort()
            self.render_jobs()
        self.run_action(lambda: self.queue.configure(replace(self.store.settings, history_sort=order)), finished)

    def release_inhibition(self):
        if self.inhibit_cookie:
            self.get_application().uninhibit(self.inhibit_cookie)
            self.inhibit_cookie = 0
        self.inhibit_attempted = False

    def sync_inhibition(self):
        needed = (not self.closing and self.store.settings.prevent_suspend and
                  any(j.status in {"Downloading", "Processing"} and not j.transfer_paused for j in self.jobs.values()))
        if not needed:
            self.release_inhibition()
        elif not self.inhibit_attempted:
            self.inhibit_attempted = True
            try:
                self.inhibit_cookie = self.get_application().inhibit(self, Gtk.ApplicationInhibitFlags.SUSPEND, "Downloads are running")
            except GLib.Error:
                self.inhibit_cookie = 0
            if not self.inhibit_cookie:
                self.toast.add_toast(Adw.Toast(title="The desktop could not prevent suspend"))

    def completion_feedback(self):
        if self.closing:
            return False
        events = self.queue.take_completions()
        if events is None:
            GLib.timeout_add(50, self.completion_feedback)
            return False
        folders = set()
        for event in events:
            count = len(event.jobs)
            title = "Download Complete" if count == 1 else f"{count} Downloads Complete"
            job = event.jobs[-1]
            body = (job.filename or job.title) if count == 1 else f"{count} downloads saved"
            if event.failed:
                body += f"; {event.failed} failed"
            if event.canceled:
                body += f"; {event.canceled} canceled or interrupted"
            immediate = not any(record.id == job.id for record in self.store.jobs)
            action_label = "Open Folder" if immediate else "Show in History"
            toast = Adw.Toast(title=title, button_label=action_label, timeout=8)
            if immediate:
                toast.connect("button-clicked", lambda _toast, folder=job.output: self.open_folder(folder))
            else:
                toast.connect("button-clicked", lambda _toast, identifier=job.id: self.show_download(identifier))
            self.toast.add_toast(toast)
            if self.store.settings.notifications:
                notification = Gio.Notification.new(title)
                notification.set_body(body)
                notification.set_icon(Gio.ThemedIcon.new(APP_ID))
                action = "app.open-completed-folder" if immediate else "app.show-download"
                target = job.output if immediate else job.id
                notification.set_default_action_and_target(action, GLib.Variant("s", target))
                notification.add_button_with_target(action_label, action, GLib.Variant("s", target))
                try:
                    self.get_application().send_notification("completed-" + (job.batch_id or job.id), notification)
                except (GLib.Error, RuntimeError):
                    pass  # History and the in-app action remain available without a notification service.
            if self.store.settings.open_folder_on_completion:
                folders.update(j.output for j in event.jobs)
        for folder in sorted(folders):
            self.open_folder(folder)
        return False

    def show_download(self, identifier):
        self.search.set_text("")
        history = sorted((j for j in self.jobs.values() if j.status in TERMINAL), key=self.history_key)
        index = next((i for i, job in enumerate(history) if job.id == identifier), 0)
        self.history_limit = max(100, index + 1)
        self.render_jobs()
        self.stack.set_visible_child_name("history")
        self.present()
        if row := self.rows.get(identifier):
            row.details.grab_focus()

    def search_history(self, *_):
        self.history_limit = 100
        self.render_jobs()

    def load_more_history(self, *_):
        self.history_limit += 100
        self.render_jobs()

    def history_anchor(self):
        query = self.search.get_text()
        previous, self._history_query = getattr(self, "_history_query", None), query
        if previous != query or self.stack.get_visible_child_name() != "history":
            return None
        value = self.history_scroll.get_vadjustment().get_value()
        if value <= 0:
            return None
        visible = []
        for identifier, row in self.rows.items():
            if row.get_parent() == self.lists["history"] and row.get_mapped():
                okay, bounds = row.compute_bounds(self.history_scroll)
                if okay and bounds.get_y() + bounds.get_height() > 0 and bounds.get_y() < self.history_scroll.get_height():
                    visible.append((bounds.get_y(), identifier))
        if visible:
            y, identifier = min(visible)
            return identifier, y, value, query
        return None

    def restore_history_anchor(self, anchor):
        identifier, y, value, query = anchor
        generation = self._anchor_generation = getattr(self, "_anchor_generation", 0) + 1
        frames = 0
        def restore(_widget, _clock):
            nonlocal frames
            frames += 1
            if frames < 2:
                return True  # Wait for allocations after rows are inserted or moved.
            adjustment = self.history_scroll.get_vadjustment()
            if (generation != self._anchor_generation or self.search.get_text() != query or
                    self.stack.get_visible_child_name() != "history" or abs(adjustment.get_value() - value) > 1):
                return False
            if row := self.rows.get(identifier):
                okay, bounds = row.compute_bounds(self.history_scroll)
                if okay:
                    adjustment.set_value(value + bounds.get_y() - y)
            return False
        self.history_scroll.add_tick_callback(restore)

    def render_jobs(self):
        focus = self.get_focus()
        focused_row = next((row for row in self.rows.values()
                            if focus is not None and (focus == row or focus.is_ancestor(row))), None)
        previous_parent = focused_row.get_parent() if focused_row else None
        anchor = self.history_anchor()
        jobs = list(self.jobs.values())
        query = self.search.get_text().casefold()
        history = sorted((j for j in jobs if j.status in TERMINAL and
                          query in (j.title + " " + (j.filename or "") + " " + j.url).casefold()), key=self.history_key)
        visible = [j for j in jobs if j.status not in TERMINAL] + history[:self.history_limit]
        identifiers = {j.id for j in visible}
        for identifier in list(self.rows):
            if identifier not in identifiers:
                row = self.rows.pop(identifier)
                row.get_parent().remove(row)
        counts = {"downloads": sum(j.status not in TERMINAL for j in jobs),
                  "history": sum(j.status in TERMINAL for j in jobs)}
        for job in visible:
            name = "history" if job.status in TERMINAL else "downloads"
            row = self.rows.get(job.id)
            if not row:
                row = JobRow(self, job)
                self.rows[job.id] = row
            elif row.job != job:
                row.update(job)
            parent = row.get_parent()
            if parent != self.lists[name]:
                if parent:
                    parent.remove(row)
                self.lists[name].append(row)
        if anchor:
            self.restore_history_anchor(anchor)
        if focused_row and previous_parent == self.lists["downloads"] and focused_row.get_parent() != previous_parent:
            if self.stack.get_visible_child_name() == "downloads":
                following = next((row for row in self.rows.values() if row.get_parent() == previous_parent), None)
                (following.command if following else self.new_button).grab_focus()
        if focused_row and previous_parent == self.lists["history"] and focused_row.get_parent() != previous_parent:
            if self.stack.get_visible_child_name() == "history":
                following = self.lists["history"].get_row_at_index(0)
                (following.remove if following else self.search).grab_focus()
        for name in counts:
            self.pages[name].set_visible_child_name("list" if counts[name] else "empty")
        self.history_no_matches.set_visible(bool(counts["history"]) and not history)
        self.more_history.set_visible(len(history) > self.history_limit)
        paused = self.paused
        can_pause = any(j.status in {"Queued", "Downloading", "Processing"} for j in jobs)
        self.pause_button.set_sensitive(can_pause)
        self.queue_banner.set_revealed(paused and can_pause)
        running = sum(j.status in {"Downloading", "Processing", "Canceling"} for j in jobs)
        self.queue_banner.set_title(f"Queue Paused · {running} Active" if running else "Queue Paused")
        self.pause_button.set_icon_name("pyrobolic-start-symbolic" if paused else "pyrobolic-pause-symbolic")
        name = "Resume Queued Downloads" if paused else "Pause Queued Downloads"
        self.pause_button.set_tooltip_text(name)
        self.pause_button.update_property([Gtk.AccessibleProperty.LABEL], [name])
        self.get_application().lookup_action("cancel-all").set_enabled(any(j.status in ACTIVE and j.status != "Canceling" for j in jobs))
        self.get_application().lookup_action("retry-failed").set_enabled(any(j.status in {"Failed", "Interrupted"} for j in jobs))
        self.get_application().lookup_action("clear-history").set_enabled(bool(counts["history"]))
        self.stack.get_page(self.pages["downloads"]).set_badge_number(counts["downloads"])
        return False

    def toggle_queue(self):
        self.run_action(lambda: self.queue.set_paused(not self.store.paused))

    def retry_failed(self):
        self.run_action(self.queue.retry_failed)
        self.stack.set_visible_child_name("downloads")

    def cancel_all(self):
        count = sum(j.status in ACTIVE for j in self.jobs.values())
        if not count:
            return
        dialog = Adw.AlertDialog(heading="Cancel All Downloads?",
                                 body=f"{count} downloads will be canceled. Partial files will be kept.")
        dialog.add_response("keep", "Keep Downloading")
        dialog.add_response("cancel", "Cancel Downloads")
        dialog.set_response_appearance("cancel", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("keep")
        dialog.set_close_response("keep")
        dialog.connect("response", lambda _, response: self.run_action(self.queue.cancel_all) if response == "cancel" else None)
        dialog.present(self)

    def new_download(self, initial_url=None):
        if self.closing:
            return None
        if self.download_dialog and not self.download_dialog.closed:
            return self.download_dialog
        dialog = DownloadDialog(self, initial_url)
        self.download_dialog = dialog
        dialog.present(self)
        return dialog

    def new_batch(self, text=""):
        if self.closing:
            return None
        if self.download_dialog and not self.download_dialog.closed:
            return self.download_dialog
        if self.batch_dialog and not self.batch_dialog.closed:
            return self.batch_dialog
        from .batch_ui import BatchDialog
        self.batch_dialog = BatchDialog(self, text)
        self.batch_dialog.present(self)
        return self.batch_dialog

    def offer_clipboard(self):
        def ready(clipboard, result):
            try:
                url = validate_url(clipboard.read_text_finish(result) or "")
            except (GLib.Error, ValueError):
                return
            if not self.closing and self.download_dialog is None and self.get_visible_dialog() is None:
                self.new_download(url)
        self.get_display().get_clipboard().read_text_async(None, ready)

    def open_folder(self, path):
        launcher = Gtk.FileLauncher(file=Gio.File.new_for_path(path))
        def finished(launcher, result):
            try:
                launcher.launch_finish(result)
            except GLib.Error as error:
                self.toast.add_toast(Adw.Toast(title=str(error)))
        launcher.launch(self, None, finished)

    def open_saved_file(self, job, path):
        result = []
        def work():
            result.append(self.queue.saved_file(job.id, path))
        def finished(error):
            if error:
                self.toast.add_toast(Adw.Toast(title=error))
            elif not self.closing:
                self.open_folder(result[0])
        self.run_action(work, finished)

    def open_source(self, job):
        try:
            url = validate_url(job.url)
        except ValueError as error:
            self.toast.add_toast(Adw.Toast(title=str(error)))
            return
        launcher = Gtk.UriLauncher(uri=url)
        def finished(launcher, result):
            try:
                launcher.launch_finish(result)
            except GLib.Error as error:
                self.toast.add_toast(Adw.Toast(title=str(error)))
        launcher.launch(self, None, finished)

    def search_downloads(self):
        self.stack.set_visible_child_name("history")
        self.search.grab_focus()

    def switch_view(self, name):
        self.stack.set_visible_child_name(name)

    def details(self, job):
        dialog = Adw.Dialog(title="Download Details", content_width=600, content_height=480)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        box = margins(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12))
        box.append(label(display_title(job), css="title-3", wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True))
        if job.title != display_title(job):
            box.append(label("Source Title", css="heading"))
            box.append(label(job.title, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True))
        box.append(label(job.url, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True))
        if job.auth_ref:
            box.append(label("Authenticated Download", css="heading"))
            box.append(label("The full source URL and authentication are in the desktop keyring. "
                             "Source metadata and raw engine logs are disabled. Open Source Page opens only the site address.",
                             wrap=True, max_width_chars=45))
        source = action_row("Source Page")
        source_button = button("web-browser-symbolic", "Open Source Page", lambda: self.open_source(job))
        source.add_suffix(source_button)
        source.set_activatable_widget(source_button)
        box.append(source)
        box.append(label(job_summary(job), wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True))
        box.append(label(transfer_summary(job), wrap=True, selectable=True))
        box.append(label(metadata_summary(job), wrap=True, selectable=True))
        if job.artwork_mode != "off":
            box.append(label("Artwork: " + artwork_summary(job), wrap=True, selectable=True))
        if job.mode == "video" and job.video_processing == "convert":
            box.append(label("Video Encoding (Always)" if job.reencode_video else
                             "Video Encoding (When Conversion Is Needed)", css="heading"))
            box.append(label(encoding_summary(job), wrap=True, selectable=True))
        if job.clip_start or job.clip_end is not None:
            box.append(label(range_summary(job.clip_start, job.clip_end, job.precise_cuts), wrap=True, selectable=True))
        if job.status in TERMINAL:
            actions = Adw.PreferencesGroup(title="Saved Record")
            again = action_row("Download Again", "Same options; a new record and a separate file")
            again_button = button("folder-download-symbolic", "Download Again",
                                  lambda: self.confirm_redownload(job, dialog))
            again.add_suffix(again_button)
            again.set_activatable_widget(again_button)
            actions.add(again)
            remove = action_row("Remove from History", "Downloaded files are not removed.")
            remove_button = button("user-trash-symbolic", "Remove from History",
                                   lambda: self.confirm_job_removal(job, dialog))
            remove.add_suffix(remove_button)
            remove.set_activatable_widget(remove_button)
            actions.add(remove)
            if job.output_stem is not None:
                partial = action_row("Remove Retained Files", "Published media is kept; the next retry starts over")
                partial_button = button("edit-clear-all-symbolic", "Remove Retained Files",
                                        lambda: self.confirm_job_removal(job, dialog, partial=True))
                partial.add_suffix(partial_button)
                partial.set_activatable_widget(partial_button)
                actions.add(partial)
            box.append(actions)
        if job.status == "Completed":
            files = Adw.PreferencesGroup(title="Downloaded Files")
            for path in job.files:
                row = action_row(Path(path).name, path)
                launch = button("document-open-symbolic", "Open Downloaded File",
                                lambda path=path: self.open_saved_file(job, path))
                row.add_suffix(launch)
                row.set_activatable_widget(launch)
                files.add(row)
            box.append(files)
            playlist = action_row("Batch Playlist", "Completed media, in submission order")
            export = button("document-save-symbolic", "Export Batch Playlist", lambda: self.export_playlist(job))
            playlist.add_suffix(export)
            playlist.set_activatable_widget(export)
            box.append(playlist)
        if job.source_format:
            box.append(label("Source Formats", css="heading"))
            box.append(label(job.source_summary or job.source_format, wrap=True,
                             wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True))
        box.append(label("Source Preferences (overridden by exact selection)" if job.source_format else
                         "Source Preferences", css="heading", wrap=True))
        box.append(label(preference_summary(job), wrap=True, selectable=True))
        if job.selected_source:
            box.append(label("Selected Source", css="heading"))
            box.append(label(job.selected_source, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True))
        if subtitle_mode(job) != "off":
            box.append(label("Subtitles", css="heading"))
            box.append(label(subtitle_summary(job), wrap=True, selectable=True))
        for warning in job.warnings:
            box.append(label(warning, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True, css="warning"))
        if job.mode == "video" and job.container != "auto":
            processing = "Convert When Needed" if job.video_processing == "convert" else "Remux (Original Codecs)"
            box.append(label(f"{job.container.upper()} · {processing}", wrap=True, selectable=True))
        text = label("\n".join(job.files + job.log) or job.status, css="monospace",
                     selectable=True, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
        text.update_property([Gtk.AccessibleProperty.LABEL], ["Saved Files and Download Log"])
        box.append(text)
        toolbar.set_content(scrolled(box))
        dialog.set_child(toolbar)
        dialog.present(self)
        return dialog

    def confirm_redownload(self, job, parent):
        dialog = Adw.AlertDialog(heading="Download Again?",
                                 body="Use this record's saved options and destination. Existing files are kept; name conflicts get a numbered filename. Exact streams must still be available.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("download", "Download")
        dialog.set_close_response("cancel")
        dialog.set_default_response("cancel")
        dialog.set_response_appearance("download", Adw.ResponseAppearance.SUGGESTED)
        def respond(_dialog, response):
            if response != "download":
                return
            parent.set_can_close(False)
            def finished(error):
                parent.set_can_close(True)
                if error:
                    self.toast.add_toast(Adw.Toast(title=error))
                else:
                    parent.close()
                    self.switch_view("downloads")
                    self.toast.add_toast(Adw.Toast(title="Added to Downloads"))
            self.run_action(lambda: self.queue.redownload(job.id), finished)
        dialog.connect("response", respond)
        dialog.present(parent)
        return dialog

    def confirm_job_removal(self, job, parent, partial=False):
        title = "Remove Retained Files?" if partial else "Remove from History?"
        body = ("This removes this download's staging files. Published media is kept. A retry will start over."
                if partial else "Downloaded files are not removed.")
        dialog = Adw.AlertDialog(heading=title, body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_close_response("cancel")
        dialog.set_default_response("cancel")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        def respond(_dialog, response):
            if response != "remove":
                return
            operation = (lambda: self.queue.discard_partial(job.id)) if partial else (lambda: self.queue.remove_history({job.id}))
            def finished(error):
                if error:
                    self.toast.add_toast(Adw.Toast(title=error))
                else:
                    if parent is not self:
                        parent.close()
                    self.toast.add_toast(Adw.Toast(title="Retained files removed" if partial else "Removed from History"))
            self.run_action(operation, finished)
        dialog.connect("response", respond)
        dialog.present(parent)
        return dialog

    def export_playlist(self, job):
        dialog = Gtk.FileDialog(title="Export Batch Playlist", initial_name="Playlist.m3u8")
        dialog.set_initial_folder(Gio.File.new_for_path(job.output))
        def ready(dialog, result):
            try:
                target = dialog.save_finish(result).get_path()
            except GLib.Error:
                return
            if not target or self.closing:
                return
            result_count = []
            def work():
                with self.queue.lock:
                    jobs = [replace(j, files=j.files[:]) for j in self.store.jobs
                            if (j.batch_id or j.id) == (job.batch_id or job.id)]
                result_count.append(export_m3u(jobs, target))
            def finished(error):
                self.toast.add_toast(Adw.Toast(title=error or f"Exported {result_count[0]} completed media files"))
            self.run_action(work, finished)
        dialog.save(self, None, ready)

    def clear_history(self):
        dialog = Adw.AlertDialog(heading="Clear History?", body="Downloaded files will be kept.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("clear", "Clear History")
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _, response: self.run_action(self.queue.clear_history) if response == "clear" else None)
        dialog.present(self)

    def close_requested(self, *_):
        if not self.closing and self.get_application().tray.ready:
            if not self.get_visible():
                return True
            from .window_placement import capture, surface_id
            self.hide_generation = getattr(self, "hide_generation", 0) + 1
            generation = self.hide_generation
            identifier = surface_id(self)
            def captured(position):
                if not self.closing and generation == self.hide_generation and self.get_application():
                    if self.get_application().tray.ready:
                        self.tray_position = position
                        self.tray_window_state = (self.is_maximized(), self.is_fullscreen())
                        self.set_visible(False)
                return False
            def work():
                GLib.idle_add(captured, capture(identifier))
            self.actions.submit(work)
            return True
        return self.request_quit()

    def present(self):
        self.hide_generation = getattr(self, "hide_generation", 0) + 1
        position = getattr(self, "tray_position", None)
        self.tray_position = None
        state = getattr(self, "tray_window_state", (False, False))
        self.tray_window_state = (False, False)
        if state[1]:
            self.fullscreen()
        elif state[0]:
            self.maximize()
        super().present()
        if position is not None and not any(state) and not self.is_maximized() and not self.is_fullscreen():
            from .window_placement import restore, surface_id
            identifier = surface_id(self)
            self.actions.submit(restore, identifier, position)

    def request_quit(self):
        if self.closing:
            return True
        self.present()
        if getattr(self, "quit_dialog", None):
            self.quit_dialog.present(self)
            return True
        if self.updating:
            dialog = Adw.AlertDialog(heading="Cancel Engine Update and Quit?",
                                     body="The installed engine will be kept. An in-progress network read may take up to 30 seconds to stop.")
            dialog.add_response("keep", "Keep Updating")
            dialog.add_response("quit", "Cancel and Quit")
            dialog.set_close_response("keep")
            dialog.connect("response", lambda _, response: self.finish_close() if response == "quit" else None)
            self.quit_dialog = dialog
            dialog.connect("closed", lambda *_: setattr(self, "quit_dialog", None))
            dialog.present(self)
            return True
        if any(job.status in ACTIVE for job in self.jobs.values()):
            dialog = Adw.AlertDialog(heading="Cancel Downloads and Quit?",
                                     body="Partial downloads will be kept and can be retried.")
            dialog.add_response("keep", "Keep Downloading")
            dialog.add_response("quit", "Cancel and Quit")
            dialog.set_response_appearance("quit", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("keep")
            dialog.set_close_response("keep")
            dialog.connect("response", lambda _, response: self.finish_close() if response == "quit" else None)
            self.quit_dialog = dialog
            dialog.connect("closed", lambda *_: setattr(self, "quit_dialog", None))
            dialog.present(self)
        else:
            self.finish_close()
        return True

    def finish_close(self):
        if self.closing:
            return
        self.closing = True
        self.release_inhibition()
        self.set_sensitive(False)
        processes = list(self.processes)
        for process in processes:
            process.cancel()
        width, height = self.get_default_size()
        maximized = self.is_maximized()
        def wait_for_workers():
            self.queue.shutdown()
            while True:
                with self.queue.lock:
                    busy = bool(self.queue.running)
                if not busy:
                    break
                time.sleep(0.05)
            for process in processes:
                process.close()
            if self.store.settings.remember_window:
                try:
                    self.queue.configure(replace(self.store.settings, window_width=max(360, min(16384, width)),
                                                 window_height=max(360, min(16384, height)),
                                                 window_maximized=maximized))
                except Exception:
                    pass  # configure retains the save error for the existing Keep Open/Quit prompt.
            GLib.idle_add(self.close_ready)
        self.actions.submit(wait_for_workers)

    def close_ready(self):
        self.set_sensitive(True)
        if self.queue.persistence_error:
            dialog = Adw.AlertDialog(heading="Unable to Save Changes",
                                     body="Recent history and settings may be lost if you quit now.")
            dialog.add_response("stay", "Keep Open")
            dialog.add_response("quit", "Quit Without Saving")
            dialog.set_response_appearance("quit", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("stay")
            dialog.set_close_response("stay")
            def response(_, choice):
                if choice == "quit":
                    self.actions.shutdown(wait=False)
                    self.get_application().stop_background()
                    self.destroy()
                else:
                    self.closing = False
                    self.queue.stopped = False
                    self.changed()
            dialog.connect("response", response)
            dialog.present(self)
        else:
            self.actions.shutdown(wait=False)
            self.get_application().stop_background()
            self.destroy()
        return False


class RecoveryWindow(Adw.ApplicationWindow):
    def __init__(self, application, directory):
        super().__init__(application=application, title="Pyrobolic", default_width=560, default_height=440)
        self.directory = directory
        self.busy = False
        self.page = Adw.StatusPage(title="Opening Saved Data", icon_name="document-open-recent-symbolic")
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(scrolled(self.page))
        self.set_content(toolbar)
        self.controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        self.retry = Gtk.Button(label="Try Again")
        self.restore = Gtk.Button(label="Restore From File")
        self.fresh = Gtk.Button(label="Start Fresh")
        self.retry.connect("clicked", lambda *_: self.load())
        self.restore.connect("clicked", self.choose_recovery)
        self.fresh.connect("clicked", self.confirm_fresh)
        for widget in (self.retry, self.restore, self.fresh):
            self.controls.append(widget)
        self.page.set_child(self.controls)
        self.connect("close-request", lambda *_: self.busy)

    def load(self, recover=False, source=None):
        if self.busy:
            return
        self.busy = True
        self.controls.set_sensitive(False)
        self.page.set_title("Opening Saved Data")
        self.page.set_description("")
        def work():
            try:
                if recover:
                    store, backup = Store.recover(self.directory, source)
                else:
                    store, backup = Store(self.directory), None
                GLib.idle_add(self.loaded, store, backup, None)
            except Exception as error:
                GLib.idle_add(self.loaded, None, None, str(error))
        threading.Thread(target=work, daemon=True).start()

    def loaded(self, store, backup, error):
        self.busy = False
        if error:
            self.page.set_title("Unable to Read Saved Data")
            self.page.set_description(error)
            self.page.set_icon_name("dialog-error-symbolic")
            self.controls.set_sensitive(True)
            self.retry.grab_focus()
        else:
            application = self.get_application()
            application.store = store
            application.window = Window(application, store)
            application.window.present()
            if backup:
                application.window.toast.add_toast(Adw.Toast(title=f"Original data preserved in {backup.name}"))
            if application.pending_urls:
                application.show_pending_urls()
            elif application.pending_download is not None:
                application.show_pending_download()
            elif application.pending_folder is not None:
                application.show_pending_folder()
            else:
                application.window.offer_clipboard()
            self.destroy()
        return False

    def choose_recovery(self, *_):
        dialog = Gtk.FileDialog(title="Restore Saved Data")
        file_filter = Gtk.FileFilter()
        file_filter.set_name("JSON Saved Data")
        file_filter.add_pattern("*.json")
        dialog.set_default_filter(file_filter)
        def ready(dialog, result):
            try:
                path = dialog.open_finish(result).get_path()
                if path:
                    self.load(recover=True, source=path)
            except GLib.Error:
                pass
        dialog.open(self, None, ready)

    def confirm_fresh(self, *_):
        dialog = Adw.AlertDialog(heading="Start With Empty History and Default Settings?",
                                 body="The original saved data will be preserved in a separate file. Downloaded media will not be changed.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("fresh", "Start Fresh")
        dialog.set_response_appearance("fresh", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _, choice: self.load(recover=True) if choice == "fresh" else None)
        dialog.present(self)


class Application(Adw.Application):
    def __init__(self, store=None, directory=None):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.store = store
        self.window = None
        self.directory = directory or data_directory()
        self.pending_download = None
        self.pending_folder = None
        self.pending_urls = []
        from .tray import Tray
        self.background_held = False
        self.exiting = False
        self.tray = Tray(self.activate, self.tray_add, self.request_quit, self.tray_availability)

    def sync_background(self, window):
        if not self.exiting:
            self.tray.configure(window.store.settings.background_mode, window.store.settings.tray_appearance)

    def tray_availability(self, available):
        if available and not self.background_held:
            self.hold()
            self.background_held = True
        elif not available:
            if self.window and not self.exiting and not self.window.get_visible():
                self.window.present()
            if self.background_held:
                self.release()
                self.background_held = False

    def stop_background(self):
        self.exiting = True
        self.tray.close()

    def request_quit(self):
        if isinstance(self.window, Window):
            self.window.request_quit()
        elif self.window:
            self.window.close()

    def tray_add(self):
        self.activate()
        if isinstance(self.window, Window) and not self.window.closing:
            if self.window.batch_dialog and not self.window.batch_dialog.closed:
                self.window.batch_dialog.present(self.window)
            else:
                dialog = self.window.new_download()
                if dialog:
                    dialog.present(self.window)

    def do_shutdown(self):
        self.stop_background()
        Adw.Application.do_shutdown(self)

    def do_startup(self):
        Adw.Application.do_startup(self)
        Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(
            str(Path(__file__).resolve().parent / "icons"))
        Gtk.Window.set_default_icon_name(APP_ID)
        actions = {"new-download": lambda: self.window.new_download(),
                   "import-downloads": lambda: self.window.new_batch(),
                   "show-downloads": lambda: self.window.switch_view("downloads"),
                   "show-history": lambda: self.window.switch_view("history"),
                   "search-history": lambda: self.window.search_downloads(),
                   "open-download-folder": lambda: self.window.open_folder(self.window.store.settings.output),
                   "main-menu": lambda: self.window.menu_button.popup(),
                   "preferences": lambda: Preferences(self.window).present(self.window),
                   "clear-history": lambda: self.window.clear_history(),
                   "retry-failed": lambda: self.window.retry_failed(),
                   "cancel-all": lambda: self.window.cancel_all(),
                   "about": self.about, "quit": self.request_quit}
        for name, callback in actions.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _action, _param, callback=callback, name=name:
                           callback() if (isinstance(self.window, Window) and not self.window.closing) or
                           (self.window is not None and name in {"quit", "about"}) else None)
            self.add_action(action)
        show = Gio.SimpleAction.new("show-download", GLib.VariantType.new("s"))
        show.connect("activate", self.activate_download)
        self.add_action(show)
        folder = Gio.SimpleAction.new("open-completed-folder", GLib.VariantType.new("s"))
        folder.connect("activate", lambda _, value: self.open_completed_folder(value.get_string()))
        self.add_action(folder)
        self.set_accels_for_action("app.new-download", ["<Control>n"])
        self.set_accels_for_action("app.import-downloads", ["<Control>i"])
        self.set_accels_for_action("app.preferences", ["<Control>comma"])
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("app.show-downloads", ["<Control>1"])
        self.set_accels_for_action("app.show-history", ["<Control>2"])
        self.set_accels_for_action("app.search-history", ["<Control>f"])
        self.set_accels_for_action("app.open-download-folder", ["<Control><Shift>o"])
        self.set_accels_for_action("app.main-menu", ["F10"])

    def do_activate(self):
        if not self.window:
            if self.store is None:
                self.window = RecoveryWindow(self, self.directory)
                self.window.load()
            else:
                self.window = Window(self, self.store)
                if self.pending_download is None and self.pending_folder is None and not self.pending_urls:
                    self.window.offer_clipboard()
        self.window.present()
        self.show_pending_download()
        self.show_pending_folder()
        self.show_pending_urls()

    def do_open(self, files, count, hint):
        # External URLs enter review only. They never start network work on arrival.
        self.pending_urls.extend(file.get_uri() for file in files)
        self.activate()

    def show_pending_urls(self):
        if not self.pending_urls or not isinstance(self.window, Window) or self.window.closing:
            return
        for dialog in (self.window.download_dialog, self.window.batch_dialog):
            if dialog and not dialog.closed:
                if not getattr(dialog, "handoff_waiting", False):
                    dialog.handoff_waiting = True
                    dialog.connect("closed", lambda *_: GLib.idle_add(self.show_pending_urls))
                    self.window.toast.add_toast(Adw.Toast(title="Incoming URLs will open after the current dialog closes"))
                return
        values, self.pending_urls = self.pending_urls, []
        try:
            urls = list(dict.fromkeys(validate_url(value) for value in values))
            if len(urls) > 1000:
                raise ValueError("Open up to 1,000 URLs at a time.")
        except ValueError as error:
            self.window.toast.add_toast(Adw.Toast(title=str(error)))
            return
        if len(urls) == 1:
            self.window.new_download(urls[0])
        else:
            self.window.new_batch("\n".join(urls))

    def activate_download(self, _action, parameter):
        self.pending_download = parameter.get_string()
        self.activate()

    def open_completed_folder(self, folder):
        if not Path(folder).is_absolute():
            return
        self.pending_folder = folder
        self.activate()

    def show_pending_folder(self):
        if self.pending_folder is not None and isinstance(self.window, Window) and not self.window.closing:
            folder, self.pending_folder = self.pending_folder, None
            self.window.open_folder(folder)

    def show_pending_download(self):
        if self.pending_download is not None and isinstance(self.window, Window) and not self.window.closing:
            identifier, self.pending_download = self.pending_download, None
            self.window.show_download(identifier)

    def about(self):
        dialog = Adw.AboutDialog(application_name="Pyrobolic", application_icon=APP_ID,
                                 version=__version__, developer_name="Erick Woods",
                                 copyright="Copyright 2026 Erick Woods", license_type=Gtk.License.GPL_3_0_ONLY,
                                 comments="A native media downloader for GNOME.")
        dialog.present(self.window)


def main():
    import sys
    return Application().run(sys.argv)
