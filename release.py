#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Build source and Debian artifacts from an exact Git commit; never publish automatically."""
import argparse
import ast
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile

from package_deb import release_files, DEFAULT_MAINTAINER


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(output, epoch, root=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parent
    names = sorted(set(release_files(root, "runtime") + release_files(root, "source")))
    payload = {name: (root / name).read_bytes() for name in names}
    inventory = {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()}
    payload["SOURCE-MANIFEST.json"] = (json.dumps(inventory, indent=2, sort_keys=True) + "\n").encode()
    with Path(output).open("xb") as stream, tarfile.open(fileobj=stream, mode="w:xz", format=tarfile.PAX_FORMAT) as archive:
        for name, data in sorted(payload.items()):
            entry = tarfile.TarInfo("pyrobolic/" + name)
            entry.size = len(data)
            entry.mode = 0o755 if name == "pyrobolic-launcher" else 0o644
            entry.mtime = epoch
            archive.addfile(entry, io.BytesIO(data))
    return inventory


@contextmanager
def frozen_commit(repository, commit, epoch=None):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repository), *args])
    identifier = git("rev-parse", "--verify", "--end-of-options", commit + "^{commit}").decode().strip()
    if epoch is None:
        epoch = int(git("show", "-s", "--format=%ct", identifier))
    if type(epoch) is not int or epoch < 0:
        raise ValueError("Build epoch must be nonnegative")
    with tempfile.TemporaryDirectory(prefix="pyrobolic-commit-") as temporary:
        root = Path(temporary)
        with tarfile.open(fileobj=io.BytesIO(git("archive", "--format=tar", identifier))) as archive:
            for member in archive.getmembers():
                if member.issym() or member.islnk() or (not member.isfile() and not member.isdir()):
                    raise ValueError("Release commits must contain only regular files and directories")
                if Path(member.name).suffix.lower() in {".md", ".markdown", ".mdown"}:
                    raise ValueError("Release commit contains Markdown")
            archive.extractall(root, filter="data")
        declared = set(release_files(root, "runtime") + release_files(root, "source"))
        actual = {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}
        if declared != actual:
            raise ValueError("Committed files must exactly match the release manifest")
        yield root, identifier, epoch


def application_version(root):
    tree = ast.parse((root / "pyrobolic/__init__.py").read_text())
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "__version__"
                                                     for target in statement.targets):
            value = ast.literal_eval(statement.value)
            if isinstance(value, str) and value:
                return value
    raise ValueError("Frozen source has no application version")


def bundle(destination, revision=None, epoch=None, maintainer=DEFAULT_MAINTAINER, commit="HEAD", repository=None):
    destination = Path(destination).absolute()
    repository = Path(repository) if repository is not None else Path(__file__).resolve().parent
    with frozen_commit(repository, commit, epoch) as (root, identifier, epoch):
        upstream_version = application_version(root)
        version = upstream_version if revision is None else f"{upstream_version}+dev{revision}"
        destination.mkdir(parents=True, exist_ok=False)
        source = destination / f"pyrobolic_{version}_source.tar.xz"
        inventory = snapshot(source, epoch, root)
    package = destination / f"pyrobolic_{version}_all.deb"
    version_args = ["--base-version"] if revision is None else ["--revision", str(revision)]
    # Build the frozen inputs, not a checkout that may change after snapshotting.
    with tempfile.TemporaryDirectory(prefix="pyrobolic-release-") as temporary:
        with tarfile.open(source) as archive:
            archive.extractall(temporary, filter="data")
        subprocess.run(["/usr/bin/python3", "package_deb.py", str(package), *version_args,
                        "--maintainer", maintainer], cwd=Path(temporary) / "pyrobolic",
                       env={**os.environ, "SOURCE_DATE_EPOCH": str(epoch)}, check=True)
    information = {
        "version": version, "source_date_epoch": epoch, "revision": revision, "maintainer": maintainer,
        "source_commit": identifier, "application_version": upstream_version,
        "source_sha256": sha256(source), "package_sha256": sha256(package),
        "source_files": inventory,
        "build_tools": {"python": subprocess.check_output(["/usr/bin/python3", "--version"], text=True).strip(),
                        "dpkg_deb": subprocess.check_output(["dpkg-deb", "--version"], text=True).splitlines()[0]},
        "rebuild": f"SOURCE_DATE_EPOCH={epoch} /usr/bin/python3 package_deb.py rebuilt.deb {' '.join(version_args)} --maintainer '{maintainer}'",
    }
    info = destination / "build-info.json"
    info.write_text(json.dumps(information, indent=2, sort_keys=True) + "\n")
    (destination / "SHA256SUMS").write_text("".join(f"{sha256(path)}  {path.name}\n" for path in (source, package, info)))
    return package


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="New directory outside the source checkout")
    versions = parser.add_mutually_exclusive_group()
    versions.add_argument("--revision", type=int)
    versions.add_argument("--base-version", dest="revision", action="store_const", const=None)
    parser.add_argument("--commit", default="HEAD", help="Exact source commit or ref (default: HEAD)")
    parser.add_argument("--epoch", type=int, help="Reproducible timestamp (default: commit timestamp)")
    parser.add_argument("--maintainer", default=DEFAULT_MAINTAINER)
    args = parser.parse_args()
    if (args.revision is not None and args.revision < 1) or (args.epoch is not None and args.epoch < 0):
        parser.error("Revision must be positive and epoch nonnegative")
    if Path(__file__).resolve().parent in args.destination.absolute().parents:
        parser.error("Keep release outputs outside the checkout")
    print(bundle(args.destination, args.revision, args.epoch, args.maintainer, args.commit))
