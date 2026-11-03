#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-only
"""Build Firefox submission archives and stage optional integrations for the main deb."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import zipfile

ADDON = "extensions/firefox/addon/"
SIGNED = "extensions/firefox/signed.xpi"


def addon_payload(root, names):
    payload = {name.removeprefix(ADDON): (root / name).read_bytes()
               for name in sorted(names) if name.startswith(ADDON)}
    if "manifest.json" not in payload:
        raise ValueError("Firefox manifest is missing from release inputs")
    return payload


def build_firefox(root, names, output):
    payload = addon_payload(root, names)
    with zipfile.ZipFile(output, "x", zipfile.ZIP_DEFLATED) as archive:
        for name, data in payload.items():
            entry = zipfile.ZipInfo(name, (2026, 11, 2, 0, 0, 0))
            entry.external_attr = 0o100644 << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, data)
    return Path(output)


def validate_signed_payload(root, names):
    """Check payload identity and signature metadata; Firefox verifies the signature."""
    if SIGNED not in names:
        raise ValueError("Mozilla-signed Firefox XPI is required for a release build. Use a development revision until unlisted signing is complete.")
    path = root / SIGNED
    expected = addon_payload(root, names)
    with zipfile.ZipFile(path) as archive:
        files = [entry for entry in archive.infolist() if not entry.is_dir()]
        if len(files) != len({entry.filename for entry in files}):
            raise ValueError("Duplicate filenames in signed Firefox XPI")
        if sum(entry.file_size for entry in files) > 5 * 1024 * 1024:
            raise ValueError("Signed Firefox XPI exceeds the expected size limit")
        metadata = {entry.filename.lower() for entry in files if entry.filename.upper().startswith("META-INF/")}
        if not ({"meta-inf/mozilla.rsa", "meta-inf/cose.sig"} & metadata):
            raise ValueError("Firefox XPI has no Mozilla signature metadata")
        actual = {entry.filename: archive.read(entry) for entry in files
                  if not entry.filename.upper().startswith("META-INF/")}
        if actual != expected:
            raise ValueError("Signed Firefox XPI does not match the declared extension sources")
    return path


def stage_extensions(root, stage, names, *, require_signed):
    def copy(source, target, executable=False):
        path = stage / target
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / source, path)
        path.chmod(0o755 if executable else 0o644)
    for name in ("extensions/native/bridge.py", "extensions/firefox/host.py"):
        if name not in names:
            raise ValueError(f"Missing extension release input: {name}")
        copy(name, "usr/lib/pyrobolic/" + name, executable=name.endswith("/host.py"))
    copy("extensions/firefox/native-host.json",
         "usr/lib/mozilla/native-messaging-hosts/io.github.woodpile72.pyrobolic.json")
    for name in names:
        if name.startswith(ADDON):
            copy(name, "usr/share/pyrobolic/" + name)
    copy("extensions/README.rst", "usr/share/doc/pyrobolic/extensions.rst")
    destination = stage / "usr/share/pyrobolic/extensions/firefox"
    if require_signed or SIGNED in names:
        signed = validate_signed_payload(root, names)
        shutil.copyfile(signed, destination / "pyrobolic-firefox.xpi")
    else:
        build_firefox(root, names, destination / "pyrobolic-firefox-unsigned.xpi")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from package_deb import release_files
    parser = argparse.ArgumentParser(description="Build the Firefox XPI to submit for unlisted signing")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    print(build_firefox(root, release_files(root, "source"), args.output))
