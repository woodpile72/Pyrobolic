# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Best-effort, bounded thumbnail loading; no cache files or download dependency."""
import threading
import time
from urllib.request import ProxyHandler, Request, build_opener


def fetch_thumbnail(url, options, canceled):
    from .core import validate_url
    validate_url(url)
    if options.proxy.startswith("socks"):
        return None  # Never bypass an explicitly selected proxy for a preview.
    proxies = {} if options.direct_connection else (
        {"http": options.proxy, "https": options.proxy} if options.proxy else None)
    opener = build_opener(ProxyHandler(proxies))
    deadline = time.monotonic() + 4
    chunks, size = [], 0
    if canceled.is_set():
        return None
    with opener.open(Request(url), timeout=3) as response:
        while not canceled.is_set() and time.monotonic() < deadline:
            block = response.read1(65536)
            if not block:
                return b"".join(chunks)
            size += len(block)
            if size > 2 * 1024 * 1024:
                return None
            chunks.append(block)
    return None


def load_thumbnail(url, options, ready):
    canceled = threading.Event()
    def work():
        try:
            import gi
            gi.require_version("GdkPixbuf", "2.0")
            from gi.repository import GdkPixbuf, GLib
            data = fetch_thumbnail(url, options, canceled)
            if not data or canceled.is_set():
                return
            loader = GdkPixbuf.PixbufLoader()
            def size_ready(loader, width, height):
                scale = min(144 / max(1, width), 90 / max(1, height), 1)
                loader.set_size(max(1, round(width * scale)), max(1, round(height * scale)))
            loader.connect("size-prepared", size_ready)
            loader.write(data)
            loader.close()
            pixbuf = loader.get_pixbuf()
            if pixbuf is not None and not canceled.is_set():
                def deliver():
                    if not canceled.is_set():
                        ready(pixbuf)
                    return False
                GLib.idle_add(deliver)
        except Exception:
            pass  # A missing or invalid thumbnail must never block the download workflow.
    threading.Thread(target=work, daemon=True, name="pyro-thumbnail").start()
    return canceled
