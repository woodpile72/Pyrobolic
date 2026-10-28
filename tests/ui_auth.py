# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Authentication draft, submission and forgetting using synthetic data and a fake vault."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import traceback
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pyrobolic.app import Application, Adw, GLib, Gtk
from pyrobolic.auth import Authentication, AuthenticationError
from pyrobolic.auth_ui import AuthenticationManager
from pyrobolic.core import Store
from test_auth import FakeVault
import gi
gi.require_version("GdkX11", "4.0")
from gi.repository import GdkX11

temporary = tempfile.TemporaryDirectory()
store = Store(Path(temporary.name) / "state")
store.settings.output = str(Path(temporary.name) / "media")
store.settings.metadata = True
store.paused = True
app = Application(store)
state, failures = {}, []
class ControlledVault(FakeVault):
    blocked = False

    def put(self, url, authentication, process=None):
        if self.blocked:
            if process.cancelled.wait(10):
                raise InterruptedError("Canceled")
            raise AuthenticationError("Fixture unlock timed out")
        return super().put(url, authentication, process)

vault = ControlledVault()
hc = bool(os.environ.get("ADW_DEBUG_HIGH_CONTRAST"))
url = "https://example.org/private?token=fixture-query-secret"


def key(value):
    subprocess.run(["xdotool", "key", "--clearmodifiers", value], check=True)


def initial():
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT if hc else Adw.ColorScheme.FORCE_DARK)
    if hc:
        Gtk.Settings.get_default().set_property("gtk-font-name", "Sans 20")
    app.window.set_default_size(440, 850)
    app.window.queue.stopped = True
    parent = state["parent"] = app.window.new_download(url)
    parent.more.set_expanded(True)
    parent.url.set_text("")
    assert not parent.auth_button.get_sensitive()
    assert parent.edit_authentication() is None
    parent.url.set_text(url)
    assert Gtk.test_accessible_has_property(parent.auth_button, Gtk.AccessibleProperty.LABEL)


def open_authentication():
    parent = state["parent"]
    parent.auth_button.grab_focus()
    key("Return")


def draft():
    editor = state["editor"] = app.window.get_visible_dialog()
    editor.username.set_text("fixture-account")
    assert not editor.apply.get_sensitive()
    editor.password.set_text("fixture-password")
    editor.video_password.set_text("fixture-video-password")
    assert isinstance(editor.password, Adw.PasswordEntryRow)
    assert editor.apply.get_sensitive()
    assert not state["parent"].authentication.active
    assert not vault.records
    editor.password.grab_focus()


def screenshot():
    editor = state["editor"]
    for widget in (editor.password, editor.apply, editor.cancel):
        ok, bounds = widget.compute_bounds(app.window)
        assert ok and bounds.get_x() >= 0 and bounds.get_x() + bounds.get_width() <= app.window.get_width() + 1
    output = Path(__file__).resolve().parents[1] / ".qa"
    output.mkdir(exist_ok=True)
    subprocess.run(["import", "-window", str(GdkX11.X11Surface.get_xid(app.window.get_surface())),
                    str(output / ("auth-hc.png" if hc else "auth-dark.png"))], check=True)
    editor.cancel.grab_focus()
    key("Return")


def canceled():
    assert not state["parent"].authentication.active
    assert not state["editor"].password.get_text()
    assert not vault.records
    state["editor"] = state["parent"].edit_authentication()
    state["editor"].protect.set_active(True)


def apply_draft():
    state["editor"].apply.grab_focus()
    key("Return")


def changed_site():
    parent = state["parent"]
    assert parent.authentication.protect_url
    assert "Off (Authenticated Download)" in parent.quick_summary.get_text()
    assert not vault.records
    parent.url.set_text("https://other.example.org/item")
    assert not parent.authentication.active
    assert "Metadata: On" in parent.quick_summary.get_text()
    parent.url.set_text(url)
    editor = state["editor"] = parent.edit_authentication()
    editor.mode.set_selected(1)
    assert not editor.apply.get_sensitive()
    editor.mode.set_selected(2)
    assert not editor.apply.get_sensitive()
    editor.mode.set_selected(0)
    editor.username.set_text("fixture-account")
    editor.password.set_text("fixture-password")


