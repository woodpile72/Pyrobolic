# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
from functools import partial
from http.server import ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from pyrobolic.auth import Authentication, public_url
from pyrobolic.core import Job, Process, Queue, Store, inspect_url
from test_auth import COOKIES
from test_core import await_idle
from test_integration import QuietHandler


class CookieHandler(QuietHandler):
    def do_GET(self):
        if "session=fixture-cookie-secret" not in self.headers.get("Cookie", ""):
            self.send_error(403, "Fixture authentication required")
            return
        super().do_GET()


@unittest.skipUnless(os.environ.get("PYROBOLIC_INTEGRATION") == "1", "Set PYROBOLIC_INTEGRATION=1")
class AuthMediaTests(unittest.TestCase):
    def test_authenticated_inspection_download_retry_and_redaction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fixture.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24",
                            "-t", "1", "-c:v", "libx264", str(source)], check=True)
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(CookieHandler, directory=directory))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/fixture.mp4?token=fixture-query-secret"
                auth = Authentication(cookies=COOKIES)
                with self.assertRaises(RuntimeError):
                    inspect_url(url, Process(), root / "state")
                items = inspect_url(url, Process(), root / "state", authentication=auth)
                self.assertEqual(len(items), 1)
                profile = root / "explicit-firefox-profile"
                profile.mkdir()
                database = profile / "cookies.sqlite"
                with sqlite3.connect(database) as connection:
                    connection.execute("CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT, path TEXT, expiry INTEGER, isSecure INTEGER)")
                    connection.execute("INSERT INTO moz_cookies VALUES (?, ?, ?, ?, ?, ?)",
                                       ("127.0.0.1", "session", "fixture-cookie-secret", "/", 4102444800, 0))
                original_database = database.read_bytes()
                browser_auth = Authentication(browser="firefox", profile=str(profile))
                browser_items = inspect_url(url, Process(), root / "state", authentication=browser_auth)
                self.assertEqual(len(browser_items), 1)
                self.assertEqual(database.read_bytes(), original_database)
                missing_profile = Authentication(browser="firefox", profile=str(root / "missing-profile"))
                with self.assertRaises(RuntimeError):
                    inspect_url(url, Process(), root / "state", authentication=missing_profile)
                store = Store(root / "state")
                queue = Queue(store)
                job = Job(public_url(url), "Authenticated fixture", str(root / "media"), filename="Fixture",
                          metadata=False, auth_ref="a" * 32)
                with patch("pyrobolic.auth.CredentialVault.get", return_value=(url, auth)):
                    queue.add([job])
                    await_idle(queue, 45)
                self.assertEqual(job.status, "Completed", job.log)
                self.assertEqual(job.log, [])
                def digest(path):
                    return subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v", "-c", "copy", "-f", "hash", "-"])
                self.assertEqual(digest(job.files[0]), digest(source))
                self.assertNotIn("fixture-query-secret", store.path.read_text())
                self.assertNotIn("fixture-cookie-secret", store.path.read_text())
                bad = Authentication(cookies=COOKIES.replace("fixture-cookie-secret", "fixture-revoked-secret"))
                with patch("pyrobolic.auth.CredentialVault.get", return_value=(url, bad)):
                    failed_id = queue.redownload(job.id)
                    await_idle(queue, 45)
                failed = store.jobs[-1]
                self.assertEqual(failed.status, "Failed")
                self.assertFalse(failed.files)
                self.assertNotIn("fixture-query-secret", store.path.read_text())
                self.assertNotIn("fixture-revoked-secret", store.path.read_text())
                with patch("pyrobolic.auth.CredentialVault.get", return_value=(url, auth)):
                    queue.retry(failed_id)
                    await_idle(queue, 45)
                self.assertEqual(failed.status, "Completed", failed.log)
                self.assertEqual(digest(failed.files[0]), digest(source))
            finally:
                server.shutdown()
                server.server_close()
