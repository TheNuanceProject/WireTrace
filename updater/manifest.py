# SPDX-License-Identifier: GPL-3.0-or-later
"""The signed update manifest: what a release is, and proof of who said so.

The website serves one small file that names the latest version and, for
each platform, the installer's name, size and SHA-256. The application
acts on that file only if it carries a valid signature from one of the
trusted keys in ``version.py``. The website, its name service and its deploy
pipeline can therefore delay an update, but cannot make anyone install
something that was not signed.

The file is an envelope of two parts::

    {"payload": "<base64>", "signatures": [{"key": "<id>", "signature": "<base64>"}]}

The signature covers the bytes of the payload exactly as they are, so
there is no canonical form to disagree about. The payload is JSON::

    {"format": 2, "product": "WireTrace", "latest_version": "2.0.0",
     "min_version": "1.0.0", "release_notes": "...",
     "signed_at": "2026-10-14T09:30:00Z",
     "platforms": {"windows-x86_64": {"file_name": "...", "file_size": 1,
                                      "sha256_hash": "..."}}}

``signed_at`` is the moment the maintainer signed it, in UTC. An
installed WireTrace remembers the latest it has accepted and refuses any
manifest signed before it, so an older manifest, genuinely signed, cannot
be served again to hold an installation back. The release build writes
the manifest without it; the signing tool adds it.

Nothing of the payload is read before a signature has been verified, and
every field is then checked against what this version of the application
expects. The address of a download is never taken from the manifest: it
is built here from the pinned releases address, the version and a file
name of exactly the expected form.

This module does not touch the GUI, the disk or the network.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.constants import UPDATE_DOWNLOAD_MAX_BYTES, UPDATE_MANIFEST_MAX_BYTES
from version import (
    APP_NAME,
    LINUX_APPIMAGE_NAME,
    RELEASE_PAGE_URL,
    RELEASES_BASE_URL,
    WINDOWS_INSTALLER_NAME,
)

MANIFEST_FORMAT = 2

#: Platforms an installer is published for, and the name of each one's file.
PLATFORM_FILE_NAMES: dict[str, str] = {
    "windows-x86_64": WINDOWS_INSTALLER_NAME,
    "linux-x86_64": LINUX_APPIMAGE_NAME,
}

_ENCODING = "utf-8"
_PUBLIC_KEY_BYTES = 32
_SIGNATURE_BYTES = 64
_KEY_ID_BYTES = 8
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
#: When a manifest was signed: a UTC moment to the second.
_SIGNED_AT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_SIGNED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class ManifestError(ValueError):
    """The manifest is not signed by a trusted key, or is not well formed."""


@dataclass(frozen=True)
class PlatformRelease:
    """The installer of one platform."""

    file_name: str
    file_size: int
    sha256: str


@dataclass(frozen=True)
class ReleaseManifest:
    """A release, as a verified manifest describes it."""

    version: str
    min_version: str
    release_notes: str
    platforms: dict[str, PlatformRelease]
    #: When the maintainer signed it; None before it is signed.
    signed_at: datetime | None = None

    @property
    def release_page(self) -> str:
        """The page of this release, for installs that cannot update themselves."""
        return RELEASE_PAGE_URL.format(version=self.version)

    def download_url(self, platform_key: str) -> str:
        """Where the installer of ``platform_key`` is downloaded from."""
        return f"{RELEASES_BASE_URL}/v{self.version}/{self.platforms[platform_key].file_name}"


# ── Versions ─────────────────────────────────────────────────────────────


def parse_version(text: str) -> tuple[int, int, int]:
    """A ``MAJOR.MINOR.PATCH`` version as numbers that compare in order.

    Raises:
        ManifestError: ``text`` is not such a version.
    """
    match = _VERSION.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        raise ManifestError(f"not a version: {text!r}")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def is_newer(candidate: str, installed: str) -> bool:
    """Whether ``candidate`` is a later version than ``installed``."""
    return parse_version(candidate) > parse_version(installed)


# ── Keys and signatures ──────────────────────────────────────────────────────


def decode_public_key(encoded: str) -> bytes:
    """The 32 bytes of a public key written in base64.

    Raises:
        ManifestError: it is not base64 or not 32 bytes.
    """
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ManifestError("a public key is not valid base64") from exc
    if len(raw) != _PUBLIC_KEY_BYTES:
        raise ManifestError(f"a public key must be {_PUBLIC_KEY_BYTES} bytes")
    return raw


def key_id(public_key: bytes) -> str:
    """A short name for a public key, derived from the key itself."""
    return hashlib.sha256(public_key).hexdigest()[: _KEY_ID_BYTES * 2]


def build_envelope(payload: bytes, signatures: Sequence[tuple[str, bytes]]) -> bytes:
    """Wrap a payload and its ``(key id, signature)`` pairs for publishing."""
    envelope = {
        "payload": base64.b64encode(payload).decode("ascii"),
        "signatures": [
            {"key": key, "signature": base64.b64encode(signature).decode("ascii")}
            for key, signature in signatures
        ],
    }
    return (json.dumps(envelope, indent=2) + "\n").encode(_ENCODING)


def _decode_field(value: object, name: str) -> bytes:
    if not isinstance(value, str):
        raise ManifestError(f"the manifest has no {name}")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ManifestError(f"the manifest's {name} is not valid base64") from exc


def verify_envelope(data: bytes, trusted_keys: Sequence[bytes]) -> bytes:
    """Return the payload of ``data`` if a trusted key signed it.

    One valid signature from one trusted key is enough, so a release can
    be signed with the spare key when the working key is not available.

    Raises:
        ManifestError: no trusted key signed the payload, or ``data`` is
            not an envelope.
    """
    if not trusted_keys:
        raise ManifestError("this build holds no key to verify updates with")
    if len(data) > UPDATE_MANIFEST_MAX_BYTES:
        raise ManifestError("the manifest is larger than a manifest can be")
    try:
        envelope = json.loads(data.decode(_ENCODING))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ManifestError("the manifest is not a signed envelope") from exc
    if not isinstance(envelope, dict) or not isinstance(envelope.get("signatures"), list):
        raise ManifestError("the manifest is not a signed envelope")

    payload = _decode_field(envelope.get("payload"), "payload")
    keys = {key_id(key): key for key in trusted_keys}
    for entry in envelope["signatures"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("key"), str):
            continue
        if entry["key"] not in keys:
            continue
        # An entry that is not a valid signature is passed over, so that
        # one bad entry cannot hide a good one that follows it.
        try:
            signature = _decode_field(entry.get("signature"), "signature")
            if len(signature) != _SIGNATURE_BYTES:
                continue
            Ed25519PublicKey.from_public_bytes(keys[entry["key"]]).verify(signature, payload)
        except (ManifestError, InvalidSignature):
            continue
        return payload
    raise ManifestError("the manifest is not signed by a trusted key")


# ── The payload ──────────────────────────────────────────────────────────


def expected_file_name(platform_key: str, version: str) -> str:
    """The only file name a release of ``version`` may have on a platform."""
    return PLATFORM_FILE_NAMES[platform_key].format(name=APP_NAME, version=version)


def _text(payload: dict, name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str):
        raise ManifestError(f"the manifest has no {name}")
    return value


def _platform(key: str, entry: object, version: str) -> PlatformRelease:
    if not isinstance(entry, dict):
        raise ManifestError(f"the manifest's entry for {key} is not well formed")
    file_name = entry.get("file_name")
    if file_name != expected_file_name(key, version):
        raise ManifestError(f"the manifest names an unexpected file for {key}")
    size = entry.get("file_size")
    if isinstance(size, bool) or not isinstance(size, int):
        raise ManifestError(f"the manifest gives no size for {key}")
    if not 0 < size <= UPDATE_DOWNLOAD_MAX_BYTES:
        raise ManifestError(f"the manifest gives an impossible size for {key}")
    digest = entry.get("sha256_hash")
    if not isinstance(digest, str) or _SHA256_HEX.fullmatch(digest) is None:
        raise ManifestError(f"the manifest gives no SHA-256 for {key}")
    return PlatformRelease(file_name=file_name, file_size=size, sha256=digest)


def parse_manifest(payload: bytes) -> ReleaseManifest:
    """Read a verified payload. Every field must be exactly as expected.

    A platform this version does not know is left out, so a later release
    can add one without this version refusing the whole manifest.

    Raises:
        ManifestError: the payload is for another product or format, or a
            field is missing or malformed.
    """
    try:
        data = json.loads(payload.decode(_ENCODING))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ManifestError("the manifest's payload is not JSON") from exc
    if not isinstance(data, dict):
        raise ManifestError("the manifest's payload is not an object")
    if data.get("format") != MANIFEST_FORMAT or isinstance(data.get("format"), bool):
        raise ManifestError("the manifest is of a format this version does not read")
    if data.get("product") != APP_NAME:
        raise ManifestError("the manifest is for another product")

    version = _text(data, "latest_version")
    parse_version(version)
    min_version = _text(data, "min_version")
    parse_version(min_version)
    platforms = data.get("platforms")
    if not isinstance(platforms, dict):
        raise ManifestError("the manifest lists no platforms")
    return ReleaseManifest(
        version=version,
        min_version=min_version,
        release_notes=_text(data, "release_notes"),
        platforms={
            key: _platform(key, entry, version)
            for key, entry in platforms.items()
            if key in PLATFORM_FILE_NAMES
        },
        signed_at=parse_signed_at(data.get("signed_at")) if "signed_at" in data else None,
    )


def parse_signed_at(value: object) -> datetime:
    """A time of signing as the manifest writes it, in UTC.

    Raises:
        ManifestError: ``value`` is not such a time.
    """
    if not isinstance(value, str) or _SIGNED_AT.fullmatch(value) is None:
        raise ManifestError("the manifest's time of signing is not well formed")
    try:
        return datetime.strptime(value, _SIGNED_AT_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ManifestError("the manifest's time of signing is not a real time") from exc


def format_signed_at(moment: datetime) -> str:
    """A time of signing as the manifest writes it."""
    return moment.astimezone(timezone.utc).strftime(_SIGNED_AT_FORMAT)


def build_payload(manifest: ReleaseManifest) -> bytes:
    """The bytes to sign for ``manifest``. The inverse of ``parse_manifest``."""
    data = {
        "format": MANIFEST_FORMAT,
        "product": APP_NAME,
        "latest_version": manifest.version,
        "min_version": manifest.min_version,
        "release_notes": manifest.release_notes,
        **({} if manifest.signed_at is None else {"signed_at": format_signed_at(manifest.signed_at)}),
        "platforms": {
            key: {
                "file_name": release.file_name,
                "file_size": release.file_size,
                "sha256_hash": release.sha256,
            }
            for key, release in sorted(manifest.platforms.items())
        },
    }
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode(_ENCODING)


def read_signed_manifest(data: bytes, trusted_keys: Sequence[bytes]) -> ReleaseManifest:
    """Verify ``data`` and read it. The one way the application reads a manifest.

    Raises:
        ManifestError: it is not signed by a trusted key, is not well
            formed, or does not say when it was signed.
    """
    manifest = parse_manifest(verify_envelope(data, trusted_keys))
    if manifest.signed_at is None:
        raise ManifestError("the manifest does not say when it was signed")
    return manifest
