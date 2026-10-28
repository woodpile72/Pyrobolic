# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
import threading
from urllib.parse import urlsplit

from gi.repository import Adw, Gio, GLib, Gtk

from .auth import (Authentication, AuthenticationError, BROWSERS, CredentialOperation, CredentialVault,
                   public_url, read_cookie_file, safe_auth_error)


class AuthenticationDialog(Adw.Dialog):
    def __init__(self, parent):
        from .app import button, combo, label, margins, scrolled
        super().__init__(title="Authentication", content_width=520, content_height=600)
        self.parent = parent
        self.closed = False
        self.operation = None
        self.cookies = parent.authentication.cookies
        self.profile = parent.authentication.profile
        self.origin = public_url(parent.url.get_text())
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
        self.page = margins(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12))
        self.page.append(label(urlsplit(self.origin).netloc, css="heading", wrap=True))
        saved = Adw.PreferencesGroup(title="Desktop Keyring")
        self.page.append(saved)
        self.references = [ref for ref, host in parent.window.store.settings.auth_profiles.items()
                           if host == urlsplit(self.origin).netloc]
        self.stored = combo("Saved Authentication", ["None"] + [f"{urlsplit(self.origin).netloc} ({ref[:8]})" for ref in self.references])
        self.stored.set_use_subtitle(True)
        self.stored.update_property([Gtk.AccessibleProperty.LABEL], ["Saved Authentication"])
        self.stored.connect("notify::selected", self.load_saved)
        saved.add(self.stored)
        group = Adw.PreferencesGroup(title="Cookies")
        self.page.append(group)
        self.mode = combo("Cookie Source", ["None", "Cookie File", "Browser Profile"])
        self.mode.set_selected(1 if self.cookies else 2 if parent.authentication.browser else 0)
        self.mode.connect("notify::selected", self.changed)
        group.add(self.mode)
        self.cookie_row = Adw.ActionRow(title="Cookie File", subtitle="Imported" if self.cookies else "Not selected")
        self.cookie_button = button("document-open-symbolic", "Choose Cookie File", self.choose_cookie)
        self.cookie_row.add_suffix(self.cookie_button)
        self.cookie_row.set_activatable_widget(self.cookie_button)
        group.add(self.cookie_row)
        self.browser = combo("Browser", ["Firefox", "Chromium", "Chrome", "Brave", "Edge", "Vivaldi", "Opera"])
        self.browser.set_selected(BROWSERS.index(parent.authentication.browser) if parent.authentication.browser else 0)
        self.browser.connect("notify::selected", self.changed)
        group.add(self.browser)
        self.profile_row = Adw.ActionRow(title="Profile Folder", subtitle="Selected" if self.profile else "Not selected")
        self.profile_button = button("folder-open-symbolic", "Choose Browser Profile Folder", self.choose_profile)
        self.profile_row.add_suffix(self.profile_button)
        self.profile_row.set_activatable_widget(self.profile_button)
        group.add(self.profile_row)
        credentials = Adw.PreferencesGroup(title="Site Credentials")
        self.page.append(credentials)
        self.username = Adw.EntryRow(title="Account Name", text=parent.authentication.username)
        self.password = Adw.PasswordEntryRow(title="Account Password", text=parent.authentication.password)
        self.video_password = Adw.PasswordEntryRow(title="Video Password", text=parent.authentication.video_password)
        for row in (self.username, self.password, self.video_password):
            row.update_property([Gtk.AccessibleProperty.LABEL], [row.get_title()])
            row.connect("changed", self.changed)
            credentials.add(row)
        self.protect = Adw.SwitchRow(title="Protect Source URL", active=parent.authentication.protect_url)
        self.protect.connect("notify::active", self.changed)
        saved.add(self.protect)
        for row, title in ((self.mode, "Cookie Source"), (self.browser, "Browser"), (self.protect, "Protect Source URL")):
            row.update_property([Gtk.AccessibleProperty.LABEL], [title])
        self.page.append(label("Authentication and full source URLs are saved in the desktop keyring when submitted. "
                               "History keeps only the site address. Source metadata and raw engine logs are disabled. "
                               "Browser cookies are read only from the profile you select; the browser keyring may ask to unlock. "
                               "Account login support depends on the site.", wrap=True, max_width_chars=40))
        self.error = label(wrap=True, css="error", max_width_chars=40)
        self.page.append(self.error)
        toolbar.set_content(scrolled(self.page))
        self.set_child(toolbar)
        self.connect("closed", self.on_closed)
        self.set_focus(self.mode)
        self.changed()

    def draft(self):
        mode = self.mode.get_selected()
        return Authentication(protect_url=self.protect.get_active(), cookies=self.cookies if mode == 1 else "",
                              browser=BROWSERS[self.browser.get_selected()] if mode == 2 else "",
                              profile=self.profile if mode == 2 else "", username=self.username.get_text(),
                              password=self.password.get_text(), video_password=self.video_password.get_text())

    def changed(self, *_):
        if not hasattr(self, "error"):
            return
        mode = self.mode.get_selected()
        self.cookie_row.set_visible(mode == 1)
        self.browser.set_visible(mode == 2)
        self.profile_row.set_visible(mode == 2)
        try:
            if mode == 1 and not self.cookies:
                raise ValueError("Choose a cookie file.")
            # Imported files were validated in a worker. Do not reparse them on every keypress.
            auth = self.draft()
            Authentication(protect_url=auth.protect_url, browser=auth.browser, profile=auth.profile,
                           username=auth.username, password=auth.password, video_password=auth.video_password).validate()
            error = ""
        except ValueError as problem:
            error = str(problem)
        except RuntimeError as problem:
            error = str(problem)
        self.error.set_text(error)
        self.apply.set_sensitive(not error and self.operation is None)

    def work(self, operation, finished):
        if self.operation is not None:
            return
        task = self.operation = CredentialOperation()
        self.parent.window.processes.add(task)
        self.page.set_sensitive(False)
        self.apply.set_sensitive(False)
        def worker():
            try:
                result, error = operation(task.process), None
            except Exception as problem:
                result, error = None, safe_auth_error(problem)
            finally:
                task.done.set()
            GLib.idle_add(done, result, error)
        def done(result, error):
            self.parent.window.processes.discard(task)
            self.operation = None
            if self.closed or self.parent.closed:
                return
            self.page.set_sensitive(True)
            if error:
                self.changed()
                self.error.set_text(error)
            else:
                finished(result)
                self.changed()
        threading.Thread(target=worker, daemon=True).start()

    def load_saved(self, *_):
        index = self.stored.get_selected()
        if 0 < index <= len(self.references):
            def load(process):
                result = CredentialVault().get(self.references[index - 1], process)
                if public_url(result[0]) != self.origin:
                    raise AuthenticationError("This keyring entry belongs to another site. Enter authentication explicitly.")
                return result
            self.work(load, self.loaded)

    def loaded(self, result):
        _url, auth = result
        self.cookies, self.profile = auth.cookies, auth.profile
        self.username.set_text(auth.username)
        self.password.set_text(auth.password)
        self.video_password.set_text(auth.video_password)
        self.protect.set_active(auth.protect_url)
        self.browser.set_selected(BROWSERS.index(auth.browser) if auth.browser else 0)
        self.mode.set_selected(1 if auth.cookies else 2 if auth.browser else 0)
        self.cookie_row.set_subtitle("Imported" if auth.cookies else "Not selected")
        self.profile_row.set_subtitle("Selected" if auth.profile else "Not selected")

    def choose_cookie(self):
        chooser = Gtk.FileDialog(title="Choose Netscape Cookie File")
        def selected(chooser, result):
            try:
                path = chooser.open_finish(result).get_path()
                if path and not self.closed:
                    self.work(lambda _process: read_cookie_file(path), self.cookie_loaded)
            except GLib.Error:
                pass
        chooser.open(self.parent.window, None, selected)

    def cookie_loaded(self, text):
        self.cookies = text
        self.cookie_row.set_subtitle("Imported")

    def choose_profile(self):
        chooser = Gtk.FileDialog(title="Choose Browser Profile Folder")
        def selected(chooser, result):
            try:
                path = chooser.select_folder_finish(result).get_path()
                if path and not self.closed:
                    self.profile = path
                    self.profile_row.set_subtitle("Selected")
                    self.changed()
            except GLib.Error:
                pass
        chooser.select_folder(self.parent.window, None, selected)

    def save(self, *_):
        if self.apply.get_sensitive() and not self.parent.closed:
            self.parent.authentication = self.draft()
            self.parent.auth_origin = self.origin
            self.parent.url_changed()
            self.close()

    def on_closed(self, *_):
        self.closed = True
        if self.operation:
            self.operation.cancel()
        self.cookies = ""
        self.password.set_text("")
        self.video_password.set_text("")


