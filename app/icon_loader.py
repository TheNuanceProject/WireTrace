# SPDX-License-Identifier: MIT
"""WireTrace icon loader — loads SVG icons from the filesystem.

In production (Nuitka build), icons are embedded via .qrc.
In development, icons are loaded from resources/icons/ on disk.

Usage:
    from app.icon_loader import icon, app_icon
    button.setIcon(icon("connect"))
    window.setWindowIcon(app_icon())
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon, QPixmap

# Resolve directories (relative to project root)
_PROJECT_ROOT = Path(__file__).parent.parent
_ICONS_DIR = str(_PROJECT_ROOT / "resources" / "icons")
_RESOURCES_DIR = str(_PROJECT_ROOT / "resources")


@lru_cache(maxsize=1)
def app_icon() -> QIcon:
    """Load the application icon (used for window icon, taskbar, splash, about).

    Tries filesystem first (development), then Qt resource (production).
    Uses .ico on Windows (multi-size), .png everywhere else.
    """
    import sys

    # Try .ico first on Windows (contains 16-256px sizes)
    if sys.platform == "win32":
        ico_path = os.path.join(_RESOURCES_DIR, "app_icon.ico")
        if os.path.isfile(ico_path):
            qi = QIcon(ico_path)
            if not qi.isNull():
                return qi

    # Try .png (512px source — works everywhere)
    png_path = os.path.join(_RESOURCES_DIR, "app_icon.png")
    if os.path.isfile(png_path):
        qi = QIcon(png_path)
        if not qi.isNull():
            return qi

    # Try Qt resource system (production builds)
    for qrc in (":/app_icon.ico", ":/app_icon.png"):
        qi = QIcon(qrc)
        if not qi.isNull():
            return qi

    return QIcon()


def app_icon_pixmap(size: int = 64) -> QPixmap:
    """Return the app icon as a QPixmap at the requested size.

    Used for splash screen, welcome page, and about dialog.
    """
    qi = app_icon()
    if qi.isNull():
        return QPixmap()
    return qi.pixmap(QSize(size, size))


@lru_cache(maxsize=64)
def icon(name: str) -> QIcon:
    """Load an icon by semantic name.

    Tries filesystem first (resources/icons/name.svg for dev),
    then Qt resource path (:/icons/name.svg for production builds).

    Returns an empty QIcon if not found (graceful degradation).
    """
    # Try filesystem first (development)
    fs_path = os.path.join(_ICONS_DIR, f"{name}.svg")
    if os.path.isfile(fs_path):
        return QIcon(fs_path)

    # Try Qt resource system (production — .qrc compiled)
    qrc_path = f":/icons/{name}.svg"
    qi = QIcon(qrc_path)
    if not qi.isNull():
        return qi

    # Graceful fallback — empty icon
    return QIcon()


def has_icons() -> bool:
    """Check if any icons are available."""
    if os.path.isdir(_ICONS_DIR):
        return any(f.endswith(".svg") for f in os.listdir(_ICONS_DIR))
    return False
