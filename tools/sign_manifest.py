# SPDX-License-Identifier: GPL-3.0-or-later
"""Sign the update manifest of a release.

The release build leaves a draft release holding the installers,
``manifest.unsigned.json`` and ``SHA256SUMS``, each with a build
provenance attestation. The maintainer downloads all of them into one
folder on their own computer and runs this tool, which:

  1. Confirms with GitHub that every installer and the unsigned manifest
     were built by the release workflow of this repository, from the
     tag of this version, on GitHub's own machines
     (``gh attestation verify``).
  2. Recomputes the size and SHA-256 of every installer and checks them
     against the manifest and against ``SHA256SUMS``.
  3. Shows what is about to be signed and asks.
  4. Stamps the moment of signing, signs with the private key, reads the
     result back exactly as an installed WireTrace reads it, with the
     public keys in ``version.py``, and only then writes the two files
     the website serves:

       - ``wiretrace/manifest.json``: the signed manifest that versions
         from 2.0 read.
       - ``wiretrace-update.json``: the unsigned manifest that 1.x
         versions read, which is how they are told about a release.

A file that was changed after the build, or built anywhere else, is
therefore never signed. The private key and its passphrase never leave
this process.

Usage:
    python tools/sign_manifest.py RELEASE-FOLDER --key KEYFILE --out FOLDER
"""

from __future__ import annotations

import argparse
import dataclasses
import getpass
import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.release_keys import SigningKeyError, load_key, public_bytes
from tools.release_manifest import CHECKSUMS_NAME, UNSIGNED_MANIFEST_NAME, sha256_of
from updater.manifest import (
    ManifestError,
    ReleaseManifest,
    build_envelope,
    build_payload,
    decode_public_key,
    format_signed_at,
    key_id,
    parse_manifest,
    read_signed_manifest,
)
from version import (
    APP_REPOSITORY_URL,
    APP_VERSION,
    UPDATE_LEGACY_JSON_FILE,
    UPDATE_MANIFEST_URL,
    UPDATE_PUBLIC_KEYS,
)

_ENCODING = "utf-8"
_YES = "yes"

#: The repository and the workflow that alone may have built a release.
REPOSITORY = APP_REPOSITORY_URL.removeprefix("https://github.com/")
RELEASE_WORKFLOW = f"{REPOSITORY}/.github/workflows/release.yml"
#: How long GitHub is given to answer for one file.
_ATTESTATION_TIMEOUT_S = 120

#: The name of each platform in the manifest that 1.x versions read.
_LEGACY_PLATFORM_NAMES = {"windows-x86_64": "windows", "linux-x86_64": "linux"}


class ProvenanceError(Exception):
    """A file was not built by the release workflow, or that could not be confirmed."""


#: Confirms a file's provenance, or raises ProvenanceError.
Verifier = Callable[[Path], None]


def tag_ref(version: str) -> str:
    """The git ref the release of ``version`` is built from."""
    return f"refs/tags/v{version}"


def attestation_command(gh: str, path: Path, version: str) -> list[str]:
    """The ``gh`` command that confirms ``path`` was built for ``version``."""
    return [
        gh, "attestation", "verify", str(path),
        "--repo", REPOSITORY,
        "--signer-workflow", RELEASE_WORKFLOW,
        "--source-ref", tag_ref(version),
        "--deny-self-hosted-runners",
    ]


