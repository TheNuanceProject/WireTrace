# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests of the signed update manifest.

An update is trusted only if a key the application holds signed it, and
only if every field is exactly what this version expects. Each way of
getting past that is tried here and must fail.
"""

from __future__ import annotations

import base64
import json

import pytest

from app.constants import UPDATE_DOWNLOAD_MAX_BYTES, UPDATE_MANIFEST_MAX_BYTES
from tests.update_helpers import SigningKey, release, signed
from updater.manifest import (
    MANIFEST_FORMAT,
    PLATFORM_FILE_NAMES,
    ManifestError,
    build_envelope,
    build_payload,
    decode_public_key,
    expected_file_name,
    is_newer,
    key_id,
    parse_manifest,
    parse_version,
    read_signed_manifest,
    verify_envelope,
)
from version import APP_NAME, APP_VERSION, RELEASES_BASE_URL


@pytest.fixture
def key() -> SigningKey:
    return SigningKey()


def _payload(**changes: object) -> bytes:
    data = json.loads(build_payload(release()))
    data.update(changes)
    return json.dumps(data).encode()


def _platform_payload(**changes: object) -> bytes:
    data = json.loads(build_payload(release()))
    data["platforms"]["windows-x86_64"].update(changes)
    return json.dumps(data).encode()


# ── Versions ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1.0.0", (1, 0, 0)), ("0.0.1", (0, 0, 1)), ("10.20.30", (10, 20, 30))],
)
def test_version_is_read_as_three_numbers(text: str, expected: tuple[int, int, int]) -> None:
    assert parse_version(text) == expected


@pytest.mark.parametrize(
    "text",
    ["", "1.0", "1.0.0.0", "v1.0.0", " 1.0.0", "1.0.0 ", "1.0.0-beta", "01.0.0", "1.a.0",
     "not-a-version", None, 100],
)
def test_anything_else_is_not_a_version(text: object) -> None:
    with pytest.raises(ManifestError):
        parse_version(text)


@pytest.mark.parametrize(
    ("candidate", "installed", "newer"),
    [
        ("1.0.1", "1.0.0", True),
        ("1.1.0", "1.0.9", True),
        ("2.0.0", "1.99.99", True),
        ("1.10.0", "1.9.0", True),
        ("1.0.0", "1.0.0", False),
        ("1.0.0", "1.0.1", False),
        ("1.9.0", "1.10.0", False),
    ],
)
def test_versions_compare_as_numbers(candidate: str, installed: str, newer: bool) -> None:
    assert is_newer(candidate, installed) is newer


def test_the_application_version_is_a_version() -> None:
    parse_version(APP_VERSION)


# ── Signatures ───────────────────────────────────────────────────────────


def test_a_manifest_signed_by_a_trusted_key_is_read(key: SigningKey) -> None:
    manifest = release("9.9.9")

    assert read_signed_manifest(signed(manifest, key), [key.public]) == manifest


def test_either_of_two_trusted_keys_is_enough(key: SigningKey) -> None:
    spare = SigningKey()
    payload = build_payload(release())

    assert verify_envelope(spare.envelope(payload), [key.public, spare.public]) == payload
    assert verify_envelope(key.envelope(payload), [key.public, spare.public]) == payload


def test_a_valid_signature_is_found_among_invalid_ones(key: SigningKey) -> None:
    stranger = SigningKey()
    payload = build_payload(release())
    envelope = build_envelope(payload, [
        (stranger.id, stranger.sign(payload)),
        (key.id, b"\x00" * 64),
        (key.id, key.sign(payload)),
    ])

    assert verify_envelope(envelope, [key.public]) == payload


def test_a_manifest_signed_by_another_key_is_refused(key: SigningKey) -> None:
    stranger = SigningKey()

    with pytest.raises(ManifestError, match="not signed by a trusted key"):
        read_signed_manifest(signed(release(), stranger), [key.public])


def test_a_signature_filed_under_a_trusted_key_id_but_made_by_another_is_refused(
    key: SigningKey,
) -> None:
    stranger = SigningKey()
    payload = build_payload(release())
    envelope = build_envelope(payload, [(key.id, stranger.sign(payload))])

    with pytest.raises(ManifestError, match="not signed by a trusted key"):
        verify_envelope(envelope, [key.public])


def test_a_payload_changed_after_signing_is_refused(key: SigningKey) -> None:
    payload = build_payload(release("9.9.9"))
    altered = build_payload(release("9.9.10"))
    envelope = build_envelope(altered, [(key.id, key.sign(payload))])

    with pytest.raises(ManifestError, match="not signed by a trusted key"):
        verify_envelope(envelope, [key.public])


def test_an_unsigned_manifest_is_refused(key: SigningKey) -> None:
    with pytest.raises(ManifestError, match="not signed by a trusted key"):
        verify_envelope(build_envelope(build_payload(release()), []), [key.public])


def test_the_manifest_of_version_1_is_refused(key: SigningKey) -> None:
    """The unsigned file that 1.x reads must never be accepted by 2.x."""
    legacy = json.dumps({"latest_version": "9.9.9", "platforms": {}}).encode()

    with pytest.raises(ManifestError, match="not a signed envelope"):
        verify_envelope(legacy, [key.public])


def test_a_build_with_no_trusted_key_refuses_every_manifest(key: SigningKey) -> None:
    with pytest.raises(ManifestError, match="holds no key"):
        verify_envelope(signed(release(), key), [])


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not json",
        b"[]",
        b'{"payload": "AAAA"}',
        b'{"payload": 5, "signatures": []}',
        b'{"payload": "not base64!", "signatures": []}',
        b'{"payload": "AAAA", "signatures": "none"}',
        b"\xff\xfe",
    ],
)
def test_a_malformed_envelope_is_refused(data: bytes, key: SigningKey) -> None:
    with pytest.raises(ManifestError):
        verify_envelope(data, [key.public])


def test_malformed_signature_entries_are_passed_over(key: SigningKey) -> None:
    payload = build_payload(release())
    envelope = json.loads(key.envelope(payload))
    good = envelope["signatures"]
    envelope["signatures"] = [
        "text", {"key": key.id}, {"key": key.id, "signature": "AAAA"},
        {"key": key.id, "signature": "not base64!"}, {"key": 5, "signature": "AAAA"},
        {"key": [key.id], "signature": "AAAA"}, *good,
    ]

    assert verify_envelope(json.dumps(envelope).encode(), [key.public]) == payload

    envelope["signatures"] = envelope["signatures"][:-1]
    with pytest.raises(ManifestError, match="not signed by a trusted key"):
        verify_envelope(json.dumps(envelope).encode(), [key.public])


def test_an_oversized_manifest_is_refused_before_it_is_parsed(key: SigningKey) -> None:
    with pytest.raises(ManifestError, match="larger"):
        verify_envelope(b" " * (UPDATE_MANIFEST_MAX_BYTES + 1), [key.public])


def test_key_id_is_derived_from_the_key(key: SigningKey) -> None:
    other = SigningKey()

    assert key_id(key.public) == key_id(bytes(key.public))
    assert key_id(key.public) != key_id(other.public)
    assert len(key_id(key.public)) == 16


@pytest.mark.parametrize("encoded", ["", "AAAA", "not base64!", base64.b64encode(b"x" * 31).decode()])
def test_a_public_key_must_be_32_bytes_of_base64(encoded: str) -> None:
    with pytest.raises(ManifestError):
        decode_public_key(encoded)


def test_a_public_key_is_decoded(key: SigningKey) -> None:
    assert decode_public_key(key.public_base64) == key.public


# ── The payload ──────────────────────────────────────────────────────────


def test_payload_round_trip() -> None:
    manifest = release("2.4.6", notes="Line one\nLine two: ünïcode")

    assert parse_manifest(build_payload(manifest)) == manifest


def test_download_address_is_built_from_the_pinned_releases_address() -> None:
    manifest = release("2.4.6")

    assert manifest.download_url("windows-x86_64") == (
        f"{RELEASES_BASE_URL}/v2.4.6/{APP_NAME}-Setup-v2.4.6.exe"
    )
    assert manifest.download_url("linux-x86_64") == (
        f"{RELEASES_BASE_URL}/v2.4.6/{APP_NAME}-v2.4.6-x86_64.AppImage"
    )
    assert manifest.release_page.endswith("/releases/tag/v2.4.6")


def test_every_known_platform_has_a_file_name() -> None:
    for platform_key in PLATFORM_FILE_NAMES:
        assert "9.9.9" in expected_file_name(platform_key, "9.9.9")


def test_a_platform_this_version_does_not_know_is_left_out() -> None:
    data = json.loads(build_payload(release()))
    data["platforms"]["plan9-mips"] = {"file_name": "x", "file_size": 1, "sha256_hash": "0" * 64}

    assert set(parse_manifest(json.dumps(data).encode()).platforms) == set(PLATFORM_FILE_NAMES)


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"[]",
        _payload(format=1),
        _payload(format=MANIFEST_FORMAT + 1),
        _payload(format=True),
        _payload(product="AnotherProduct"),
        _payload(latest_version="9.9"),
        _payload(latest_version=9),
        _payload(min_version="oldest"),
        _payload(release_notes=None),
        _payload(platforms=[]),
        _payload(platforms={"windows-x86_64": "installer"}),
    ],
)
def test_a_payload_that_is_not_as_expected_is_refused(payload: bytes) -> None:
    with pytest.raises(ManifestError):
        parse_manifest(payload)


@pytest.mark.parametrize(
    "changes",
    [
        {"file_name": "WireTrace-Setup-v9.9.8.exe"},
        {"file_name": "../WireTrace-Setup-v9.9.9.exe"},
        {"file_name": "C:\\Windows\\WireTrace-Setup-v9.9.9.exe"},
        {"file_name": "https://example.com/WireTrace-Setup-v9.9.9.exe"},
        {"file_name": "WireTrace-Setup-v9.9.9.exe.bat"},
        {"file_name": None},
        {"file_size": 0},
        {"file_size": -1},
        {"file_size": "9"},
        {"file_size": True},
        {"file_size": 1.5},
        {"file_size": UPDATE_DOWNLOAD_MAX_BYTES + 1},
        {"sha256_hash": ""},
        {"sha256_hash": "0" * 63},
        {"sha256_hash": "G" * 64},
        {"sha256_hash": "A" * 64},
        {"sha256_hash": None},
    ],
)
def test_an_installer_entry_that_is_not_exact_is_refused(changes: dict[str, object]) -> None:
    with pytest.raises(ManifestError):
        parse_manifest(_platform_payload(**changes))
