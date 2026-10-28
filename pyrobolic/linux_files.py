# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Linux filesystem operations whose no-overwrite semantics matter to publication."""
import ctypes
import errno
import os
import sys


def rename_no_replace(source, target):
    if not sys.platform.startswith("linux"):
        raise OSError(errno.ENOTSUP, "Atomic no-overwrite publication is unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise OSError(errno.ENOTSUP, "Atomic no-overwrite publication is unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(target))