class AuthenticationManager(Adw.Dialog):
    def __init__(self, window):
        from .app import label, margins, scrolled
        super().__init__(title="Saved Authentication", content_width=520, content_height=480)
        self.window = window
        self.limit = 100
        self.busy = False
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        box = margins(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12))
        box.append(label("Saved keyring entries can be reused for the same site. Forgetting an entry also disables retries for downloads that use it. Media and browser profiles are kept.", wrap=True, max_width_chars=40))
        self.group = Adw.PreferencesGroup()
        box.append(self.group)
        self.rows = []
        self.empty = label("No Saved Authentication", css="dim-label")
        box.append(self.empty)
        self.more = Gtk.Button(label="Load More", halign=Gtk.Align.CENTER)
        self.more.connect("clicked", self.load_more)
        box.append(self.more)
        self.error = label(wrap=True, css="error", max_width_chars=40)
        box.append(self.error)
        toolbar.set_content(scrolled(box))
        self.set_child(toolbar)
        self.refresh()

    def load_more(self, *_):
        self.limit += 100
        self.refresh()

    def refresh(self):
        from .app import action_row, button
        for row in self.rows:
            self.group.remove(row)
        self.rows = []
        profiles = list(self.window.store.settings.auth_profiles.items())
        self.empty.set_visible(not profiles)
        self.more.set_visible(len(profiles) > self.limit)
        for reference, host in profiles[:self.limit]:
            row = action_row(host, reference[:8])
            remove = button("edit-delete-symbolic", "Forget Authentication", lambda ref=reference: self.confirm_forget(ref))
            row.add_suffix(remove)
            row.set_activatable_widget(remove)
            self.group.add(row)
            self.rows.append(row)

    def confirm_forget(self, reference):
        if self.busy:
            return
        dialog = Adw.AlertDialog(heading="Forget Authentication?",
                                 body="Delete this entry from the desktop keyring. Related downloads will need fresh authentication to run again. Browser profiles and downloaded files are not changed.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("forget", "Forget")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("forget", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda _dialog, response: self.forget(reference) if response == "forget" else None)
        dialog.present(self)
        return dialog

    def forget(self, reference):
        if self.busy:
            return
        from dataclasses import replace
        from .auth import AuthenticationError
        from .core import ACTIVE
        self.busy = True
        self.group.set_sensitive(False)
        self.set_can_close(False)
        task = CredentialOperation()
        self.window.processes.add(task)
        def work():
            try:
                with self.window.queue.lock:
                    if any(job.auth_ref == reference and (job.status in ACTIVE or job.id in self.window.queue.running)
                           for job in self.window.store.jobs):
                        raise AuthenticationError("Stop downloads using this authentication before forgetting it.")
                CredentialVault().forget(reference, task.process)
                profiles = dict(self.window.store.settings.auth_profiles)
                profiles.pop(reference, None)
                self.window.queue.configure(replace(self.window.store.settings, auth_profiles=profiles))
            except Exception as problem:
                raise RuntimeError(safe_auth_error(problem)) from None
            finally:
                task.done.set()
        def done(error):
            self.window.processes.discard(task)
            self.busy = False
            self.group.set_sensitive(True)
            self.set_can_close(True)
            self.error.set_text(error or "")
            self.refresh()
        self.window.run_action(work, done)
