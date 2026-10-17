# SPDX-License-Identifier: GPL-3.0-or-later
"""Fetching the manifest and downloading an installer, with nothing trusted.

Every request is to an ``https`` address, and a redirect is followed only
to another ``https`` address. An installer is written to a folder of the
application's own that only the user can read, under a name the
application built. It is accepted only if it has exactly the size and the
SHA-256 the signed manifest gave; until then it carries a temporary name,
so a file with the installer's name is always a verified one.

This module does not touch the GUI. Its functions block and are meant to
be called from a worker thread.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import stat
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

from app.constants import (
    UPDATE_DOWNLOAD_CHUNK_BYTES,
    UPDATE_DOWNLOAD_TIMEOUT_S,
    UPDATE_MANIFEST_MAX_BYTES,
    UPDATE_MANIFEST_TIMEOUT_S,
    UPDATE_STALE_DOWNLOAD_DAYS,
    get_cache_dir,
)

logger = logging.getLogger(__name__)

_HTTPS = "https://"
_CACHE_SUBDIRECTORY = "updates"
_PARTIAL_SUFFIX = ".part"
_PRIVATE_DIRECTORY = stat.S_IRWXU
_PRIVATE_FILE = stat.S_IRUSR | stat.S_IWUSR
_SECONDS_PER_DAY = 86_400

#: Names earlier versions gave the installers they left in the system's
#: temporary folder.
_LEGACY_DOWNLOAD_PATTERNS = ("WireTrace-Setup-*.exe", "WireTrace-*.dmg", "WireTrace-*.AppImage")


class DownloadError(Exception):
    """The server could not be reached, or what it sent was not accepted."""


class DownloadCancelled(Exception):
    """The download was stopped on request."""


class Opener(Protocol):
    """What this module needs of ``urllib``: tests supply their own."""

    def open(self, request: urllib.request.Request, timeout: float): ...


def require_https(url: str) -> None:
    """Refuse any address that is not ``https``.

    Raises:
        DownloadError: ``url`` uses another scheme.
    """
    if not isinstance(url, str) or not url.startswith(_HTTPS):
        raise DownloadError(f"refusing an address that is not https: {url!r}")


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to an ``https`` address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        require_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def default_opener() -> Opener:
    """An opener that verifies certificates and never leaves ``https``."""
    return urllib.request.build_opener(_HttpsOnlyRedirects)


def fetch(
    url: str,
    headers: Mapping[str, str],
    *,
    opener: Opener | None = None,
    max_bytes: int = UPDATE_MANIFEST_MAX_BYTES,
    timeout: float = UPDATE_MANIFEST_TIMEOUT_S,
) -> tuple[bytes, Mapping[str, str]]:
    """Fetch a small file. Returns its bytes and the headers of the answer.

    Raises:
        DownloadError: the address is not ``https``, the server could not
            be reached, or the answer is larger than ``max_bytes``.
    """
    require_https(url)
    request = urllib.request.Request(url, headers=dict(headers))
    try:
        with (opener or default_opener()).open(request, timeout=timeout) as response:
            data = response.read(max_bytes + 1)
            answer_headers = dict(response.headers.items())
    except DownloadError:
        raise
    except (OSError, ValueError) as exc:
        raise DownloadError(f"the server could not be reached: {exc}") from exc
    if len(data) > max_bytes:
        raise DownloadError("the server sent more than was asked for")
    return data, answer_headers


def send(
    url: str,
    body: bytes,
    headers: Mapping[str, str],
    *,
    opener: Opener | None = None,
    max_bytes: int,
    timeout: float,
) -> int:
    """Send ``body`` to ``url`` and return the status the server answered.

    An answer the server gave, refusal included, is returned as its
    status; only what it says, never more than ``max_bytes`` of it, is
    read.

    Raises:
        DownloadError: the address is not ``https``, or the server could
            not be reached.
    """
    require_https(url)
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with (opener or default_opener()).open(request, timeout=timeout) as response:
            response.read(max_bytes)
            return int(response.status)
    except DownloadError:
        raise
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (OSError, ValueError) as exc:
        raise DownloadError(f"the server could not be reached: {exc}") from exc


# ── The folder downloads are kept in ─────────────────────────────────────────


def update_cache_dir() -> Path:
    """The application's own folder for downloads, readable by the user only.

    Raises:
        DownloadError: the folder cannot be made, or made private.
    """
    directory = Path(get_cache_dir()) / _CACHE_SUBDIRECTORY
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # Has no effect on Windows, where the folder is inside the user's
        # profile and private to them already. Elsewhere it fails for a
        # folder that belongs to someone else, which must not be used.
        os.chmod(directory, _PRIVATE_DIRECTORY)
    except OSError as exc:
        raise DownloadError(f"the folder for downloads cannot be used: {exc}") from exc
    return directory


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        while chunk := file.read(UPDATE_DOWNLOAD_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def is_verified_file(path: Path, size: int, sha256: str) -> bool:
    """Whether ``path`` exists with exactly this size and SHA-256."""
    try:
        return path.is_file() and path.stat().st_size == size and _sha256_of(path) == sha256
    except OSError:
        return False


def download(
    url: str,
    destination: Path,
    size: int,
    sha256: str,
    headers: Mapping[str, str],
    *,
    progress: Callable[[int, int], None] = lambda done, total: None,
    cancelled: Callable[[], bool] = lambda: False,
    opener: Opener | None = None,
    timeout: float = UPDATE_DOWNLOAD_TIMEOUT_S,
) -> Path:
    """Download ``url`` to ``destination`` and accept it only if it is exact.

    The bytes are written beside ``destination`` under a temporary name.
    Reading stops at ``size``: a server that sends more is refused without
    the rest being read. The file takes its real name only after its
    size and SHA-256 have both matched.

    Raises:
        DownloadCancelled: ``cancelled`` returned True.
        DownloadError: anything else that kept an exact file from arriving.
    """
    require_https(url)
    partial = destination.with_name(destination.name + _PARTIAL_SUFFIX)
    request = urllib.request.Request(url, headers=dict(headers))
    digest = hashlib.sha256()
    received = 0
    try:
        with (opener or default_opener()).open(request, timeout=timeout) as response:
            descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _PRIVATE_FILE)
            with os.fdopen(descriptor, "wb") as file:
                while True:
                    if cancelled():
                        raise DownloadCancelled
                    chunk = response.read(min(UPDATE_DOWNLOAD_CHUNK_BYTES, size - received + 1))
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > size:
                        raise DownloadError("the download is larger than the release says")
                    file.write(chunk)
                    digest.update(chunk)
                    progress(received, size)
        if received != size:
            raise DownloadError("the download is smaller than the release says")
        if digest.hexdigest() != sha256:
            raise DownloadError("the download does not match the release")
        os.replace(partial, destination)
    except (DownloadCancelled, DownloadError):
        _remove(partial)
        raise
    except (OSError, ValueError) as exc:
        _remove(partial)
        raise DownloadError(f"the download failed: {exc}") from exc
    return destination


def _remove(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


# ── Housekeeping ─────────────────────────────────────────────────────────


def remove_other_downloads(keep: Path | None = None) -> None:
    """Remove every file in the download folder except ``keep``."""
    for path in update_cache_dir().iterdir():
        if path != keep and path.is_file():
            _remove(path)


def remove_legacy_downloads() -> None:
    """Remove old installers that earlier versions left in the system's
    temporary folder. Recent ones are left: another copy may be using one."""
    oldest_kept = time.time() - UPDATE_STALE_DOWNLOAD_DAYS * _SECONDS_PER_DAY
    folder = Path(tempfile.gettempdir())
    for pattern in _LEGACY_DOWNLOAD_PATTERNS:
        for path in folder.glob(pattern):
            try:
                if path.is_file() and path.stat().st_mtime < oldest_kept:
                    path.unlink()
            except OSError:
                logger.debug("An old installer was left in place: %s", path)
