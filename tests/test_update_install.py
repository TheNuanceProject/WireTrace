# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests of how an installation is recognised and how an update is put
in place: the Windows installer, the AppImage that replaces itself, and
the installs that are sent to the release page."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

import updater.install as install
from app.constants import (
    INSTALLER_CHOICES_FILE,
    INSTALLER_CHOICES_SECTION,
    INSTALLER_UPDATE_CHECK_KEY,
)
from updater.install import (
    InstallKind,
    UpdateApplyError,
    architecture,
    install_channel,
    install_kind,
    installer_update_choice,
    launch_update,
    platform_key,
    prepare_update,
    replace_appimage,
    system_name,
    user_agent,
)
from updater.manifest import PLATFORM_FILE_NAMES, PlatformRelease
from version import APP_NAME, APP_VERSION

_MOUNT = "/tmp/.mount_WireTr"


def _release(content: bytes) -> PlatformRelease:
    """What a manifest would say of a download with this content."""
    return PlatformRelease(
        file_name="download", file_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _as(monkeypatch: pytest.MonkeyPatch, system: str, machine: str = "x86_64",
        *, compiled: bool = False, appimage: str = "") -> None:
    """Make the process look like a given installation."""
    monkeypatch.setattr(install.platform, "system", lambda: system)
    monkeypatch.setattr(install.platform, "machine", lambda: machine)
    if compiled:
        monkeypatch.setitem(install.__dict__, "__compiled__", True)
    else:
        monkeypatch.delitem(install.__dict__, "__compiled__", raising=False)
    if appimage:
        # As the AppImage runtime starts a program: the file's path, the
        # folder its content is mounted at, and the program inside it.
        monkeypatch.setenv("APPIMAGE", appimage)
        monkeypatch.setenv("APPDIR", _MOUNT)
        monkeypatch.setattr(install.sys, "executable", f"{_MOUNT}/usr/bin/wiretrace")
    else:
        monkeypatch.delenv("APPIMAGE", raising=False)
        monkeypatch.delenv("APPDIR", raising=False)


# ── What this installation is ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("system", "machine", "key"),
    [
        ("Windows", "AMD64", "windows-x86_64"),
        ("Linux", "x86_64", "linux-x86_64"),
        ("Linux", "aarch64", "linux-aarch64"),
        ("Darwin", "arm64", "macos-aarch64"),
        ("Darwin", "x86_64", "macos-x86_64"),
        ("FreeBSD", "riscv64", "freebsd-riscv64"),
    ],
)
def test_platform_is_named_by_system_and_processor(
    monkeypatch: pytest.MonkeyPatch, system: str, machine: str, key: str,
) -> None:
    _as(monkeypatch, system, machine)

    assert platform_key() == key
    assert f"{system_name()}-{architecture()}" == key


def test_every_published_platform_is_one_this_code_can_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    named = set()
    for system, machine in (("Windows", "AMD64"), ("Linux", "x86_64")):
        _as(monkeypatch, system, machine)
        named.add(platform_key())

    assert named == set(PLATFORM_FILE_NAMES)


@pytest.mark.parametrize(
    ("system", "compiled", "appimage", "channel", "kind"),
    [
        ("Windows", True, "", "exe", InstallKind.INSTALLER),
        ("Linux", True, "/opt/WireTrace.AppImage", "appimage", InstallKind.APPIMAGE),
        ("Linux", True, "", "deb", InstallKind.MANUAL),
        ("Darwin", True, "", "app", InstallKind.MANUAL),
        ("Windows", False, "", "source", InstallKind.MANUAL),
        ("Linux", False, "", "source", InstallKind.MANUAL),
        ("FreeBSD", True, "", "packaged", InstallKind.MANUAL),
    ],
)
def test_install_channel_and_kind(
    monkeypatch: pytest.MonkeyPatch, system: str, compiled: bool, appimage: str,
    channel: str, kind: InstallKind,
) -> None:
    _as(monkeypatch, system, compiled=compiled, appimage=appimage)

    assert install_channel() == channel
    assert install_kind() is kind


