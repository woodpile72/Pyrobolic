#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-only
"""Shared native messaging bridge. One bounded request per invocation; no URL logs."""
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
from urllib.parse import urlsplit

VERSION = "1.0.0"
LAUNCHER = Path("/usr/bin/pyrobolic")
MAX_MESSAGE = 65536


def validate_url(value):
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise ValueError("Choose a valid HTTP or HTTPS address.")
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value) or "\\" in value:
        raise ValueError("The address contains invalid characters.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError()
        if not value.lower().startswith(("http://", "https://")):
            raise ValueError()
    except ValueError:
        raise ValueError("Choose an HTTP or HTTPS address without embedded login details.") from None
    return value


def read_exact(stream, length):
    data = bytearray()
    while len(data) < length:
        part = stream.read(length - len(data))
        if not part:
            raise ValueError("Incomplete browser message.")
        data.extend(part)
    return bytes(data)


def read_message(stream):
    length = struct.unpack("=I", read_exact(stream, 4))[0]
    if not 0 < length <= MAX_MESSAGE:
        raise ValueError("Browser message is too large or empty.")
    try:
        return json.loads(read_exact(stream, length).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("Invalid browser message.") from None


def write_message(stream, message):
    payload = json.dumps(message, ensure_ascii=True).encode("utf-8")
    stream.write(struct.pack("=I", len(payload)) + payload)
    stream.flush()


def launch(url):
    # Fixed executable and argv. A URL cannot become an option or a shell command.
    # Disconnect stdio so the app cannot corrupt Firefox's JSON pipe, and can
    # remain open after Firefox closes this one-shot native host.
    try:
        process = subprocess.Popen([str(LAUNCHER), "--", url], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True, close_fds=True)
        try:
            code = process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            return
        if code:
            raise ValueError("Pyrobolic could not open. Try launching it from the application menu.")
    except OSError:
        raise ValueError("Pyrobolic could not be launched. Check its installation.") from None


def handle(message):
    if not isinstance(message, dict) or message.get("action") not in {"ping", "open"}:
        raise ValueError("Unsupported browser request.")
    expected = {"action"} if message["action"] == "ping" else {"action", "url"}
    if set(message) != expected:
        raise ValueError("Invalid browser request fields.")
    url = validate_url(message.get("url")) if message["action"] == "open" else None
    if not LAUNCHER.is_file() or not os.access(LAUNCHER, os.X_OK):
        raise ValueError("Install Pyrobolic 1.2.0 or newer, then try again.")
    if url is not None:
        launch(url)
    return {"ok": True, "version": VERSION, "status": "launch-requested" if url else "ready"}


def main(*, caller_allowed=False):
    try:
        if not caller_allowed:
            raise ValueError("This bridge accepts requests only from an authorized Pyrobolic browser extension.")
        response = handle(read_message(sys.stdin.buffer))
    except ValueError as error:
        response = {"ok": False, "error": str(error)}
    except Exception:
        response = {"ok": False, "error": "The Pyrobolic bridge could not complete the request."}
    try:
        write_message(sys.stdout.buffer, response)
    except (BrokenPipeError, OSError):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
