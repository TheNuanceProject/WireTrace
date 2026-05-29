# SPDX-License-Identifier: MIT
"""WireTrace User Guide loader.

Locates the User Guide HTML asset across dev and packaged builds,
substitutes the live application version into it, writes a stamped
copy to a per-user cache directory, and returns the path so the
caller can open it in the system browser.

Design goals:
  - **Platform independent**: works on Windows, macOS, and Linux,
    and on Nuitka standalone builds, PyInstaller one-folder/one-file
    builds, and plain ``python main.py`` development invocations.
  - **No writes inside the install directory**: on Windows the install
    may live under ``C:\\Program Files`` which is read-only for
    standard users. We write the stamped copy to the per-user cache
    directory returned by ``QStandardPaths.CacheLocation`` (or a
    platform fallback if Qt isn't initialised yet — e.g. during tests).
  - **Version-aware**: the placeholder ``{{WIRETRACE_VERSION}}`` in
    the HTML is replaced with the live ``APP_VERSION`` so users can
    always tell whether the docs match the executable they're running.
  - **Fail loud, fail clear**: if the source HTML cannot be located,
    raise ``UserGuideNotFoundError`` with a descriptive message. The
    UI catches this and shows a toast — the user never sees a silent
    no-op.

This module is deliberately a small façade: no Qt, no GUI imports
at the module level. The caller (MainWindow) opens the resulting
path via ``QDesktopServices.openUrl``. Keeping this file UI-free
means it tests cleanly under headless stubs.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
from pathlib import Path

from version import APP_VERSION

logger = logging.getLogger(__name__)


# ── Public surface ──────────────────────────────────────────────────────────

#: Asset filename, repeated in BUILD_GUIDE.md so the Nuitka bundler
#: includes it via ``--include-data-dir=resources/help=resources/help``.
USER_GUIDE_FILENAME = "user_guide.html"

#: Placeholder token replaced with ``APP_VERSION`` at load time.
VERSION_PLACEHOLDER = "{{WIRETRACE_VERSION}}"


class UserGuideNotFoundError(FileNotFoundError):
    """Raised when the User Guide HTML asset cannot be located.

    The MainWindow surface catches this and shows a toast with a
    clear message rather than letting the user double-click and see
    nothing happen.
    """


def resolve_user_guide_source() -> Path:
    """Find the User Guide HTML on disk.

    Returns the absolute path. Raises ``UserGuideNotFoundError`` if
    no candidate path exists.

    Candidate paths (tried in order):
      1. ``<repo-root>/resources/help/user_guide.html`` — development
         invocation via ``python main.py``. ``__file__`` is
         ``<repo>/app/help_loader.py`` so the parent-parent is the
         repo root.
      2. ``<exe-dir>/resources/help/user_guide.html`` — Nuitka
         standalone and one-folder PyInstaller builds. The
         ``resources`` tree is bundled alongside the executable via
         ``--include-data-dir``.
      3. ``sys._MEIPASS/resources/help/user_guide.html`` — PyInstaller
         one-file builds extract data into a temp directory whose
         path is exposed as ``sys._MEIPASS``. Harmless on other
         build types (the attribute is absent).
      4. macOS ``.app`` bundle: ``<exe-dir>/../Resources/resources/help/...``
         The Nuitka macOS bundle convention places resources under
         ``Contents/Resources``. We try this when on Darwin.
    """
    candidates: list[Path] = []

    # 1. Development tree
    candidates.append(
        Path(__file__).resolve().parent.parent
        / "resources" / "help" / USER_GUIDE_FILENAME,
    )

    # 2. Frozen executable directory
    exe_dir = Path(sys.executable).resolve().parent
    candidates.append(exe_dir / "resources" / "help" / USER_GUIDE_FILENAME)

    # 3. PyInstaller one-file build
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(
            Path(meipass) / "resources" / "help" / USER_GUIDE_FILENAME,
        )

    # 4. macOS .app bundle layout
    if sys.platform == "darwin":
        candidates.append(
            exe_dir.parent / "Resources" / "resources" / "help"
            / USER_GUIDE_FILENAME,
        )

    for candidate in candidates:
        if candidate.is_file():
            logger.debug("User guide located at %s", candidate)
            return candidate

    raise UserGuideNotFoundError(
        "User Guide HTML not found. Looked in:\n  - "
        + "\n  - ".join(str(p) for p in candidates),
    )


def _cache_dir() -> Path:
    """Return a per-user cache directory for the stamped HTML.

    We do NOT write inside the install directory: on Windows, that
    directory may live under ``C:\\Program Files`` which is
    read-only for standard users; on macOS, writing inside an
    ``.app`` bundle invalidates the code signature.

    Order of preference:
      1. ``QStandardPaths.CacheLocation`` if Qt is available and
         a ``QCoreApplication`` instance exists.
      2. Platform-conventional cache locations:
         - Windows: ``%LOCALAPPDATA%\\WireTrace\\Cache``
         - macOS:   ``~/Library/Caches/WireTrace``
         - Linux:   ``$XDG_CACHE_HOME/WireTrace`` or ``~/.cache/WireTrace``
      3. ``tempfile.gettempdir()/wiretrace-help`` as a last resort.

    The directory is created if it doesn't exist.
    """
    qt_cache = _qt_cache_dir()
    if qt_cache is not None:
        target = qt_cache / "help"
        target.mkdir(parents=True, exist_ok=True)
        return target

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            target = Path(base) / "WireTrace" / "Cache" / "help"
            target.mkdir(parents=True, exist_ok=True)
            return target
    elif sys.platform == "darwin":
        target = Path.home() / "Library" / "Caches" / "WireTrace" / "help"
        target.mkdir(parents=True, exist_ok=True)
        return target
    else:
        xdg = os.environ.get("XDG_CACHE_HOME")
        if xdg:
            target = Path(xdg) / "WireTrace" / "help"
        else:
            target = Path.home() / ".cache" / "WireTrace" / "help"
        target.mkdir(parents=True, exist_ok=True)
        return target

    import tempfile
    target = Path(tempfile.gettempdir()) / "wiretrace-help"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _qt_cache_dir() -> Path | None:
    """Return Qt's per-user cache directory if Qt is available and a
    QCoreApplication exists. Otherwise None.

    Importing PySide6 is deferred to this function so the help loader
    module remains usable in headless / test contexts.
    """
    try:
        from PySide6.QtCore import QCoreApplication, QStandardPaths
    except ImportError:
        return None
    if QCoreApplication.instance() is None:
        return None
    try:
        path = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.CacheLocation,
        )
    except Exception:
        return None
    return Path(path) if path else None


def stamp_version(html: str, version: str = APP_VERSION) -> str:
    """Substitute the version placeholder in the HTML.

    Idempotent: a stamped string with no remaining placeholder is
    returned unchanged. Multiple occurrences are all replaced.
    """
    return html.replace(VERSION_PLACEHOLDER, version)


def prepare_user_guide() -> Path:
    """Return a path to a version-stamped User Guide HTML.

    Reads the source asset, substitutes the version, and writes the
    stamped copy to the per-user cache directory. Returns the path
    of the stamped copy, ready for ``QDesktopServices.openUrl``.

    The cache filename embeds a hash of the source content so cache
    refresh is automatic when the source HTML changes (e.g. after an
    in-place update of WireTrace) — no manual cache invalidation
    needed.

    Raises:
        UserGuideNotFoundError: if the source HTML cannot be located.
        OSError: if writing the stamped copy fails. The caller should
            catch and surface a clear message.
    """
    source = resolve_user_guide_source()
    raw = source.read_text(encoding="utf-8")
    stamped = stamp_version(raw)

    # Cache key includes both the version and a content hash so the
    # stamped copy is regenerated whenever either changes. This avoids
    # stale stamped HTML surviving an in-place upgrade.
    content_hash = hashlib.sha256(stamped.encode("utf-8")).hexdigest()[:16]
    cache_dir = _cache_dir()
    cache_file = cache_dir / f"user_guide-{APP_VERSION}-{content_hash}.html"

    if not cache_file.is_file():
        # Write atomically: write to a temp sibling, then rename.
        # Avoids a half-written file being opened if we crash mid-write.
        tmp = cache_file.with_suffix(cache_file.suffix + ".tmp")
        tmp.write_text(stamped, encoding="utf-8")
        os.replace(tmp, cache_file)
        logger.debug("Stamped user guide written to %s", cache_file)

    return cache_file
