# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import warnings
from unittest.mock import patch
from types import SimpleNamespace

from pyrobolic.auth import (Authentication, AuthenticationError, EngineAuthentication, MemoryFile,
                           cookie_values, public_url, read_cookie_file, safe_auth_error, enqueue_authenticated,
                           CredentialVault, discard_created)
from pyrobolic.core import Job, Process, Queue, Store, download_arguments

COOKIES = "# Netscape HTTP Cookie File\n127.0.0.1\tFALSE\t/\tFALSE\t4102444800\tsession\tfixture-cookie-secret\n"


class FakeVault:
    def __init__(self):
        self.records = {}
        self.counter = 0

    def put(self, url, authentication, process=None):
        self.counter += 1
        identifier = f"{self.counter:032x}"
        self.records[identifier] = (url, authentication)
        return identifier

    def get(self, identifier, process=None):
        if identifier not in self.records:
            raise AuthenticationError("Authentication is no longer in the keyring.")
        return self.records[identifier]

    def forget(self, identifier, process=None):
        self.records.pop(identifier, None)


class AuthenticationTests(unittest.TestCase):
    def test_canceled_keyring_write_also_attempts_compensating_delete(self):
        vault = CredentialVault()
        with patch.object(vault, "_api", return_value=(SimpleNamespace(), None)), \
                patch.object(vault, "_call", side_effect=InterruptedError("Canceled")), \
                patch.object(vault, "forget") as forget:
            with self.assertRaises(InterruptedError):
                vault.put("https://example.org/fixture", Authentication(protect_url=True))
            self.assertEqual(forget.call_count, 1)
            self.assertEqual(len(forget.call_args.args[0]), 32)
            self.assertIsInstance(forget.call_args.args[1], Process)

    def test_rollback_keyring_wait_has_independent_cancellation_deadline(self):
        def blocked(_reference, process):
            self.assertTrue(process.cancelled.wait(2))
            raise InterruptedError("Canceled")
        with patch("pyrobolic.auth.CLEANUP_TIMEOUT", 0.02):
            discard_created(SimpleNamespace(forget=blocked), ["a" * 32, "b" * 32])

    def test_redacted_origins_do_not_serialize_distinct_authenticated_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            store.settings.concurrency = 3
            queue = Queue(store)
            store.jobs = [Job("https://example.org/", "Fixture", directory, filename="Fixture", metadata=False,
                              auth_ref=reference * 32) for reference in ("a", "b", "a")]
            with patch("pyrobolic.core.threading.Thread") as worker:
                queue._schedule()
            self.assertEqual(worker.call_count, 2)
            self.assertEqual([job.status for job in store.jobs], ["Downloading", "Downloading", "Queued"])

    def test_protected_submission_roundtrip_and_failed_save_compensation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            store.paused = True
            queue = Queue(store)
            vault = FakeVault()
            url = "https://example.org/path?token=fixture-query-secret"
            auth = Authentication(cookies=COOKIES)
            job = Job(url, "Fixture", directory, filename="Fixture")
            enqueue_authenticated(queue, [job], store.settings, auth, Process(), public_url(url), vault)
            self.assertEqual(store.jobs[0].url, "https://example.org/")
            self.assertFalse(store.jobs[0].metadata)
            self.assertEqual(len(store.settings.auth_profiles), 1)
            self.assertEqual(len(vault.records), 1)
            restored = Store(Path(directory))
            self.assertIn(restored.jobs[0].auth_ref, vault.records)
            self.assertNotIn("fixture-query-secret", store.path.read_text())
            self.assertNotIn("fixture-cookie-secret", store.path.read_text())
            with patch.object(store, "save", side_effect=OSError("full")), self.assertRaises(OSError):
                enqueue_authenticated(queue, [job], store.settings, auth, Process(), public_url(url), vault)
            self.assertEqual(len(vault.records), 1)
            self.assertEqual(len(store.jobs), 1)

    def test_cross_site_selection_and_canceled_submission_do_not_store_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            queue = Queue(store)
            vault = FakeVault()
            job = Job("https://other.example.org/path", "Fixture", directory, filename="Fixture")
            auth = Authentication(protect_url=True)
            with self.assertRaises(AuthenticationError):
                enqueue_authenticated(queue, [job], store.settings, auth, Process(), "https://example.org/", vault)
            process = Process()
            process.cancel()
            with self.assertRaises(InterruptedError):
                enqueue_authenticated(queue, [job], store.settings, auth, process, public_url(job.url), vault)
            self.assertEqual(vault.records, {})
            self.assertEqual(store.jobs, [])

    def test_values_validate_without_disclosing_secrets(self):
        auth = Authentication(cookies=COOKIES, username="fixture-user", password="fixture-password")
        auth.validate()
        self.assertNotIn("fixture-password", repr(auth))
        self.assertEqual(cookie_values(COOKIES), ["fixture-cookie-secret"])
        for options in ({"username": "user"}, {"password": "secret"}, {"password": "secret\n--exec bad"},
                        {"cookies": "not a cookie file"}, {"browser": "firefox"},
                        {"browser": "firefox", "profile": "relative"},
                        {"browser": "firefox", "profile": "/tmp/profile::another"},
                        {"cookies": COOKIES, "browser": "firefox", "profile": "/tmp/profile"}):
            with self.subTest(options=list(options)), self.assertRaises(AuthenticationError):
                Authentication(**options).validate()

    def test_explicit_file_import_bounded_and_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.txt"
            path.write_text(COOKIES)
            self.assertEqual(read_cookie_file(path), COOKIES)
            self.assertEqual(path.read_text(), COOKIES)
            path.write_text("x" * (2 * 1024 * 1024 + 1))
            with self.assertRaises(AuthenticationError):
                read_cookie_file(path)

    def test_malformed_cookie_values_are_not_printed_as_warnings(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with self.assertRaises(AuthenticationError) as error:
                cookie_values(COOKIES.replace("4102444800", "fixture-secret-expiry"))
        self.assertFalse(caught)
        self.assertNotIn("fixture-secret", str(error.exception))

    def test_cookie_import_refuses_fifo_without_waiting_for_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookie-pipe"
            os.mkfifo(path)
            with self.assertRaises(AuthenticationError):
                read_cookie_file(path)

    def test_config_has_no_plaintext_secret_in_argv_and_fds_close(self):
        url = "https://example.org/private/path?token=fixture-query-secret"
        auth = Authentication(username="fixture-user", password="fixture-password", video_password="fixture-video-secret")
        private = EngineAuthentication(url, auth)
        fds = private.fds[:]
        try:
            args = private.arguments(download_arguments(Job(public_url(url), "Fixture", "/tmp", metadata=False), "yt-dlp"))
            self.assertNotIn("fixture-", " ".join(args))
            self.assertNotIn(url, args)
            data = Path(private.config).read_text()
            self.assertIn("fixture-password", data)
            self.assertIn("--batch-file", data)
            self.assertIn("--no-cache-dir", data)
        finally:
            private.close()
        for fd in fds:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_secrets_and_signed_urls_are_redacted(self):
        auth = Authentication(cookies=COOKIES, username="fixture-user", password="fixture-password")
        raw = "fixture-cookie-secret fixture-password fixture-user https://example.org/a?token=fixture-query-secret"
        clean = auth.scrub(raw, "https://example.org/a?token=fixture-query-secret")
        self.assertNotIn("fixture-", clean)
        self.assertNotIn("token=", clean)
        self.assertNotIn("fixture-password", safe_auth_error(RuntimeError("403 fixture-password")))

    def test_cookie_redaction_parses_once_per_immutable_draft(self):
        auth = Authentication(cookies=COOKIES)
        with patch("pyrobolic.auth.cookie_values", wraps=cookie_values) as parse:
            auth.validate()
            for _ in range(100):
                self.assertNotIn("fixture-cookie-secret", auth.scrub("fixture-cookie-secret"))
            parse.assert_called_once()

    def test_state_and_queue_reject_plain_authenticated_url_or_metadata(self):
        reference = "a" * 32
        for options in ({"url": "https://example.org/secret"}, {"metadata": True}, {"filename": None}):
            values = dict(url="https://example.org/", title="Fixture", output="/tmp", filename="Fixture",
                          metadata=False, auth_ref=reference)
            values.update(options)
            with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
                Queue(Store(Path(directory))).add([Job(**values)])
        job = Job("https://example.org/", "Fixture", "/tmp", filename="Fixture", output_stem="Fixture",
                  metadata=False, auth_ref=reference)
        settings, _, jobs = Store.decode(json.dumps({"jobs": [asdict(job)]}))
        self.assertEqual(jobs[0].auth_ref, reference)

    def test_process_cancellation_reaches_keyring_cancellable(self):
        from gi.repository import Gio
        process = Process()
        cancel = Gio.Cancellable()
        process.attach_cancellable(cancel)
        process.cancel()
        self.assertTrue(cancel.is_cancelled())
        process.detach_cancellable(cancel)
        later = Gio.Cancellable()
        process.attach_cancellable(later)
        self.assertTrue(later.is_cancelled())
        process.detach_cancellable(later)

    def test_memory_input_reaches_only_explicitly_inherited_child(self):
        memory = MemoryFile("fixture-memory-secret")
        try:
            process = Process()
            child = process.start(["/usr/bin/python3", "-c", "import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text())", memory.path],
                                  pass_fds=[memory.fd])
            output, _ = child.communicate(timeout=10)
            process.close()
            self.assertEqual(output.strip(), "fixture-memory-secret")
            denied = subprocess.run(["/usr/bin/python3", "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).read_text()", memory.path],
                                    capture_output=True)
            self.assertNotEqual(denied.returncode, 0)
        finally:
            memory.close()
