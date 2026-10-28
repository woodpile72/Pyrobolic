# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Per-download network policy without credentials or platform UI dependencies."""
from dataclasses import dataclass, fields
from urllib.parse import urlsplit

BROWSER_IDENTITIES = ["default", "firefox", "chrome", "safari"]
BROWSER_LABELS = ["Default", "Firefox", "Chrome", "Safari"]


@dataclass(kw_only=True)
class TransferPreferences:
    rate_limit_kib: int = 0
    fragment_concurrency: int = 1
    proxy: str = ""
    direct_connection: bool = False
    use_part_files: bool = True
    browser_identity: str = "default"


def transfer_values(options):
    return {field.name: getattr(options, field.name) for field in fields(TransferPreferences)}


def inspection_arguments(options, url=None):
    validate_transfer(options)
    args = ["--proxy", ""] if options.direct_connection else (["--proxy", options.proxy] if options.proxy else [])
    identity = options.browser_identity
    hostname = (urlsplit(url or "").hostname or "").lower().rstrip(".")
    # Rumble's metadata endpoint rejects the engine's ordinary HTTP identity.
    if identity == "default" and (hostname == "rumble.com" or hostname.endswith(".rumble.com")):
        identity = "firefox"
    if identity != "default":
        args += ["--impersonate", identity]
    return args


def has_inspection_options(options):
    return bool(options.direct_connection or options.proxy or options.browser_identity != "default")


def validate_transfer(options):
    if type(options.direct_connection) is not bool:
        raise ValueError("Invalid direct connection policy")
    if options.direct_connection and options.proxy:
        raise ValueError("Choose either a direct connection or a proxy, not both.")
    if options.browser_identity not in BROWSER_IDENTITIES:
        raise ValueError("Invalid browser identity")
    if type(options.use_part_files) is not bool:
        raise ValueError("Invalid partial-file policy")
    for key, minimum, maximum in (("rate_limit_kib", 0, 1048576), ("fragment_concurrency", 1, 16)):
        value = getattr(options, key)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"Invalid {key.replace('_', ' ')}")
    value = options.proxy
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Invalid proxy address")
    if value:
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            raise ValueError("Enter a proxy URL with a valid port.") from None
        if (parsed.scheme not in {"http", "https", "socks4", "socks5", "socks5h"} or not parsed.hostname or
                parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or
                parsed.path not in {"", "/"} or any(c.isspace() or ord(c) < 32 for c in value) or port == 0):
            raise ValueError("Use an HTTP, HTTPS or SOCKS proxy URL without credentials, path or query.")


def transfer_arguments(options):
    validate_transfer(options)
    args = ["--concurrent-fragments", str(options.fragment_concurrency)]
    args += ["--part"] if options.use_part_files else ["--no-part", "--force-overwrites", "--no-continue"]
    if options.rate_limit_kib:
        args += ["--limit-rate", f"{options.rate_limit_kib}K"]
    args += inspection_arguments(options, getattr(options, "url", None))
    return args


def transfer_summary(options):
    rate = f"{options.rate_limit_kib:,} KiB/s per download" if options.rate_limit_kib else "Unlimited Rate"
    return (f"{rate} · {options.fragment_concurrency} Fragment(s)" +
            (" · Direct Connection" if options.direct_connection else " · Proxy" if options.proxy else "") +
            (" · Restart Incomplete" if not options.use_part_files else "") +
            (" · " + BROWSER_LABELS[BROWSER_IDENTITIES.index(options.browser_identity)] if options.browser_identity != "default" else ""))
