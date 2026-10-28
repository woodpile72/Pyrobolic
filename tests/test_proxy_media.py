# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""All traffic goes to a synthetic loopback proxy; no remote source is contacted."""
from functools import partial
from http.server import ThreadingHTTPServer
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

from pyrobolic.auth import Authentication
from pyrobolic.batch import BatchInspection, parse_batch
from pyrobolic.core import Job, Process, Queue, Settings, Store, inspect_url
from test_core import await_idle
from test_integration import QuietHandler


@unittest.skipUnless(os.environ.get("PYROBOLIC_INTEGRATION") == "1", "Set PYROBOLIC_INTEGRATION=1")
class ProxyMediaTests(unittest.TestCase):
    def test_direct_connection_ignores_environment_proxy_for_inspection_batch_and_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fixture.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24",
                            "-t", "1", "-c:v", "libx264", str(source)], check=True)
            requests = []
            class RejectProxy(QuietHandler):
                def do_GET(self):
                    requests.append(self.path)
                    self.send_error(403)
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=directory))
            proxy = ThreadingHTTPServer(("127.0.0.1", 0), RejectProxy)
            for service in (server, proxy):
                threading.Thread(target=service.serve_forever, daemon=True).start()
            url = f"http://127.0.0.1:{server.server_port}/fixture.mp4"
            proxy_url = f"http://127.0.0.1:{proxy.server_port}"
            environment = {key: proxy_url for key in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
            environment.update(no_proxy="", NO_PROXY="")
            queue = Queue(Store(root / "state"))
            try:
                with patch.dict(os.environ, environment):
                    with self.assertRaises(RuntimeError):
                        inspect_url(url, Process(), root / "state")
                    self.assertTrue(requests, "Fixture must prove the default path uses the proxy")
                    requests.clear()
                    settings = Settings(direct_connection=True)
                    self.assertEqual(len(inspect_url(url, Process(), root / "state", transfer=settings)), 1)
                    self.assertEqual(len(inspect_url(url, Process(), root / "state", transfer=settings,
                                                    authentication=Authentication(protect_url=True))), 1)
                    entries = parse_batch(url)
                    BatchInspection().run(entries, root / "state", transfer=settings)
                    self.assertTrue(entries[0].inspected)
                    self.assertFalse(entries[0].error, entries[0].error)
                    job = Job(url, "Direct fixture", str(root / "output"), filename="Direct fixture",
                              metadata=False, direct_connection=True)
                    queue.add([job])
                    await_idle(queue, 30)
                    self.assertEqual(job.status, "Completed", job.log)
                    self.assertFalse(requests)
                    self.assertEqual(Path(job.files[0]).read_bytes(), source.read_bytes())
            finally:
                queue.shutdown()
                await_idle(queue)
                for service in (server, proxy):
                    service.shutdown()
                    service.server_close()

    def test_inspection_batch_authentication_and_download_use_explicit_proxy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fixture.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24",
                            "-t", "1", "-c:v", "libx264", str(source)], check=True)
            requests = []
            class Proxy(QuietHandler):
                def send_head(self):
                    requests.append(self.path)
                    parts = urlsplit(self.path)
                    if parts.scheme != "http" or parts.netloc != "fixture.pyrobolic.invalid":
                        self.send_error(403)
                        return None
                    self.path = parts.path
                    return super().send_head()
            proxy = ThreadingHTTPServer(("127.0.0.1", 0), partial(Proxy, directory=directory))
            threading.Thread(target=proxy.serve_forever, daemon=True).start()
            url = "http://fixture.pyrobolic.invalid/fixture.mp4"
            settings = Settings(proxy=f"http://127.0.0.1:{proxy.server_port}")
            queue = Queue(Store(root / "state"))
            try:
                self.assertEqual(len(inspect_url(url, Process(), root / "state", transfer=settings)), 1)
                self.assertEqual(len(inspect_url(url, Process(), root / "state", transfer=settings,
                                                authentication=Authentication(protect_url=True))), 1)
                entries = parse_batch(url)
                BatchInspection().run(entries, root / "state", transfer=settings)
                self.assertTrue(entries[0].inspected)
                self.assertFalse(entries[0].error, entries[0].error)
                job = Job(url, "Proxy fixture", str(root / "output"), filename="Proxy fixture",
                          metadata=False, proxy=settings.proxy)
                queue.add([job])
                await_idle(queue, 30)
                self.assertEqual(job.status, "Completed", job.log)
                self.assertGreaterEqual(len(requests), 4)
                self.assertEqual(hashlib.sha256(Path(job.files[0]).read_bytes()).digest(),
                                 hashlib.sha256(source.read_bytes()).digest())
            finally:
                queue.shutdown()
                await_idle(queue)
                proxy.shutdown()
                proxy.server_close()
