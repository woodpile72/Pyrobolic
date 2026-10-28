# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Explicit authentication, Secret Service storage and Linux memory-only engine input."""
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from functools import cached_property
import http.cookiejar
import json
import os
from pathlib import Path
import re
import shlex
import stat
import threading
from urllib.parse import parse_qsl, urlsplit, urlunsplit
import uuid
import warnings

MAX_COOKIE_BYTES = 2 * 1024 * 1024
CLEANUP_TIMEOUT = 5
BROWSERS = ("firefox", "chromium", "chrome", "brave", "edge", "vivaldi", "opera")


class AuthenticationError(RuntimeError):
    """Messages are application-owned and never contain engine output or secret values."""


def public_url(url):
    from .core import validate_url
    parts = urlsplit(validate_url(url))
    return urlunsplit((parts.scheme, parts.netloc, "/", "", ""))


def validate_ref(value):
    if not isinstance(value, str) or (value and not re.fullmatch(r"[a-f0-9]{32}", value)):
        raise ValueError("Invalid authentication reference")


class MemoryFile:
    def __init__(self, data):
        if not hasattr(os, "memfd_create"):
            raise AuthenticationError("Authenticated downloads require Linux memory-file support.")
        self.fd = os.memfd_create("pyrobolic-private", os.MFD_CLOEXEC)
        try:
            os.fchmod(self.fd, 0o600)
            with os.fdopen(os.dup(self.fd), "wb") as stream:
                stream.write(data.encode("utf-8"))
            os.lseek(self.fd, 0, os.SEEK_SET)
        except BaseException:
            self.close()
            raise

    @property
    def path(self):
        return f"/proc/self/fd/{self.fd}"

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def cookie_values(text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_COOKIE_BYTES:
        raise AuthenticationError("The cookie file is too large (maximum 2 MiB).")
    memory = MemoryFile(text)
    try:
        jar = http.cookiejar.MozillaCookieJar(memory.path)
        try:
            # Malformed fields can otherwise make cookiejar print secret-bearing tracebacks.
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message="http.cookiejar bug!", category=UserWarning)
                jar.load(ignore_discard=True, ignore_expires=True)
        except Exception:
            raise AuthenticationError("Choose a valid Netscape-format cookie file.") from None
        if not jar:
            raise AuthenticationError("The cookie file contains no cookies.")
        return [cookie.value for cookie in jar if cookie.value]
    finally:
        memory.close()


def read_cookie_file(path):
    try:
        # Refuse pipes/devices without blocking, while allowing explicitly chosen file symlinks.
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise AuthenticationError("Choose a regular cookie file.")
            data = stream.read(MAX_COOKIE_BYTES + 1)
        if len(data) > MAX_COOKIE_BYTES:
            raise AuthenticationError("The cookie file is too large (maximum 2 MiB).")
        text = data.decode("utf-8-sig")
        cookie_values(text)
        return text
    except AuthenticationError:
        raise
    except Exception:
        raise AuthenticationError("The selected cookie file could not be read.") from None


