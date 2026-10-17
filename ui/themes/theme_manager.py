# SPDX-License-Identifier: GPL-3.0-or-later
"""WireTrace theme manager: applies the design tokens to the application.

Responsibilities:
  - Decide which theme to draw from the saved preference, following the
    system's light or dark setting when the preference says so
  - Fill the stylesheet template from ``ui.themes.tokens`` and apply it
  - Set the application palette and interface font to match
  - Tell widgets when the theme has changed

This module does not touch serial I/O, log files or business logic.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

from app.constants import (
    LEGACY_THEME_VALUES,
    THEME_FALLBACK,
    THEME_FOLLOW_SYSTEM,
    ThemeID,
)
from ui.themes import icons, tokens
from ui.themes.fonts import interface_font
from ui.themes.window_frames import redraw_frames

logger = logging.getLogger(__name__)

_TEMPLATE_PATH = Path(__file__).resolve().parent / "wiretrace.qss.template"

#: Qt's own cross-platform style, under the stylesheet. With it, whatever
#: the stylesheet does not draw (the parts of a drop-down, a number field
#: or a message box) is drawn the same on every operating system.
BASE_STYLE = "Fusion"

#: ``{{name}}``, ``{{m.name}}``, ``{{f.name}}``, ``{{w.name}}`` or ``{{icon:name:role}}``.
_PLACEHOLDER = re.compile(r"\{\{([^{}]*)\}\}")

#: Stylesheet comments. They are removed before filling, so a comment can
#: show a placeholder as an example without it being treated as one.
_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)

_METRIC_PREFIX = "m."
_FONT_PREFIX = "f."
_WEIGHT_PREFIX = "w."
_ICON_PREFIX = "icon:"

#: Preference value -> name shown in the Theme menu and Preferences.
_PREFERENCE_NAMES: tuple[tuple[str, str], ...] = (
    (THEME_FOLLOW_SYSTEM, "System"),
    (ThemeID.LIGHT.value, "Light"),
    (ThemeID.DARK.value, "Dark"),
)


class StylesheetError(ValueError):
    """The stylesheet template refers to something the tokens do not define."""


def normalise_preference(value: str | None) -> str:
    """Return a valid theme preference for any stored value.

    Names written by versions before 2.0 are translated. Anything
    unknown or missing becomes "follow the system".
    """
    if not value:
        return THEME_FOLLOW_SYSTEM
    value = LEGACY_THEME_VALUES.get(value, value)
    if value in {name for name, _ in _PREFERENCE_NAMES}:
        return value
    return THEME_FOLLOW_SYSTEM


def _schemes() -> dict[ThemeID, Qt.ColorScheme]:
    """The colour scheme of each theme, as Qt and the operating system name it."""
    return {ThemeID.LIGHT: Qt.ColorScheme.Light, ThemeID.DARK: Qt.ColorScheme.Dark}


def system_theme() -> ThemeID:
    """The theme the system is set to, or the fallback when it does not say."""
    hints = QGuiApplication.styleHints()
    scheme = hints.colorScheme() if hints is not None else Qt.ColorScheme.Unknown
    for theme_id, theme_scheme in _schemes().items():
        if scheme == theme_scheme:
            return theme_id
    return THEME_FALLBACK


def requested_scheme(preference: str) -> Qt.ColorScheme:
    """The colour scheme to ask the operating system for.

    A chosen theme asks for its own, so that what the system draws for
    WireTrace (the title bar of every window on Windows) matches it.
    Following the system asks for none: ``Unknown`` is Qt's "the
    system's own".
    """
    if preference == THEME_FOLLOW_SYSTEM:
        return Qt.ColorScheme.Unknown
    return _schemes()[ThemeID(preference)]


def font_sizes() -> dict[str, str]:
    """Text size of each style of the type scale, in logical pixels."""
    return {name: str(style.size) for name, style in tokens.TYPE_STYLES.items()}


def font_weights() -> dict[str, str]:
    """The weights of the language, by name."""
    typography = tokens.TYPOGRAPHY
    return {"regular": str(typography.regular), "medium": str(typography.medium)}


def render_stylesheet(template: str, palette: tokens.Palette) -> str:
    """Fill every placeholder of ``template``.

    Raises:
        StylesheetError: a placeholder names a colour, size, text size,
            weight or icon that does not exist.
    """
    colours = palette.colours()
    measures = {**asdict(tokens.METRICS), **tokens.METRICS.content_sizes()}
    metrics = {name: str(value) for name, value in measures.items()}
    sizes = font_sizes()
    weights = font_weights()

    def fill(match: re.Match[str]) -> str:
        name = match.group(1)
        try:
            if name.startswith(_ICON_PREFIX):
                _, icon_name, role = name.split(":")
                return icons.stylesheet_url(icon_name, role)
            if name.startswith(_METRIC_PREFIX):
                return metrics[name[len(_METRIC_PREFIX):]]
            if name.startswith(_FONT_PREFIX):
                return sizes[name[len(_FONT_PREFIX):]]
            if name.startswith(_WEIGHT_PREFIX):
                return weights[name[len(_WEIGHT_PREFIX):]]
            return colours[name]
        except (KeyError, ValueError, LookupError, AttributeError) as exc:
            raise StylesheetError(f"unknown stylesheet placeholder: {name!r}") from exc

    filled = _PLACEHOLDER.sub(fill, _COMMENT.sub("", template))
    if "{{" in filled or "}}" in filled:
        raise StylesheetError("the stylesheet template has an unfinished placeholder")
    return filled


def build_palette(palette: tokens.Palette) -> QPalette:
    """A ``QPalette`` for anything the stylesheet does not reach."""
    result = QPalette()
    roles = QPalette.ColorRole
    assignments = {
        roles.Window: palette.paper,
        roles.WindowText: palette.text,
        roles.Base: palette.paper,
        roles.AlternateBase: palette.surface,
        roles.Text: palette.text,
        roles.Button: palette.paper,
        roles.ButtonText: palette.text,
        roles.BrightText: palette.error,
        roles.Highlight: palette.selection,
        roles.HighlightedText: palette.text,
        roles.ToolTipBase: palette.text,
        roles.ToolTipText: palette.paper,
        roles.PlaceholderText: palette.text_tertiary,
        roles.Link: palette.accent_strong,
        roles.LinkVisited: palette.accent_strong,
        roles.Mid: palette.edge,
        roles.Midlight: palette.divider,
        roles.Dark: palette.edge,
        roles.Light: palette.paper,
        roles.Shadow: palette.edge,
    }
    for role, colour in assignments.items():
        result.setColor(role, QColor(colour))
    for role in (roles.WindowText, roles.Text, roles.ButtonText):
        result.setColor(QPalette.ColorGroup.Disabled, role, QColor(palette.muted))
    return result


class ThemeManager(QObject):
    """Applies a theme and keeps it in step with the system setting.

    Signals:
        theme_changed(str):  Emitted after the drawn theme changes; carries
            the ``ThemeID`` value now in use.
    """

    theme_changed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._preference: str = THEME_FOLLOW_SYSTEM
        self._current_theme: ThemeID = tokens.active_theme()
        #: The theme last drawn by this manager; None before the first.
        self._drawn: ThemeID | None = None

        hints = QGuiApplication.styleHints()
        if hints is not None:
            hints.colorSchemeChanged.connect(self._on_system_scheme_changed)

    # ── State ──────────────────────────────────────────────────────────

    @property
    def preference(self) -> str:
        """The saved choice: a theme name or "follow the system"."""
        return self._preference

    @property
    def current_theme(self) -> ThemeID:
        """The theme being drawn."""
        return self._current_theme

    def available_preferences(self) -> list[tuple[str, str]]:
        """``(preference value, display name)`` for every choice offered."""
        return list(_PREFERENCE_NAMES)

    # ── Applying ───────────────────────────────────────────────────────

    def apply_preference(self, preference: str | None) -> bool:
        """Apply a saved theme preference. Returns ``True`` on success."""
        self._preference = normalise_preference(preference)
        # Asked for first: following the system then reads the system's
        # own scheme, not one asked for before.
        hints = QGuiApplication.styleHints()
        if hints is not None:
            hints.setColorScheme(requested_scheme(self._preference))
        return self._apply(self._resolve(self._preference))

    def apply_theme(self, theme_id: ThemeID) -> bool:
        """Draw ``theme_id`` and make it the preference."""
        return self.apply_preference(theme_id.value)

    def _resolve(self, preference: str) -> ThemeID:
        if preference == THEME_FOLLOW_SYSTEM:
            return system_theme()
        return ThemeID(preference)

    def _apply(self, theme_id: ThemeID) -> bool:
        app = QApplication.instance()
        if app is None:
            logger.error("Cannot apply a theme before the application exists")
            return False

        # Following the system, asking it for its own scheme can already
        # have drawn the theme (``_on_system_scheme_changed``); it is not
        # drawn twice.
        if theme_id is self._drawn:
            return True

        font = interface_font()

        previous = tokens.active_theme()
        tokens.set_active_theme(theme_id)
        try:
            template = _TEMPLATE_PATH.read_text(encoding="utf-8")
            stylesheet = render_stylesheet(template, tokens.PALETTES[theme_id])
        except (OSError, StylesheetError) as exc:
            tokens.set_active_theme(previous)
            logger.error("Failed to build the %s theme: %s", theme_id.value, exc)
            return False

        # Each of these restyles every widget, so none is repeated needlessly.
        app.setPalette(build_palette(tokens.PALETTES[theme_id]))
        replaces_style = not app.styleSheet()
        if app.styleSheet() != stylesheet:
            app.setStyleSheet(stylesheet)
        # The interface font goes last. Whenever Qt replaces the
        # application's style, and the first style sheet does, it gives
        # menus, message boxes, tooltips and lists the operating system's
        # own fonts again (QApplication::setStyle); setting the
        # application's font takes them away.
        if replaces_style or app.font() != font:
            app.setFont(font)
        icons.refresh_bound_icons()
        # Once every window has the new palette and scheme, which Qt gives
        # them through events still to be delivered.
        QTimer.singleShot(0, self._redraw_frames)

        self._current_theme = theme_id
        self._drawn = theme_id
        self.theme_changed.emit(theme_id.value)
        logger.info("Theme applied: %s", theme_id.value)
        return True

    def _redraw_frames(self) -> None:
        redraw_frames()

    def _on_system_scheme_changed(self, *_: object) -> None:
        if self._preference != THEME_FOLLOW_SYSTEM:
            return
        theme_id = system_theme()
        if theme_id != self._current_theme:
            self._apply(theme_id)
