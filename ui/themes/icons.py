# SPDX-License-Identifier: GPL-3.0-or-later
"""Interface icons drawn in the colours of the active theme.

The SVG files in ``resources/icons`` carry no colour of their own: every
stroke and fill is ``currentColor``. This module produces a copy of an
icon in a given colour, so one drawing serves both themes and every
state (normal text, secondary text, on a filled button).

Coloured copies are written once to the cache directory and reused. The
stylesheet refers to them by path, and buttons load them as ``QIcon``.
Buttons registered with :func:`bind_icon` are redrawn when the theme
changes.
"""

from __future__ import annotations

import logging
import os
import weakref
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QAbstractButton

from app._atomic import atomic_write_text
from app.constants import get_cache_dir
from ui.themes import tokens

logger = logging.getLogger(__name__)

_ICONS_DIR = Path(__file__).resolve().parent.parent.parent / "resources" / "icons"

#: Sub-directory of the cache directory that holds coloured icon copies.
_CACHE_SUBDIR = "icons"

#: The colour keyword every icon file uses for its strokes and fills.
_COLOUR_KEYWORD = "currentColor"

#: Palette role an icon is drawn in when none is asked for.
DEFAULT_ROLE = "text"

#: Buttons whose icon follows the theme: button -> (icon name, role).
_bound: weakref.WeakKeyDictionary[QAbstractButton, tuple[str, str]] = (
    weakref.WeakKeyDictionary()
)


def icon_names() -> list[str]:
    """Names of every icon shipped with the application."""
    if not _ICONS_DIR.is_dir():
        return []
    return sorted(path.stem for path in _ICONS_DIR.glob("*.svg"))


def coloured_icon_path(name: str, colour: str) -> str | None:
    """Path to the icon ``name`` drawn in ``colour`` (``#RRGGBB``).

    Returns ``None`` when the application has no icon of that name.
    When the cache directory cannot be written, the original file is
    returned so the icon is still shown, in the reader's default colour.
    """
    source = _ICONS_DIR / f"{name}.svg"
    if not source.is_file():
        logger.warning("No icon named %r", name)
        return None

    cache_dir = Path(get_cache_dir()) / _CACHE_SUBDIR
    target = cache_dir / f"{name}-{colour.lstrip('#').upper()}.svg"
    try:
        drawing = source.read_text(encoding="utf-8").replace(_COLOUR_KEYWORD, colour)
        if not target.is_file() or target.read_text(encoding="utf-8") != drawing:
            cache_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(str(target), drawing)
    except OSError as exc:
        logger.warning("Could not prepare icon %r in %s: %s", name, colour, exc)
        return str(source)
    return str(target)


def icon(name: str, role: str = DEFAULT_ROLE) -> QIcon:
    """The icon ``name`` in the active theme's colour for ``role``.

    Returns an empty icon when the application has no icon of that name.
    """
    path = coloured_icon_path(name, getattr(tokens.palette(), role))
    return QIcon(path) if path else QIcon()


def bind_icon(button: QAbstractButton, name: str, role: str = DEFAULT_ROLE) -> bool:
    """Give ``button`` an icon that follows the theme.

    Returns ``False`` and leaves the button unchanged when the
    application has no icon of that name.
    """
    drawn = icon(name, role)
    if drawn.isNull():
        return False
    button.setIcon(drawn)
    _bound[button] = (name, role)
    return True


def refresh_bound_icons() -> None:
    """Redraw every bound icon in the active theme. Called on a theme change."""
    for button, (name, role) in list(_bound.items()):
        try:
            button.setIcon(icon(name, role))
        except RuntimeError:
            # The Qt object behind this wrapper was already destroyed.
            _bound.pop(button, None)


def stylesheet_url(name: str, role: str) -> str:
    """Path for a stylesheet ``url("...")``.

    The template puts the path in double quotes, so a folder name with a
    space or a bracket (a Windows user name, for example) is safe. The
    path uses forward slashes on every platform, and a backslash or a
    double quote that is part of a name is escaped.
    """
    path = coloured_icon_path(name, getattr(tokens.palette(), role))
    if path is None:
        raise LookupError(f"stylesheet refers to an unknown icon: {name!r}")
    if os.sep != "/":
        path = path.replace(os.sep, "/")
    return path.replace("\\", "\\\\").replace('"', '\\"')
