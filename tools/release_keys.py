# SPDX-License-Identifier: GPL-3.0-or-later
"""Make and read the keys that update manifests are signed with.

An installed WireTrace acts on an update manifest only if it carries a
signature from a key whose public half is in ``UPDATE_PUBLIC_KEYS`` in
``version.py``. The private half is made here, protected by a
passphrase, and written to a file the maintainer keeps. It must
never be put in the repository, on GitHub or in a build.

Two keys are made, each in its own run: one to work with, and a spare
kept somewhere else. If the working key is lost, the spare signs the
release that replaces it.

Usage:
    python tools/release_keys.py new PATH     # make a key and write it to PATH
    python tools/release_keys.py show PATH    # print the public half of the key at PATH
"""

from __future__ import annotations

import argparse
import base64
import getpass
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from updater.manifest import key_id

MIN_PASSPHRASE_LENGTH = 12
_OWNER_ONLY = 0o600
_ENCODING = "utf-8"


class SigningKeyError(Exception):
    """A key could not be made, written or read."""


def public_bytes(key: Ed25519PrivateKey) -> bytes:
    """The 32 bytes of the public half of ``key``."""
    return key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )


def public_text(key: Ed25519PrivateKey) -> str:
    """The public half of ``key`` as ``version.py`` holds it."""
    return base64.b64encode(public_bytes(key)).decode("ascii")


def is_inside_repository(path: Path) -> bool:
    return path.resolve().is_relative_to(ROOT)


def write_new_key(path: Path, passphrase: str) -> Ed25519PrivateKey:
    """Make a key and write it to ``path``, encrypted with ``passphrase``.

    Raises:
        SigningKeyError: the passphrase is too short, ``path`` is inside the
            repository or already exists, or it cannot be written.
    """
    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise SigningKeyError(f"the passphrase must be at least {MIN_PASSPHRASE_LENGTH} characters")
    if is_inside_repository(path):
        raise SigningKeyError("a private key must not be kept inside the repository")

    key = Ed25519PrivateKey.generate()
    encoded = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(passphrase.encode(_ENCODING)),
    )
    try:
        # Created anew and for the owner only; an existing file is never
        # written over.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _OWNER_ONLY)
    except FileExistsError as exc:
        raise SigningKeyError(f"{path} already exists; it was left as it is") from exc
    except OSError as exc:
        raise SigningKeyError(f"{path} could not be created: {exc}") from exc
    with os.fdopen(descriptor, "wb") as file:
        file.write(encoded)
    return key


def load_key(path: Path, passphrase: str) -> Ed25519PrivateKey:
    """Read the key at ``path``.

    Raises:
        SigningKeyError: the file cannot be read, the passphrase is wrong, or
            the file does not hold an Ed25519 key.
    """
    try:
        encoded = path.read_bytes()
    except OSError as exc:
        raise SigningKeyError(f"{path} could not be read: {exc}") from exc
    try:
        key = serialization.load_pem_private_key(encoded, passphrase.encode(_ENCODING))
    except (ValueError, TypeError) as exc:
        raise SigningKeyError("the passphrase is wrong, or the file is not a key") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise SigningKeyError("the file does not hold an Ed25519 key")
    return key


def describe(key: Ed25519PrivateKey) -> str:
    return f"Key id:      {key_id(public_bytes(key))}\nPublic key:  {public_text(key)}"


def _ask_new_passphrase() -> str:
    passphrase = getpass.getpass("Passphrase for the new key: ")
    if getpass.getpass("The same passphrase again: ") != passphrase:
        raise SigningKeyError("the two passphrases differ")
    return passphrase


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (("new", "make a key"), ("show", "print the public half of a key")):
        command = commands.add_parser(name, help=text)
        command.add_argument("path", type=Path, help="the file of the private key")
    options = parser.parse_args(arguments)

    try:
        if options.command == "new":
            key = write_new_key(options.path, _ask_new_passphrase())
            print(f"\nThe private key was written to {options.path.resolve()}")
            print("Keep the file safe, and the passphrase apart from it. Neither can be recovered.\n")
            print(describe(key))
            print("\nAdd the public key to UPDATE_PUBLIC_KEYS in version.py.")
        else:
            print(describe(load_key(options.path, getpass.getpass("Passphrase: "))))
    except SigningKeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