def github_verifier(version: str) -> Verifier:
    """A verifier that asks GitHub, through the ``gh`` command.

    Raises:
        ProvenanceError: ``gh`` is not installed.
    """
    gh = shutil.which("gh")
    if gh is None:
        raise ProvenanceError(
            "the GitHub command line tool, gh, is needed to confirm where the files were built: "
            "https://cli.github.com/"
        )

    def verify(path: Path) -> None:
        try:
            result = subprocess.run(
                attestation_command(gh, path, version),
                capture_output=True, text=True, timeout=_ATTESTATION_TIMEOUT_S, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProvenanceError(f"{path.name}: the attestation could not be checked: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise ProvenanceError(
                f"{path.name} was not confirmed as built by {RELEASE_WORKFLOW} "
                f"from {tag_ref(version)}: {detail[-1] if detail else 'no reason given'}"
            )

    return verify


def _checksums(text: str) -> dict[str, str]:
    """The SHA-256 of each file named in a ``SHA256SUMS`` text."""
    sums: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        if not name:
            raise ManifestError(f"{CHECKSUMS_NAME} has a line that is not a checksum: {line!r}")
        sums[name] = digest
    return sums


def check_release(folder: Path, verify: Verifier) -> ReleaseManifest:
    """The unsigned manifest in ``folder``, once every file it names, and
    the manifest itself, are confirmed.

    Raises:
        ProvenanceError: a file was not built by the release workflow.
        ManifestError: the manifest is not of this version, is already
            signed, or does not match the installers or ``SHA256SUMS``.
        OSError: a file is missing or cannot be read.
    """
    unsigned = folder / UNSIGNED_MANIFEST_NAME
    manifest = parse_manifest(unsigned.read_bytes())
    if manifest.version != APP_VERSION:
        raise ManifestError(
            f"the manifest is of version {manifest.version}, this checkout of {APP_VERSION}"
        )
    if manifest.signed_at is not None:
        raise ManifestError("the manifest already carries a time of signing")
    sums = _checksums((folder / CHECKSUMS_NAME).read_text(encoding=_ENCODING))
    verify(unsigned)
    if sums.get(UNSIGNED_MANIFEST_NAME) != sha256_of(unsigned):
        raise ManifestError(f"{UNSIGNED_MANIFEST_NAME} does not match {CHECKSUMS_NAME}")
    for key, release in manifest.platforms.items():
        installer = folder / release.file_name
        verify(installer)
        if installer.stat().st_size != release.file_size:
            raise ManifestError(f"{release.file_name} is not the size the manifest gives for {key}")
        digest = sha256_of(installer)
        if digest != release.sha256:
            raise ManifestError(f"{release.file_name} does not have the SHA-256 the manifest gives for {key}")
        if sums.get(release.file_name) != digest:
            raise ManifestError(f"{release.file_name} does not match {CHECKSUMS_NAME}")
    return manifest


def signed_manifest_path(folder: Path) -> Path:
    """Where the signed manifest goes under ``folder``: the last two parts
    of its address, so the folder mirrors the website's updates folder."""
    return folder.joinpath(*UPDATE_MANIFEST_URL.rsplit("/", 2)[-2:])


def legacy_manifest(manifest: ReleaseManifest) -> bytes:
    """The manifest in the form 1.x versions read."""
    data = {
        "latest_version": manifest.version,
        "release_notes": manifest.release_notes,
        "min_version": manifest.min_version,
        "platforms": {
            _LEGACY_PLATFORM_NAMES[key]: {
                "download_url": manifest.download_url(key),
                "file_size": release.file_size,
                "sha256_hash": release.sha256,
            }
            for key, release in manifest.platforms.items()
            if key in _LEGACY_PLATFORM_NAMES
        },
    }
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode(_ENCODING)


def stamped(manifest: ReleaseManifest, moment: datetime) -> ReleaseManifest:
    """``manifest`` with its time of signing, to the second, in UTC."""
    return dataclasses.replace(manifest, signed_at=moment.astimezone(timezone.utc).replace(microsecond=0))


def sign(manifest: ReleaseManifest, key_file: Path, passphrase: str) -> tuple[bytes, ReleaseManifest]:
    """Sign ``manifest``, which carries its time of signing, and return
    the envelope and the manifest as an installed WireTrace reads it.

    Raises:
        SigningKeyError: the key cannot be read.
        ManifestError: the manifest is not of this version or has no time
            of signing, or the key is not one this version trusts.
    """
    if manifest.version != APP_VERSION:
        raise ManifestError(
            f"the manifest is of version {manifest.version}, this checkout of {APP_VERSION}"
        )
    if manifest.signed_at is None:
        raise ManifestError("the manifest has no time of signing")
    payload = build_payload(manifest)
    key = load_key(key_file, passphrase)
    envelope = build_envelope(payload, [(key_id(public_bytes(key)), key.sign(payload))])
    trusted = [decode_public_key(encoded) for encoded in UPDATE_PUBLIC_KEYS]
    try:
        verified = read_signed_manifest(envelope, trusted)
    except ManifestError as exc:
        raise ManifestError(
            "this key is not one of UPDATE_PUBLIC_KEYS in version.py, "
            "so no installed copy would accept its signature"
        ) from exc
    if verified != manifest:
        raise ManifestError("the signed manifest does not read back as it was written")
    return envelope, verified


def write_signed(folder: Path, envelope: bytes, manifest: ReleaseManifest) -> list[Path]:
    """Write the signed and the legacy manifest under ``folder``."""
    signed = signed_manifest_path(folder)
    legacy = folder / UPDATE_LEGACY_JSON_FILE
    signed.parent.mkdir(parents=True, exist_ok=True)
    signed.write_bytes(envelope)
    legacy.write_bytes(legacy_manifest(manifest))
    return [signed, legacy]


def summary(manifest: ReleaseManifest) -> str:
    lines = [f"Version {manifest.version} (updates from {manifest.min_version} and later)"]
    if manifest.signed_at is not None:
        lines.append(f"Signed at {format_signed_at(manifest.signed_at)} (UTC), by this computer's clock.")
        lines.append(
            "  Check that this is the current time. Installed copies refuse every manifest "
            "signed before the newest they accepted, so a time ahead of now would make them "
            "refuse the manifests of later releases until that time has passed."
        )
    for key, release in manifest.platforms.items():
        lines.append(f"  {key}: {release.file_name}")
        lines.append(f"    {release.file_size:,} bytes, SHA-256 {release.sha256}")
    lines.extend(["", "What the update window will show:", ""])
    lines.extend(f"    {line}" for line in manifest.release_notes.splitlines())
    return "\n".join(lines)


def main(
    arguments: list[str] | None = None,
    *,
    verifier: Callable[[str], Verifier] = github_verifier,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "folder", type=Path,
        help=f"the folder holding the installers, {UNSIGNED_MANIFEST_NAME} and {CHECKSUMS_NAME} of the release",
    )
    parser.add_argument("--key", type=Path, required=True, help="the file of the private key")
    parser.add_argument("--out", type=Path, required=True, help="the folder to write into")
    options = parser.parse_args(arguments)

    try:
        print(f"Confirming with GitHub that the files were built by {RELEASE_WORKFLOW} "
              f"from {tag_ref(APP_VERSION)}...")
        manifest = stamped(check_release(options.folder, verifier(APP_VERSION)), now())
        print("Every file is confirmed and matches the manifest.\n")
        print(summary(manifest))
        if input(f"\nSign this manifest? Type {_YES} to go on: ").strip().lower() != _YES:
            print("Nothing was signed.")
            return 1
        envelope, signed = sign(manifest, options.key, getpass.getpass("Passphrase: "))
        written = write_signed(options.out, envelope, signed)
    except (ProvenanceError, SigningKeyError, ManifestError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("\nSigned and verified. Wrote:")
    for path in written:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