@dataclass(repr=False, frozen=True)
class Authentication:
    protect_url: bool = False
    cookies: str = ""
    browser: str = ""
    profile: str = ""
    username: str = ""
    password: str = ""
    video_password: str = ""

    @property
    def active(self):
        return bool(self.protect_url or self.cookies or self.browser or self.username or self.password or self.video_password)

    def validate(self):
        if type(self.protect_url) is not bool or not isinstance(self.cookies, str):
            raise AuthenticationError("Invalid authentication options.")
        for value in (self.browser, self.profile, self.username, self.password, self.video_password):
            if not isinstance(value, str) or len(value) > 4096 or any(c in value for c in "\r\n\x00"):
                raise AuthenticationError("Authentication fields must be single-line text of at most 4096 characters.")
        if self.cookies:
            _ = self._cookie_values
        if self.cookies and self.browser:
            raise AuthenticationError("Choose a cookie file or a browser profile, not both.")
        if self.browser:
            if self.browser not in BROWSERS or not Path(self.profile).is_absolute() or ":" in self.profile:
                raise AuthenticationError("Choose a supported browser and an explicit absolute profile folder.")
        elif self.profile:
            raise AuthenticationError("Choose a browser for this profile folder.")
        if bool(self.username) != bool(self.password):
            raise AuthenticationError("Enter both the account name and password.")

    @cached_property
    def _cookie_values(self):
        return tuple(cookie_values(self.cookies)) if self.cookies else ()

    def scrub(self, text, url=""):
        values = [self.username, self.password, self.video_password]
        if self.cookies:
            values += self._cookie_values
        if url:
            values += [value for _, value in parse_qsl(urlsplit(url).query) if value]
        for value in sorted(set(values) - {""}, key=len, reverse=True):
            text = text.replace(value, "[redacted]")
        return re.sub(r"https?://[^\s<>\"']+", "[private source]", text)


def validate_profiles(profiles):
    if not isinstance(profiles, dict):
        raise ValueError("Invalid authentication index")
    for identifier, label in profiles.items():
        validate_ref(identifier)
        if not identifier or not isinstance(label, str) or len(label) > 300 or any(c in label for c in "\r\n\0"):
            raise ValueError("Invalid saved authentication label")


class CredentialVault:
    def _api(self):
        try:
            import gi
            gi.require_version("Secret", "1")
            from gi.repository import Secret
            schema = Secret.Schema.new("io.github.woodpile72.Pyrobolic.Authentication", Secret.SchemaFlags.NONE,
                                       {"id": Secret.SchemaAttributeType.STRING})
            return Secret, schema
        except Exception:
            raise AuthenticationError("Desktop keyring support is missing. Install gir1.2-secret-1 and a Secret Service provider.") from None

    def _call(self, operation, process=None):
        from gi.repository import Gio
        cancellable = Gio.Cancellable()
        if process is not None:
            process.attach_cancellable(cancellable)
        try:
            result = operation(cancellable)
            if cancellable.is_cancelled():
                raise InterruptedError("Canceled")
            return result
        except InterruptedError:
            raise
        except Exception:
            if cancellable.is_cancelled():
                raise InterruptedError("Canceled") from None
            raise AuthenticationError("The desktop keyring is unavailable, locked, or access was canceled. Unlock it and try again.") from None
        finally:
            if process is not None:
                process.detach_cancellable(cancellable)

    def put(self, url, authentication, process=None):
        authentication.validate()
        public_url(url)
        api, schema = self._api()
        identifier = uuid.uuid4().hex
        payload = json.dumps({"url": url, "authentication": asdict(authentication)})
        try:
            okay = self._call(lambda cancel: api.password_store_sync(schema, {"id": identifier}, api.COLLECTION_DEFAULT,
                             "Pyrobolic download authentication", payload, cancel), process)
        except Exception:
            # Cancellation can arrive after the service saved the item but before it replied.
            discard_created(self, [identifier])
            raise
        if not okay:
            raise AuthenticationError("The desktop keyring did not save authentication. Nothing was queued.")
        return identifier

    def get(self, identifier, process=None):
        validate_ref(identifier)
        api, schema = self._api()
        payload = self._call(lambda cancel: api.password_lookup_sync(schema, {"id": identifier}, cancel), process)
        if payload is None:
            raise AuthenticationError("Authentication is no longer in the keyring. Start a new download with fresh authentication.")
        try:
            data = json.loads(payload)
            authentication = Authentication(**data["authentication"])
            authentication.validate()
            public_url(data["url"])
            return data["url"], authentication
        except Exception:
            raise AuthenticationError("The saved authentication is unreadable. Start a new download with fresh authentication.") from None

    def forget(self, identifier, process=None):
        validate_ref(identifier)
        api, schema = self._api()
        self._call(lambda cancel: api.password_clear_sync(schema, {"id": identifier}, cancel), process)


