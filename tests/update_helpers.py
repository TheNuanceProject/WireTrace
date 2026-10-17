# SPDX-License-Identifier: GPL-3.0-or-later
"""Helpers for the tests of the updater: keys made for the test, signed
manifests, and a stand-in for the server."""

from __future__ import annotations

import base64
import hashlib
import io
import urllib.request
from collections.abc import Mapping
from datetime import datetime, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from updater.manifest import (
    PlatformRelease,
    ReleaseManifest,
    build_envelope,
    build_payload,
    expected_file_name,
    key_id,
)

#: When the manifests of the tests were signed, unless a test says otherwise.
SIGNED_AT = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


class SigningKey:
    """A key pair that exists only for one test."""

    def __init__(self) -> None:
        self._private = Ed25519PrivateKey.generate()
        self.public = self._private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw,
        )
        self.public_base64 = base64.b64encode(self.public).decode("ascii")
        self.id = key_id(self.public)

    def sign(self, payload: bytes) -> bytes:
        return self._private.sign(payload)

    def envelope(self, payload: bytes) -> bytes:
        return build_envelope(payload, [(self.id, self.sign(payload))])


def release(
    version: str = "9.9.9",
    *,
    platforms: tuple[str, ...] = ("windows-x86_64", "linux-x86_64"),
    content: bytes = b"installer",
    min_version: str = "1.0.0",
    notes: str = "- A change",
    signed_at: datetime | None = SIGNED_AT,
) -> ReleaseManifest:
    """A manifest whose installers all have ``content``."""
    return ReleaseManifest(
        version=version,
        min_version=min_version,
        release_notes=notes,
        signed_at=signed_at,
        platforms={
            key: PlatformRelease(
                file_name=expected_file_name(key, version),
                file_size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
            for key in platforms
        },
    )


def signed(manifest: ReleaseManifest, key: SigningKey) -> bytes:
    return key.envelope(build_payload(manifest))


class _Answer(io.BytesIO):
    def __init__(self, body: bytes, headers: Mapping[str, str]) -> None:
        super().__init__(body)
        self.headers = dict(headers)


class FakeServer:
    """Answers requests from a table of address to bytes.

    It is an ``Opener``: what would go to the network comes here instead.
    Every request is kept, so a test can read what was sent.
    """

    def __init__(self) -> None:
        self.pages: dict[str, bytes] = {}
        self.headers: dict[str, str] = {}
        self.requests: list[urllib.request.Request] = []
        self.error: Exception | None = None

    def open(self, request: urllib.request.Request, timeout: float) -> _Answer:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if request.full_url not in self.pages:
            raise OSError(f"HTTP Error 404: {request.full_url}")
        return _Answer(self.pages[request.full_url], self.headers)

    def sent(self, name: str) -> list[str | None]:
        """The value of header ``name`` in each request, in order."""
        return [request.get_header(name.capitalize()) for request in self.requests]
