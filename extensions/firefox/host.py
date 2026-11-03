#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-only
"""Firefox caller validation for the shared local URL bridge."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native"))
from bridge import main

if __name__ == "__main__":
    # Firefox supplies the manifest filename and calling extension ID.
    raise SystemExit(main(caller_allowed=len(sys.argv) == 3 and
                          sys.argv[2] == "pyrobolic@woodpile72"))
