# SPDX-License-Identifier: GPL-3.0-or-later
"""Fonts: the interface and console fonts shipped with the application.

Interface text is Poppins and the console is JetBrains Mono, both from
``resources/fonts``, so text has the same shape and size on Windows,
Linux and macOS. Sizes are set in logical pixels from the type scale of
``ui.themes.tokens``: they follow the display's scaling, which enlarges
everything evenly, and not the operating system's text size, which
would enlarge text alone and break the layout.

JetBrains Mono joins some pairs of characters, such as ``->``, ``!=`` and
``|=``, into one symbol. A monitor must show every character a device
sent as it was sent, so every fixed-width font made here has those
joins switched off.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase

from ui.themes.tokens import TYPE_STYLES, TYPOGRAPHY

logger = logging.getLogger(__name__)

_FONTS_DIR = Path(__file__).resolve().parent.parent.parent / "resources" / "fonts"

#: The OpenType features that join characters into one symbol: contextual
#: alternates (where JetBrains Mono keeps its joins), standard and
#: discretionary ligatures.
JOINING_FEATURES = ("calt", "liga", "dlig")

#: The type style of interface text that has no style of its own.
INTERFACE_STYLE = "ui"


def register_bundled_fonts() -> list[str]:
    """Make the fonts in ``resources/fonts`` available to the application.

    Must be called after the ``QApplication`` exists. Returns the family
    names that were registered; an empty list means text falls back to
    fonts already on the system.
    """
    families: list[str] = []
    for path in sorted(_FONTS_DIR.glob("*.ttf"), key=lambda p: p.name):
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id < 0:
            logger.warning("Could not load font file %s", path.name)
            continue
        for family in QFontDatabase.applicationFontFamilies(font_id):
            if family not in families:
                families.append(family)
    if not families:
        logger.warning("No bundled fonts were loaded from %s", _FONTS_DIR)
    return families


def _drawn_as_designed(font: QFont) -> QFont:
    """Have ``font`` drawn from its outlines, never fitted to the pixel grid.

    By default Qt on Windows fits text of 16 pixels or less to the pixel
    grid on a display at 100 % scale (Windows' "GDI classic" rendering):
    the round tops of C, D and S are cut flat and the tails of g and y
    are shortened, for fonts like ours that were drawn for screens that
    do not need it. Without hinting, every system draws the letters as
    the font draws them, as browsers draw the website.
    """
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return font


def monospace_font(family: str, pixel_size: int | None = None) -> QFont:
    """A fixed-width font: ``family`` first, then the language's mono families.

    ``pixel_size`` is in logical pixels; without it the font has the
    size of the ``mono`` type style.
    """
    families = [family, *(f for f in TYPOGRAPHY.console_families if f != family)]
    font = QFont()
    font.setFamilies(families)
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setFixedPitch(True)
    font.setWeight(QFont.Weight(TYPOGRAPHY.regular))
    for feature in JOINING_FEATURES:
        font.setFeature(QFont.Tag(feature), 0)
    font.setPixelSize(pixel_size if pixel_size is not None else TYPE_STYLES["mono"].size)
    return _drawn_as_designed(font)


def interface_font(style: str = INTERFACE_STYLE) -> QFont:
    """Poppins at the size, weight and letter spacing of the type style ``style``.

    Raises:
        KeyError: the type scale has no style of that name.
    """
    type_style = TYPE_STYLES[style]
    if type_style.family != "sans":
        raise KeyError(f"the type style {style!r} is not an interface style")
    font = QFont()
    font.setFamilies(list(TYPOGRAPHY.interface_families))
    font.setStyleHint(QFont.StyleHint.SansSerif)
    font.setPixelSize(type_style.size)
    font.setWeight(QFont.Weight(type_style.weight))
    font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, type_style.letter_spacing)
    return _drawn_as_designed(font)
