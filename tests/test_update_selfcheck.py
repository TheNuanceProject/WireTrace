# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests of the check every build runs: that signatures verify at all,
and that a build made for release trusts at least one key."""

from __future__ import annotations

import pytest

import updater.selfcheck as selfcheck
from tests.update_helpers import SigningKey
from updater.manifest import ManifestError
from updater.selfcheck import check_signature_verification
from version import UPDATE_PUBLIC_KEYS


def test_the_standard_test_vector_verifies() -> None:
    assert check_signature_verification() == len(UPDATE_PUBLIC_KEYS)


def test_a_packaged_build_without_a_key_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(selfcheck, "UPDATE_PUBLIC_KEYS", ())
    monkeypatch.setattr(selfcheck, "install_channel", lambda: "exe")

    with pytest.raises(ManifestError, match="trusts no key"):
        check_signature_verification()


def test_a_packaged_build_with_a_key_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    key = SigningKey()
    monkeypatch.setattr(selfcheck, "UPDATE_PUBLIC_KEYS", (key.public_base64,))
    monkeypatch.setattr(selfcheck, "install_channel", lambda: "exe")

    assert check_signature_verification() == 1


def test_a_key_that_cannot_be_read_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(selfcheck, "UPDATE_PUBLIC_KEYS", ("not a key",))

    with pytest.raises(ManifestError):
        check_signature_verification()


def test_a_broken_signature_library_is_noticed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(selfcheck, "verify_envelope", lambda data, keys: b"anything")

    with pytest.raises(ManifestError):
        check_signature_verification()


def test_embedded_keys_are_readable_and_distinct() -> None:
    from updater.manifest import decode_public_key

    decoded = [decode_public_key(key) for key in UPDATE_PUBLIC_KEYS]
    assert len(set(decoded)) == len(decoded)
