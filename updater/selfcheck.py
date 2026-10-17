# SPDX-License-Identifier: GPL-3.0-or-later
"""A check, run by every build, that updates can be verified at all.

A build in which the signature library is missing or broken would refuse
every update, and its users would stay on that build for good. The
start-up self-check therefore verifies a known signature, and a build
made for release must also carry at least one key to trust.

The known signature is test vector 2 of RFC 8032, section 7.1.
"""

from __future__ import annotations

from updater.install import install_channel
from updater.manifest import (
    ManifestError,
    build_envelope,
    decode_public_key,
    key_id,
    verify_envelope,
)
from version import UPDATE_PUBLIC_KEYS

_VECTOR_PUBLIC_KEY = bytes.fromhex(
    "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c"
)
_VECTOR_MESSAGE = bytes.fromhex("72")
_VECTOR_SIGNATURE = bytes.fromhex(
    "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
    "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"
)
_SOURCE_CHANNEL = "source"


def check_signature_verification() -> int:
    """Verify a known signature, reject an altered one, and read the
    trusted keys. Returns how many keys this build trusts.

    Raises:
        ManifestError: verification does not work, a trusted key cannot
            be read, or a packaged build trusts no key.
    """
    keys = [_VECTOR_PUBLIC_KEY]
    envelope = build_envelope(_VECTOR_MESSAGE, [(key_id(_VECTOR_PUBLIC_KEY), _VECTOR_SIGNATURE)])
    if verify_envelope(envelope, keys) != _VECTOR_MESSAGE:
        raise ManifestError("a valid signature was not accepted")

    altered = build_envelope(
        _VECTOR_MESSAGE + _VECTOR_MESSAGE, [(key_id(_VECTOR_PUBLIC_KEY), _VECTOR_SIGNATURE)],
    )
    try:
        verify_envelope(altered, keys)
    except ManifestError:
        pass
    else:
        raise ManifestError("an altered message was accepted")

    trusted = [decode_public_key(key) for key in UPDATE_PUBLIC_KEYS]
    if not trusted and install_channel() != _SOURCE_CHANNEL:
        raise ManifestError("this build trusts no key, so it could never be updated")
    return len(trusted)
