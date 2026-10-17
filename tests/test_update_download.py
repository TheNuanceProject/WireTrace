# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests of fetching the manifest and downloading an installer.

The server is a stand-in, so nothing here touches the network. What is
tested is that only an exact file is ever accepted, and that nothing is
left behind when one is not.
"""

from __future__ import annotations

import hashlib
import os
import stat
import time
import urllib.request
from pathlib import Path

import pytest

import updater.download as download_module
from app.constants import UPDATE_DOWNLOAD_CHUNK_BYTES, UPDATE_STALE_DOWNLOAD_DAYS
from tests.update_helpers import FakeServer
from updater.download import (
    DownloadCancelled,
    DownloadError,
    download,
    fetch,
    is_verified_file,
    remove_legacy_downloads,
    remove_other_downloads,
    require_https,
    update_cache_dir,
)

URL = "https://example.invalid/file"
CONTENT = bytes(range(256)) * 1024  # larger than one chunk
SHA256 = hashlib.sha256(CONTENT).hexdigest()


@pytest.fixture
def server() -> FakeServer:
    fake = FakeServer()
    fake.pages[URL] = CONTENT
    return fake


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(download_module, "get_cache_dir", lambda: str(tmp_path / "cache"))
    return update_cache_dir()


def _download(server: FakeServer, cache: Path, **changes: object) -> Path:
    arguments = {
        "url": URL, "destination": cache / "installer.bin", "size": len(CONTENT),
        "sha256": SHA256, "headers": {"User-Agent": "test"}, "opener": server,
    }
    arguments.update(changes)
    return download(**arguments)


# ── Addresses ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    ["http://example.invalid/x", "file:///etc/passwd", "ftp://example.invalid/x", "", None,
     "HTTPS://example.invalid/x", " https://example.invalid/x", "javascript:alert(1)"],
)
def test_only_https_is_accepted(url: object) -> None:
    with pytest.raises(DownloadError):
        require_https(url)


def test_a_redirect_away_from_https_is_refused() -> None:
    handler = download_module._HttpsOnlyRedirects()
    request = urllib.request.Request(URL)

    with pytest.raises(DownloadError):
        handler.redirect_request(request, None, 302, "Found", {}, "http://example.invalid/x")

    followed = handler.redirect_request(
        request, None, 302, "Found", {}, "https://other.invalid/x",
    )
    assert followed.full_url == "https://other.invalid/x"


# ── Small files ──────────────────────────────────────────────────────────


def test_fetch_returns_the_bytes_and_the_headers(server: FakeServer) -> None:
    server.pages[URL] = b"manifest"
    server.headers = {"X-Something": "7"}

    data, headers = fetch(URL, {"User-Agent": "test/1"}, opener=server)

    assert data == b"manifest"
    assert headers == {"X-Something": "7"}
    assert server.sent("User-Agent") == ["test/1"]


def test_fetch_refuses_an_answer_larger_than_allowed(server: FakeServer) -> None:
    server.pages[URL] = b"x" * 11

    with pytest.raises(DownloadError, match="more than was asked for"):
        fetch(URL, {}, opener=server, max_bytes=10)

    assert fetch(URL, {}, opener=server, max_bytes=11)[0] == b"x" * 11


def test_fetch_reports_a_server_that_cannot_be_reached(server: FakeServer) -> None:
    server.error = OSError("no route to host")

    with pytest.raises(DownloadError, match="could not be reached"):
        fetch(URL, {}, opener=server)


def test_fetch_never_asks_a_server_that_is_not_https(server: FakeServer) -> None:
    with pytest.raises(DownloadError):
        fetch("http://example.invalid/file", {}, opener=server)

    assert server.requests == []


# ── Downloading ──────────────────────────────────────────────────────────


def test_an_exact_file_is_accepted(server: FakeServer, cache: Path) -> None:
    seen: list[tuple[int, int]] = []

    path = _download(server, cache, progress=lambda done, total: seen.append((done, total)))

    assert path.read_bytes() == CONTENT
    assert seen[-1] == (len(CONTENT), len(CONTENT))
    assert len(seen) == -(-len(CONTENT) // UPDATE_DOWNLOAD_CHUNK_BYTES)
    assert [item.name for item in cache.iterdir()] == ["installer.bin"]


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"size": len(CONTENT) - 1}, "larger"),
        ({"size": len(CONTENT) + 1}, "smaller"),
        ({"sha256": "0" * 64}, "does not match"),
    ],
)
def test_a_file_that_is_not_exact_is_refused_and_removed(
    server: FakeServer, cache: Path, changes: dict[str, object], reason: str,
) -> None:
    with pytest.raises(DownloadError, match=reason):
        _download(server, cache, **changes)

    assert list(cache.iterdir()) == []


def test_reading_stops_as_soon_as_the_file_is_too_large(server: FakeServer, cache: Path) -> None:
    server.pages[URL] = CONTENT * 4
    read: list[int] = []
    original = server.open

    def counting(request, timeout):
        answer = original(request, timeout)
        plain_read = answer.read
        answer.read = lambda count: (read.append(count), plain_read(count))[1]
        return answer

    server.open = counting

    with pytest.raises(DownloadError, match="larger"):
        _download(server, cache)

    assert sum(read) <= len(CONTENT) + 1


def test_a_cancelled_download_leaves_nothing(server: FakeServer, cache: Path) -> None:
    chunks: list[int] = []

    with pytest.raises(DownloadCancelled):
        _download(
            server, cache,
            progress=lambda done, total: chunks.append(done),
            cancelled=lambda: len(chunks) >= 1,
        )

    assert len(chunks) == 1
    assert list(cache.iterdir()) == []


def test_a_failed_connection_leaves_nothing(server: FakeServer, cache: Path) -> None:
    server.error = OSError("connection reset")

    with pytest.raises(DownloadError, match="download failed"):
        _download(server, cache)

    assert list(cache.iterdir()) == []


def test_a_failed_download_does_not_remove_an_earlier_verified_file(
    server: FakeServer, cache: Path,
) -> None:
    good = _download(server, cache)
    server.pages[URL] = b"tampered"

    with pytest.raises(DownloadError):
        _download(server, cache)

    assert good.read_bytes() == CONTENT


def test_download_never_asks_a_server_that_is_not_https(server: FakeServer, cache: Path) -> None:
    with pytest.raises(DownloadError):
        _download(server, cache, url="http://example.invalid/file")

    assert server.requests == []
    assert list(cache.iterdir()) == []


# ── The folder ───────────────────────────────────────────────────────────


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions")
def test_downloads_are_readable_by_the_user_only(server: FakeServer, cache: Path) -> None:
    path = _download(server, cache)

    assert stat.S_IMODE(cache.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_verified_file_is_recognised(server: FakeServer, cache: Path) -> None:
    path = _download(server, cache)

    assert is_verified_file(path, len(CONTENT), SHA256)
    assert not is_verified_file(path, len(CONTENT) + 1, SHA256)
    assert not is_verified_file(path, len(CONTENT), "0" * 64)
    assert not is_verified_file(cache / "missing.bin", len(CONTENT), SHA256)


def test_other_downloads_are_removed_and_the_named_one_kept(cache: Path) -> None:
    keep = cache / "keep.bin"
    for path in (keep, cache / "old.bin", cache / "older.bin.part"):
        path.write_bytes(b"x")

    remove_other_downloads(keep)
    assert [item.name for item in cache.iterdir()] == ["keep.bin"]

    remove_other_downloads()
    assert list(cache.iterdir()) == []


def test_old_installers_of_earlier_versions_are_removed_from_the_temp_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(download_module.tempfile, "gettempdir", lambda: str(tmp_path))
    old = time.time() - (UPDATE_STALE_DOWNLOAD_DAYS + 1) * 86_400
    stale = tmp_path / "WireTrace-Setup-v1.2.0.exe"
    recent = tmp_path / "WireTrace-v1.2.0-x86_64.AppImage"
    unrelated = tmp_path / "Other-Setup-v1.0.0.exe"
    for path in (stale, recent, unrelated):
        path.write_bytes(b"x")
    os.utime(stale, (old, old))
    os.utime(unrelated, (old, old))

    remove_legacy_downloads()

    assert sorted(item.name for item in tmp_path.iterdir()) == sorted([recent.name, unrelated.name])