class EngineAuthentication:
    def __init__(self, url, authentication):
        authentication.validate()
        public_url(url)
        self.stack = ExitStack()
        self.fds = []
        try:
            def memory(text):
                item = MemoryFile(text)
                self.stack.callback(item.close)
                self.fds.append(item.fd)
                return item.path
            options = ["--no-cache-dir", "--no-embed-metadata"]
            if authentication.cookies:
                options += ["--cookies", memory(authentication.cookies)]
            if authentication.browser:
                options += ["--cookies-from-browser", authentication.browser + ":" + authentication.profile]
            for field, flag in (("username", "--username"), ("password", "--password"), ("video_password", "--video-password")):
                if value := getattr(authentication, field):
                    options += [flag, value]
            options += ["--batch-file", memory(url + "\n")]
            self.config = memory("# coding: utf-8\n" + shlex.join(options) + "\n")
        except BaseException:
            self.close()
            raise

    def arguments(self, args):
        if len(args) < 2 or args[-2] != "--":
            raise ValueError("Expected a single validated URL argument")
        # Explicit config locations remain honored with --ignore-config; no other configs are read.
        return args[:-2] + ["--config-locations", self.config]

    def close(self):
        self.stack.close()


def safe_auth_error(error):
    if isinstance(error, AuthenticationError):
        return str(error)
    if isinstance(error, InterruptedError):
        return "Canceled"
    text = str(error).casefold()
    if "format" in text and ("not available" in text or "unavailable" in text):
        return "The requested format is unavailable. Authentication did not change the exact-stream requirement."
    if any(word in text for word in ("401", "403", "login", "sign in", "cookie", "password")):
        return "Authentication was not accepted. Refresh the login or cookies and start a new download."
    if "timed out" in text or "timeout" in text:
        return "The authenticated request timed out. Retry or check the connection."
    return "The authenticated operation failed. Check access, output options and the destination, then retry. Raw engine output was withheld."


class CredentialOperation:
    """Keep credential work visible to application shutdown, even before a child exists."""
    def __init__(self):
        from .core import Process
        self.process = Process()
        self.done = threading.Event()

    def cancel(self):
        self.process.cancel()

    def close(self):
        self.done.wait()


def discard_created(vault, references):
    """Best-effort rollback must not start another unbounded keyring prompt after cancellation."""
    if not references:
        return
    from .core import Process
    process = Process()
    deadline = threading.Timer(CLEANUP_TIMEOUT, process.cancel)
    deadline.daemon = True
    deadline.start()
    try:
        for reference in references:
            if process.cancelled.is_set():
                break
            try:
                vault.forget(reference, process)
            except Exception:
                pass
    finally:
        deadline.cancel()
        deadline.join()


def enqueue_authenticated(queue, jobs, settings, authentication, process, origin, vault=None):
    """Persist only opaque references; compensate for failed multi-job submission."""
    from dataclasses import replace
    vault = vault or CredentialVault()
    authentication.validate()
    if any(public_url(job.url) != origin for job in jobs):
        raise AuthenticationError("This selection includes another site. Download those items separately with explicit authentication.")
    created, protected = [], []
    profiles = dict(settings.auth_profiles)
    try:
        for job in jobs:
            if process.cancelled.is_set():
                raise InterruptedError("Canceled")
            reference = vault.put(job.url, authentication, process)
            created.append(reference)
            profiles[reference] = urlsplit(job.url).netloc
            protected.append(replace(job, url=public_url(job.url), auth_ref=reference, metadata=False,
                                     title=authentication.scrub(job.title, job.url),
                                     filename=authentication.scrub(job.filename, job.url),
                                     media_id=authentication.scrub(job.media_id, job.url)))
        if process.cancelled.is_set():
            raise InterruptedError("Canceled")
        queue.add(protected, replace(settings, auth_profiles=profiles))
    except Exception:
        discard_created(vault, created)
        raise
