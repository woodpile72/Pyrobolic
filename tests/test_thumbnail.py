# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
from io import BytesIO
import threading
import unittest
from unittest.mock import Mock, patch

from pyrobolic.core import Settings
from pyrobolic.thumbnail import fetch_thumbnail


class ThumbnailTests(unittest.TestCase):
    def test_bounded_success_and_oversize(self):
        for payload, expected in ((b"image", b"image"), (b"x" * (2 * 1024 * 1024 + 1), None)):
            opener = Mock()
            opener.open.return_value = BytesIO(payload)
            with patch("pyrobolic.thumbnail.build_opener", return_value=opener):
                self.assertEqual(fetch_thumbnail("https://example.org/image", Settings(), threading.Event()), expected)
            self.assertEqual(opener.open.call_args.kwargs["timeout"], 3)

    def test_canceled_or_socks_never_fetches(self):
        canceled = threading.Event()
        canceled.set()
        with patch("pyrobolic.thumbnail.build_opener") as opener:
            self.assertIsNone(fetch_thumbnail("https://example.org/image", Settings(), canceled))
            opener.return_value.open.assert_not_called()
        with patch("pyrobolic.thumbnail.build_opener") as opener:
            self.assertIsNone(fetch_thumbnail("https://example.org/image", Settings(proxy="socks5://localhost:1080"), threading.Event()))
            opener.assert_not_called()

    def test_direct_and_explicit_proxy_are_respected(self):
        for settings, expected in ((Settings(direct_connection=True), {}),
                                   (Settings(proxy="http://localhost:8080"),
                                    {"http": "http://localhost:8080", "https": "http://localhost:8080"})):
            with patch("pyrobolic.thumbnail.ProxyHandler") as proxy, patch("pyrobolic.thumbnail.build_opener") as opener:
                opener.return_value.open.return_value = BytesIO(b"image")
                fetch_thumbnail("https://example.org/image", settings, threading.Event())
                proxy.assert_called_once_with(expected)