def test_user_agent_names_version_system_processor_and_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _as(monkeypatch, "Windows", "AMD64", compiled=True)

    assert user_agent() == f"{APP_NAME}/{APP_VERSION} (windows; x86_64; exe)"


# ── Putting an update in place ───────────────────────────────────────────────


@pytest.fixture
def images(tmp_path: Path) -> tuple[Path, Path]:
    running = tmp_path / "WireTrace-running.AppImage"
    running.write_bytes(b"OLD")
    downloaded = tmp_path / "cache" / "WireTrace-v9.9.9-x86_64.AppImage"
    downloaded.parent.mkdir()
    downloaded.write_bytes(b"NEW")
    return running, downloaded


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions")
def test_appimage_is_replaced_and_made_executable(images: tuple[Path, Path]) -> None:
    running, downloaded = images

    replace_appimage(downloaded, running, _release(b"NEW"))

    assert running.read_bytes() == b"NEW"
    assert running.stat().st_mode & stat.S_IXUSR
    assert [path.name for path in running.parent.iterdir() if path.name.startswith(".")] == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions")
def test_appimage_in_a_folder_that_cannot_be_written_is_reported(
    images: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.geteuid() == 0:
        pytest.skip("running as root bypasses directory permissions")
    running, downloaded = images
    _as(monkeypatch, "Linux", compiled=True, appimage=str(running))
    os.chmod(running.parent, 0o500)
    try:
        with pytest.raises(UpdateApplyError):
            prepare_update(downloaded, _release(b"NEW"))
    finally:
        os.chmod(running.parent, 0o700)
    assert running.read_bytes() == b"OLD"


def test_preparing_an_appimage_replaces_it_and_launching_starts_it(
    images: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    running, downloaded = images
    _as(monkeypatch, "Linux", compiled=True, appimage=str(running))
    started: list[list[str]] = []
    monkeypatch.setattr(install.subprocess, "Popen", lambda arguments: started.append(arguments))

    prepare_update(downloaded, _release(b"NEW"))
    assert running.read_bytes() == b"NEW"
    assert started == []

    launch_update(downloaded, _release(b"NEW"))
    assert started == [[str(running)]]


def test_the_windows_installer_is_run_only_at_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _as(monkeypatch, "Windows", "AMD64", compiled=True)
    installer = tmp_path / "WireTrace-Setup-v9.9.9.exe"
    installer.write_bytes(b"SETUP")
    run: list[Path] = []
    monkeypatch.setattr(install.os, "startfile", run.append, raising=False)

    prepare_update(installer, _release(b"SETUP"))
    assert run == []

    launch_update(installer, _release(b"SETUP"))
    assert run == [installer]


def test_an_installer_changed_after_it_was_verified_is_never_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _as(monkeypatch, "Windows", "AMD64", compiled=True)
    installer = tmp_path / "WireTrace-Setup-v9.9.9.exe"
    installer.write_bytes(b"SETUP")
    run: list[Path] = []
    monkeypatch.setattr(install.os, "startfile", run.append, raising=False)
    prepare_update(installer, _release(b"SETUP"))

    # Swapped while the application was closing its sessions.
    installer.write_bytes(b"OTHER")

    with pytest.raises(UpdateApplyError, match="changed after it was checked"):
        launch_update(installer, _release(b"SETUP"))
    with pytest.raises(UpdateApplyError, match="changed after it was checked"):
        prepare_update(installer, _release(b"SETUP"))
    assert run == []


def test_an_appimage_changed_after_it_was_verified_is_not_put_in_place(
    images: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    running, downloaded = images
    _as(monkeypatch, "Linux", compiled=True, appimage=str(running))
    downloaded.write_bytes(b"BAD")

    with pytest.raises(UpdateApplyError, match="changed after it was checked"):
        prepare_update(downloaded, _release(b"NEW"))

    assert running.read_bytes() == b"OLD"
    assert [path.name for path in running.parent.iterdir() if path.name.startswith(".")] == []


def test_another_programs_appimage_is_never_taken_for_this_one(
    images: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Started from inside another application's AppImage, a program
    inherits that AppImage's variables. They are not this program's."""
    other, downloaded = images
    _as(monkeypatch, "Linux", compiled=True, appimage=str(other))
    monkeypatch.setattr(install.sys, "executable", "/opt/wiretrace/wiretrace")

    assert install.running_appimage() is None
    assert install_channel() == "deb"
    assert install_kind() is InstallKind.MANUAL
    with pytest.raises(UpdateApplyError, match="cannot update itself"):
        prepare_update(downloaded, _release(b"NEW"))
    assert other.read_bytes() == b"OLD"


def test_run_from_source_inside_an_appimage_is_still_from_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _as(monkeypatch, "Linux", compiled=False)
    monkeypatch.setenv("APPIMAGE", "/opt/Editor.AppImage")
    monkeypatch.setenv("APPDIR", "/tmp/.mount_Editor")

    assert install_channel() == "source"


def test_an_installer_that_cannot_be_started_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _as(monkeypatch, "Windows", "AMD64", compiled=True)

    def refuse(path: Path) -> None:
        raise OSError("access denied")

    monkeypatch.setattr(install.os, "startfile", refuse, raising=False)
    installer = tmp_path / "WireTrace-Setup-v9.9.9.exe"
    installer.write_bytes(b"SETUP")

    with pytest.raises(UpdateApplyError, match="access denied"):
        launch_update(installer, _release(b"SETUP"))


@pytest.mark.parametrize("system", ["Linux", "Darwin", "Windows"])
def test_an_installation_that_cannot_update_itself_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system: str,
) -> None:
    _as(monkeypatch, system)  # run from source

    with pytest.raises(UpdateApplyError, match="cannot update itself"):
        prepare_update(tmp_path / "installer", _release(b""))


def test_the_installer_mutex_is_only_for_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install.sys, "platform", "linux")

    assert install.hold_installer_mutex() is False


# ── The answer the Windows installer recorded ────────────────────────────────


class TestInstallerUpdateChoice:
    @pytest.fixture
    def installed(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        """An installation made by the Windows installer, in ``tmp_path``."""
        _as(monkeypatch, "Windows", "AMD64", compiled=True)
        monkeypatch.setattr(install.sys, "executable", str(tmp_path / f"{APP_NAME}.exe"))
        return tmp_path / INSTALLER_CHOICES_FILE

    def _write(self, path: Path, value: str) -> None:
        path.write_text(
            f"[{INSTALLER_CHOICES_SECTION}]\n{INSTALLER_UPDATE_CHECK_KEY}={value}\n",
            encoding="utf-8",
        )

    @pytest.mark.parametrize(("value", "expected"), [("true", True), ("false", False)])
    def test_the_recorded_answer_is_read(self, installed, value, expected):
        self._write(installed, value)
        assert installer_update_choice() is expected

    def test_a_file_with_a_byte_order_mark_is_read(self, installed):
        installed.write_bytes(
            b"\xef\xbb\xbf"
            + f"[{INSTALLER_CHOICES_SECTION}]\r\n{INSTALLER_UPDATE_CHECK_KEY}=false\r\n".encode()
        )
        assert installer_update_choice() is False

    def test_no_file_means_the_installer_did_not_ask(self, installed):
        assert installer_update_choice() is None

    @pytest.mark.parametrize("content", ["", "not an ini file", "[Updates]\ncheck=perhaps\n",
                                         "[Other]\ncheck=true\n"])
    def test_a_file_that_gives_no_clear_answer_counts_as_not_asked(self, installed, content):
        installed.write_text(content, encoding="utf-8")
        assert installer_update_choice() is None

    def test_other_kinds_of_installation_never_read_it(self, monkeypatch, tmp_path):
        _as(monkeypatch, "Linux", compiled=True, appimage="/opt/WireTrace.AppImage")
        monkeypatch.setattr(install.sys, "executable", str(tmp_path / "wiretrace"))
        self._write(tmp_path / INSTALLER_CHOICES_FILE, "true")
        assert installer_update_choice() is None
