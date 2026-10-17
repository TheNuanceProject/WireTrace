# SPDX-License-Identifier: GPL-3.0-or-later
"""WireTrace application icon.

The window, taskbar, splash and About icon, loaded from ``resources/``.
Interface icons (toolbar buttons, stylesheet marks) are drawn in the
active theme's colours by ``ui.themes.icons``.

Usage:
    from app.icon_loader import app_icon
    window.setWindowIcon(app_icon())
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon, QPixmap

# Resolve directories (relative to project root)
_PROJECT_ROOT = Path(__file__).parent.parent
_RESOURCES_DIR = str(_PROJECT_ROOT / "resources")


@lru_cache(maxsize=1)
def app_icon() -> QIcon:
    """Load the application icon (used for window icon, taskbar, splash, about).

    Uses .ico on Windows (multi-size), .png everywhere else.
    """
    # Try .ico first on Windows (contains 16-256px sizes)
    if sys.platform == "win32":
        ico_path = os.path.join(_RESOURCES_DIR, "app_icon.ico")
        if os.path.isfile(ico_path):
            qi = QIcon(ico_path)
            if not qi.isNull():
                return qi

    # Try .png (512px source: works everywhere)
    png_path = os.path.join(_RESOURCES_DIR, "app_icon.png")
    if os.path.isfile(png_path):
        qi = QIcon(png_path)
        if not qi.isNull():
            return qi

    return QIcon()


def app_icon_pixmap(size: int) -> QPixmap:
    """Return the app icon as a QPixmap at the requested size.

    Used for the splash screen and the About dialog.
    """
    qi = app_icon()
    if qi.isNull():
        return QPixmap()
    return qi.pixmap(QSize(size, size))
