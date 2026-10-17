# SPDX-License-Identifier: GPL-3.0-or-later
"""What kind of installation this is, and how an update is put in place.

How an update can be applied depends on how the application was
installed:

  - From the Windows installer: the new installer is run.
  - As a Linux AppImage: the running file is replaced by the new one.
  - Any other way (a ``.deb``, from source, on macOS): the application
    cannot replace itself, so the user is sent to the release page.

This module does not touch the GUI or the network.
"""

from __future__ import annotations

import configparser
import contextlib
import enum
import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from app.constants import (
    INSTALLER_CHOICES_FILE,
    INSTALLER_CHOICES_SECTION,
    INSTALLER_UPDATE_CHECK_KEY,
    WINDOWS_INSTALLER_MUTEX,
)
from updater.download import is_verified_file
from updater.manifest import PlatformRelease
from version import APP_NAME, APP_VERSION

logger = logging.getLogger(__name__)

_SYSTEM_NAMES = {"windows": "windows", "darwin": "macos", "linux": "linux"}
_ARCHITECTURE_NAMES = {
    "amd64": "x86_64",
    "x86_64": "x86_64",
    "arm64": "aarch64",
    "aarch64": "aarch64",
}
_COMPILED_CHANNELS = {"windows": "exe", "linux": "deb", "macos": "app"}
#: Set by the AppImage runtime for the program it starts: the path of
#: the AppImage file, and the folder its content is mounted at.
_APPIMAGE_VARIABLE = "APPIMAGE"
_APPDIR_VARIABLE = "APPDIR"
_CHANGED_AFTER_CHECK = "the download changed after it was checked"
_EXECUTABLE = 0o755
_REPLACEMENT_PREFIX = ".wiretrace-update-"
# The installer writes plain text; a byte-order mark is accepted if present.
_CHOICES_ENCODING = "utf-8-sig"

#: The option as Preferences and the Windows installer word it.
UPDATE_CHECK_OPTION = f"Check for new versions of {APP_NAME}"

#: Held for as long as the process lives. See ``hold_installer_mutex``.
_installer_mutex: object | None = None


class UpdateApplyError(OSError):
    """The downloaded update could not be put in place."""


class InstallKind(enum.Enum):
    """How an update reaches this installation."""

    INSTALLER = "installer"   # the Windows installer is run
    APPIMAGE = "appimage"     # the running AppImage is replaced
    MANUAL = "manual"         # the user installs it from the release page


# ── What this installation is ────────────────────────────────────────────────


def system_name() -> str:
    """``windows``, ``linux`` or ``macos``; another system by its own name."""
    name = platform.system().lower()
    return _SYSTEM_NAMES.get(name, name)


def architecture() -> str:
    """The processor type, under one name per type on every system."""
    machine = platform.machine().lower()
    return _ARCHITECTURE_NAMES.get(machine, machine)


def platform_key() -> str:
    """The name of this platform in an update manifest: ``windows-x86_64``."""
    return f"{system_name()}-{architecture()}"


def running_appimage() -> Path | None:
    """The AppImage file this process was started from, if it was.

    The runtime's variables alone do not say so: a program started from
    inside another application's AppImage inherits them. This process
    runs from an AppImage only if it is a compiled build whose executable
    lies in the folder that AppImage is mounted at.
    """
    appimage = os.environ.get(_APPIMAGE_VARIABLE)
    appdir = os.environ.get(_APPDIR_VARIABLE)
    if not appimage or not appdir or "__compiled__" not in globals():
        return None
    try:
        inside = Path(sys.executable).resolve().is_relative_to(Path(appdir).resolve())
    except OSError:
        return None
    return Path(appimage) if inside else None


def install_channel() -> str:
    """How the application was installed, read from the running process.

    ``appimage``: started from its own AppImage (see ``running_appimage``).
    ``source``: run by a Python interpreter.
    ``exe``, ``deb``, ``app``: a compiled build on Windows, Linux, macOS
    (the compiler defines ``__compiled__`` in every module).
    """
    if running_appimage() is not None:
        return "appimage"
    if "__compiled__" not in globals():
        return "source"
    return _COMPILED_CHANNELS.get(system_name(), "packaged")


def install_kind() -> InstallKind:
    channel = install_channel()
    if channel == "exe":
        return InstallKind.INSTALLER
    if channel == "appimage":
        return InstallKind.APPIMAGE
    return InstallKind.MANUAL


def user_agent() -> str:
    """What every update request says of its sender:
    ``WireTrace/2.0.0 (windows; x86_64; exe)``."""
    return f"{APP_NAME}/{APP_VERSION} ({system_name()}; {architecture()}; {install_channel()})"


