# SPDX-License-Identifier: GPL-3.0-or-later
"""Describe the files of a release: their checksums and the update manifest.

Run on a folder that holds the finished installers of every platform.
It writes two files into that folder:

  - ``SHA256SUMS``: the SHA-256 of every file of the release, in the
    form ``sha256sum --check`` reads.
  - ``manifest.unsigned.json``: the update manifest of this version,
    ready to be signed with ``tools/sign_manifest.py``.

The version is the one in ``version.py``. What the update window says
about the release is read from ``build/release_notes.txt``, whose first
line must name that version, so that the notes of an earlier release
cannot be published by oversight.

Usage:
    python tools/release_manifest.py FOLDER
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from updater.manifest import (
    PLATFORM_FILE_NAMES,
    ManifestError,
    PlatformRelease,
    ReleaseManifest,
    build_payload,
    expected_file_name,
    parse_manifest,
)
from version import APP_NAME, APP_VERSION, UPDATE_MIN_VERSION

RELEASE_NOTES = ROOT / "build" / "release_notes.txt"
CHECKSUMS_NAME = "SHA256SUMS"
UNSIGNED_MANIFEST_NAME = "manifest.unsigned.json"

_ENCODING = "utf-8"
_READ_BYTES = 1024 * 1024


class ReleaseError(Exception):
    """The release cannot be described as it is."""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(_READ_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def release_notes(path: Path = RELEASE_NOTES, version: str = APP_VERSION) -> str:
    """The notes of ``version``.

    Raises:
        ReleaseError: the file is missing, or is headed by another version.
    """
    try:
        text = path.read_text(encoding=_ENCODING).strip()
    except OSError as exc:
        raise ReleaseError(f"the release notes could not be read: {exc}") from exc
    heading = f"{APP_NAME} v{version}"
    if text.split("\n", 1)[0].strip() != heading:
        raise ReleaseError(f"{path.name} must begin with the line: {heading}")
    return text


def build_manifest(folder: Path, notes: str, version: str = APP_VERSION) -> ReleaseManifest:
    """The manifest of the installers in ``folder``.

    Raises:
        ReleaseError: the installer of a platform is missing.
    """
    platforms = {}
    for key in PLATFORM_FILE_NAMES:
        installer = folder / expected_file_name(key, version)
        if not installer.is_file():
            raise ReleaseError(f"{installer.name} is not in {folder}")
        platforms[key] = PlatformRelease(
            file_name=installer.name,
            file_size=installer.stat().st_size,
            sha256=sha256_of(installer),
        )
    return ReleaseManifest(
        version=version,
        min_version=UPDATE_MIN_VERSION,
        release_notes=notes,
        platforms=platforms,
    )


def checksums(folder: Path) -> str:
    """One line for each file of ``folder``, in the order of their names."""
    # Sorted by the name itself: Windows compares paths without case, so
    # sorting the paths would order the file differently there.
    files = sorted(
        (path for path in folder.iterdir() if path.is_file() and path.name != CHECKSUMS_NAME),
        key=lambda path: path.name,
    )
    return "".join(f"{sha256_of(path)}  {path.name}\n" for path in files)


def describe_release(folder: Path) -> ReleaseManifest:
    """Write the unsigned manifest and the checksums into ``folder``.

    Raises:
        ReleaseError: an installer or the release notes are missing.
        ManifestError: the manifest would not be accepted by the application.
    """
    manifest = build_manifest(folder, release_notes())
    payload = build_payload(manifest)
    # Read back the way the application reads it, so that a manifest it
    # would refuse is never handed on to be signed.
    if parse_manifest(payload) != manifest:
        raise ManifestError("the manifest does not read back as it was written")
    (folder / UNSIGNED_MANIFEST_NAME).write_bytes(payload)
    (folder / CHECKSUMS_NAME).write_text(checksums(folder), encoding=_ENCODING, newline="\n")
    return manifest


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", type=Path, help="the folder that holds the installers")
    options = parser.parse_args(arguments)
    try:
        manifest = describe_release(options.folder)
    except (ReleaseError, ManifestError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{APP_NAME} {manifest.version}")
    for key, release in manifest.platforms.items():
        print(f"  {key}: {release.file_name}  {release.file_size:,} bytes  {release.sha256}")
    print(f"wrote {UNSIGNED_MANIFEST_NAME} and {CHECKSUMS_NAME} in {options.folder}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