def inspect_fixture():
    parent = state["parent"]
    assert parent.authentication.password == "fixture-password"
    parent.inspected([{"url": url, "title": "Authentication fixture", "media_id": "1",
                       "formats_loaded": True, "subtitle_tracks": []}], None)
    assert not parent.metadata.get_active()
    assert not parent.metadata.get_sensitive()
    assert not parent.auth_button.get_sensitive()
    vault.blocked = True
    parent.enqueue()


def cancel_save():
    parent = state["parent"]
    assert parent.saving
    parent.cancel.grab_focus()
    key("Return")


def save_canceled():
    parent = state["parent"]
    assert not parent.saving
    assert not store.jobs and not vault.records
    assert parent.message.get_text() == "Canceled"
    assert parent.cancel.get_sensitive() and parent.download.get_sensitive()
    vault.blocked = False
    parent.enqueue()


def submitted():
    assert state["parent"].closed
    assert len(vault.records) == 1
    assert len(store.jobs) == 1
    assert store.settings.metadata
    assert store.jobs[0].url == "https://example.org/"
    assert not store.jobs[0].metadata
    content = store.path.read_text()
    for secret in ("fixture-password", "fixture-query-secret", "fixture-account"):
        assert secret not in content
    assert Store(store.directory).jobs[0].auth_ref == store.jobs[0].auth_ref
    state["parent"] = app.window.new_download(url)
    editor = state["editor"] = state["parent"].edit_authentication()
    editor.stored.set_selected(1)


def loaded():
    assert state["editor"].password.get_text() == "fixture-password"
    state["editor"].cancel.grab_focus()
    key("Return")


def fail_lookup():
    reference = store.jobs[0].auth_ref
    state["record"] = vault.records.pop(reference)
    state["editor"] = state["parent"].edit_authentication()
    state["editor"].stored.set_selected(1)


def lookup_failed():
    editor = state["editor"]
    assert "no longer" in editor.error.get_text()
    assert not editor.password.get_text()
    vault.records[store.jobs[0].auth_ref] = ("https://other.example.org/", Authentication(protect_url=True))
    editor.stored.set_selected(0)
    editor.stored.set_selected(1)


def wrong_site():
    editor = state["editor"]
    assert "another site" in editor.error.get_text()
    assert not editor.protect.get_active()
    vault.records[store.jobs[0].auth_ref] = state["record"]
    editor.close()


def close_parent():
    state["parent"].close()


def manage():
    manager = state["manager"] = AuthenticationManager(app.window)
    manager.present(app.window)
    dialog = manager.confirm_forget(store.jobs[0].auth_ref)
    assert dialog.get_default_response() == "cancel"
    key("Escape")


def forget():
    assert len(vault.records) == 1
    state["manager"].forget(store.jobs[0].auth_ref)


def stop_then_forget():
    assert "Stop downloads" in state["manager"].error.get_text()
    assert len(vault.records) == 1
    app.window.queue.cancel(store.jobs[0].id)
    state["manager"].forget(store.jobs[0].auth_ref)


def finish():
    assert not state["manager"].error.get_text(), state["manager"].error.get_text()
    assert not vault.records
    assert not store.settings.auth_profiles
    assert not Store(store.directory).settings.auth_profiles
    assert store.jobs[0].auth_ref
    app.window.queue.shutdown()
    app.window.actions.shutdown(wait=True)
    app.quit()


steps = iter([initial, open_authentication, draft, screenshot, canceled, apply_draft, changed_site, apply_draft,
              inspect_fixture, cancel_save, save_canceled, submitted, loaded,
              fail_lookup, lookup_failed, wrong_site, close_parent, manage, forget, stop_then_forget, finish])
def advance():
    try:
        next(steps)()
    except StopIteration:
        return False
    except Exception:
        failures.append(traceback.format_exc())
        app.quit()
        return False
    return True

GLib.timeout_add(950, advance)
with patch("pyrobolic.auth.CredentialVault", return_value=vault), patch("pyrobolic.auth_ui.CredentialVault", return_value=vault):
    app.run([])
temporary.cleanup()
if failures:
    print("\n".join(failures))
    raise SystemExit(1)
print("Authentication UI checks passed")
