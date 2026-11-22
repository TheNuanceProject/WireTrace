# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests of fetching the manifest and downloading an installer.

The server is a stand-in, so nothing here touches the network. What is
tested is that only an exact file is ever accepted, and that nothing is
left behind when one is not.
"""

from __future__ import annotations

import datetime
import hashlib
import http.server
import ipaddress
import os
import ssl
import stat
import threading
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import truststore
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

import updater.download as download_module
from app.constants import UPDATE_DOWNLOAD_CHUNK_BYTES, UPDATE_STALE_DOWNLOAD_DAYS
from tests.update_helpers import FakeServer
from updater.download import (
    DownloadCancelled,
    DownloadError,
    DownloadRejected,
    DownloadStorageError,
    DownloadUnreachable,
    default_opener,
    download,
    fetch,
    is_verified_file,
    remove_legacy_downloads,
    remove_other_downloads,
    require_https,
    tls_context,
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


# ── Certificates ─────────────────────────────────────────────────────────
#
# A real TLS server on this computer, with certificates made for the test:
# what the operating system does not trust, or a certificate for another
# name, is refused; what it is told to trust is accepted.


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _certificate(subject: str, key: ec.EllipticCurvePrivateKey, issuer: str,
                 issuer_key: ec.EllipticCurvePrivateKey, *, ca: bool,
                 names: list[x509.GeneralName] | None = None) -> x509.Certificate:
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(subject))
        .issuer_name(_name(issuer))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
            critical=False,
        )
    )
    if ca:
        builder = builder.add_extension(
            x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                          content_commitment=False, key_encipherment=False,
                          data_encipherment=False, key_agreement=False,
                          encipher_only=False, decipher_only=False),
            critical=True,
        )
    else:
        builder = builder.add_extension(x509.SubjectAlternativeName(names or []), critical=False)
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False,
        )
    return builder.sign(issuer_key, hashes.SHA256())


def _pem(path: Path, *items: object) -> Path:
    data = b""
    for item in items:
        if isinstance(item, x509.Certificate):
            data += item.public_bytes(serialization.Encoding.PEM)
        else:
            data += item.private_bytes(  # type: ignore[attr-defined]
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
    path.write_bytes(data)
    return path


class _Answer(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"verified"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@contextmanager
def _serving(certificate: Path) -> Iterator[str]:
    """An HTTPS server on this computer showing ``certificate``; yields its
    address."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate)
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Answer)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    # A refused handshake is the point of some tests, not an error to print.
    httpd.handle_error = lambda request, client_address: None  # type: ignore[method-assign]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://localhost:{httpd.server_address[1]}/file"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


@pytest.fixture
def tls_server(tmp_path: Path) -> Iterator[tuple[str, Path, Path]]:
    """A local HTTPS server, the CA its certificate is signed by, and a
    certificate from that CA for another name.

    Yields the server's address, the CA's file, and the other certificate's
    file (with its key), for a server that names itself wrongly.
    """
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = _certificate("WireTrace test CA", ca_key, "WireTrace test CA", ca_key, ca=True)
    server_key = ec.generate_private_key(ec.SECP256R1())
    names = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    leaf = _certificate("localhost", server_key, "WireTrace test CA", ca_key, ca=False, names=names)
    other_key = ec.generate_private_key(ec.SECP256R1())
    other = _certificate("other.invalid", other_key, "WireTrace test CA", ca_key, ca=False,
                         names=[x509.DNSName("other.invalid")])
    with _serving(_pem(tmp_path / "server.pem", leaf, server_key)) as url:
        yield url, _pem(tmp_path / "ca.pem", ca), _pem(tmp_path / "other.pem", other, other_key)


def test_certificates_are_verified_by_the_operating_system() -> None:
    context = tls_context()

    assert isinstance(context, truststore.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_the_default_opener_verifies_with_the_operating_system() -> None:
    handlers = [h for h in default_opener().handlers if isinstance(h, urllib.request.HTTPSHandler)]

    assert len(handlers) == 1
    assert isinstance(handlers[0]._context, truststore.SSLContext)


def test_a_server_the_system_does_not_trust_is_refused(tls_server: tuple[str, Path, Path]) -> None:
    url, _, _ = tls_server

    with pytest.raises(DownloadError, match="certificate"):
        fetch(url, {"User-Agent": "test"}, timeout=10)


def _trusting(ca: Path) -> urllib.request.OpenerDirector:
    context = tls_context()
    context.load_verify_locations(cafile=str(ca))
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context), download_module._HttpsOnlyRedirects,
    )


def test_a_server_with_a_trusted_certificate_is_accepted(tls_server: tuple[str, Path, Path]) -> None:
    url, ca, _ = tls_server

    data, _ = fetch(url, {"User-Agent": "test"}, opener=_trusting(ca), timeout=10)

    assert data == b"verified"


def test_a_certificate_for_another_name_is_refused(tls_server: tuple[str, Path, Path]) -> None:
    _, ca, other = tls_server

    with _serving(other) as url, pytest.raises(DownloadError, match="certificate"):
        fetch(url, {"User-Agent": "test"}, opener=_trusting(ca), timeout=10)


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


def test_a_download_ended_by_anything_else_leaves_nothing(server: FakeServer, cache: Path) -> None:
    # For example, the window that showed its progress was closed.
    def report(done: int, total: int) -> None:
        raise RuntimeError("the window showing the progress has gone")

    with pytest.raises(RuntimeError):
        _download(server, cache, progress=report)

    assert list(cache.iterdir()) == []


def test_a_failed_connection_leaves_nothing(server: FakeServer, cache: Path) -> None:
    server.error = OSError("connection reset")

    with pytest.raises(DownloadUnreachable, match="could not be reached"):
        _download(server, cache)

    assert list(cache.iterdir()) == []


class _LostAfterFirstChunk:
    """An answer whose connection is lost after its first chunk."""

    def __init__(self) -> None:
        self._sent = False

    def __enter__(self):
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self, amount: int) -> bytes:
        if self._sent:
            raise OSError("connection reset")
        self._sent = True
        return CONTENT[:amount]


class _LosingOpener:
    def open(self, request, timeout):
        return _LostAfterFirstChunk()


def test_a_connection_lost_during_the_download_is_unreachable(cache: Path) -> None:
    with pytest.raises(DownloadUnreachable, match="connection was lost"):
        _download(None, cache, opener=_LosingOpener())  # type: ignore[arg-type]

    assert list(cache.iterdir()) == []


def test_a_download_that_does_not_match_is_rejected(server: FakeServer, cache: Path) -> None:
    server.pages[URL] = b"tampered"

    with pytest.raises(DownloadRejected):
        _download(server, cache)


def test_a_download_that_cannot_be_written_is_a_storage_error(server: FakeServer, cache: Path) -> None:
    (cache / "installer.bin.part").mkdir()

    with pytest.raises(DownloadStorageError, match="could not be written"):
        _download(server, cache)


def test_an_address_that_is_not_https_is_rejected() -> None:
    with pytest.raises(DownloadRejected):
        require_https("http://example.invalid/file")


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