def installer_update_choice() -> bool | None:
    """What was chosen in the Windows installer about update checks, or
    None if nothing was chosen there.

    The installer writes the choice to a file beside the executable. A
    silent installation, and every other kind of installation, writes
    none, and the setting keeps its default.
    """
    if install_kind() is not InstallKind.INSTALLER:
        return None
    parser = configparser.ConfigParser()
    try:
        parser.read(
            Path(sys.executable).resolve().parent / INSTALLER_CHOICES_FILE,
            encoding=_CHOICES_ENCODING,
        )
        return parser.getboolean(INSTALLER_CHOICES_SECTION, INSTALLER_UPDATE_CHECK_KEY)
    except (configparser.Error, ValueError, OSError):
        return None


# ── Putting an update in place ───────────────────────────────────────────────


def _require_unchanged(path: Path, release: PlatformRelease) -> None:
    """Raises UpdateApplyError unless ``path`` is still the file that was
    verified: the size and SHA-256 the signed manifest states."""
    if not is_verified_file(path, release.file_size, release.sha256):
        raise UpdateApplyError(_CHANGED_AFTER_CHECK)


def replace_appimage(source: Path, target: Path, release: PlatformRelease) -> None:
    """Replace the AppImage at ``target`` with ``source``, all or nothing.

    The new file is first copied into the target's own folder, so the
    final step is a rename within one file system, which cannot leave a
    half-written file. The copy, not the download it was made from, is
    what is checked against the manifest, so what is put in place is
    what was verified. Replacing a running executable is safe on Linux:
    the running process keeps the old file until it exits.

    Raises:
        UpdateApplyError: the copy is not the verified file.
        OSError: the target's folder cannot be written to.
    """
    descriptor, temporary = tempfile.mkstemp(prefix=_REPLACEMENT_PREFIX, dir=target.parent)
    os.close(descriptor)
    try:
        shutil.copyfile(source, temporary)
        _require_unchanged(Path(temporary), release)
        os.chmod(temporary, _EXECUTABLE)
        os.replace(temporary, target)
    except OSError:
        with contextlib.suppress(OSError):
            os.remove(temporary)
        raise


def prepare_update(installer: Path, release: PlatformRelease) -> None:
    """Do the part of an update that can fail, while the application is
    still open and can say so.

    The download waited in the cache while the user decided, so it is
    checked against the manifest once more. For an AppImage the file on
    disk is then replaced; the running process keeps using the old one
    until it exits. The Windows installer is run later, once the
    application has closed its sessions.

    Raises:
        UpdateApplyError: this installation cannot update itself, the
            download is no longer the verified file, or the running file
            could not be replaced.
    """
    kind = install_kind()
    if kind is InstallKind.MANUAL:
        raise UpdateApplyError("this installation cannot update itself")
    if kind is InstallKind.INSTALLER:
        _require_unchanged(installer, release)
        return
    target = running_appimage()
    if target is None:
        raise UpdateApplyError("this installation cannot update itself")
    try:
        replace_appimage(installer, target, release)
    except UpdateApplyError:
        raise
    except OSError as exc:
        raise UpdateApplyError(str(exc)) from exc


def launch_update(installer: Path, release: PlatformRelease) -> None:
    """Start what takes over once the application has closed its sessions:
    the installer on Windows, the new AppImage on Linux. The caller then
    quits.

    Raises:
        UpdateApplyError: it could not be started, or the installer is
            no longer the verified file.
    """
    if install_kind() is InstallKind.INSTALLER:
        _require_unchanged(installer, release)
        try:
            # Through the shell, so that an installer which asks for
            # administrator rights can be given them.
            os.startfile(installer)  # type: ignore[attr-defined]
        except OSError as exc:
            raise UpdateApplyError(str(exc)) from exc
        return
    target = running_appimage()
    if target is None:
        raise UpdateApplyError("this installation cannot update itself")
    try:
        subprocess.Popen([str(target)])
    except OSError as exc:
        raise UpdateApplyError(str(exc)) from exc


def hold_installer_mutex() -> bool:
    """Tell the Windows installer that the application is running.

    The installer waits for a named mutex to be free before it replaces
    files. The mutex is created here and released by the system when the
    process ends. Does nothing on other systems.

    Returns:
        True if the mutex is held.
    """
    global _installer_mutex
    if sys.platform != "win32":
        return False
    if _installer_mutex is None:
        import ctypes

        handle = ctypes.windll.kernel32.CreateMutexW(None, False, WINDOWS_INSTALLER_MUTEX)
        if not handle:
            logger.warning("The installer mutex could not be created")
            return False
        _installer_mutex = handle
    return True
