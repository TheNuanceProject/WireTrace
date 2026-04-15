# SPDX-License-Identifier: MIT
"""WireTrace theme loader and hot-swap engine.

Responsibilities:
  - Load QSS theme files (studio_light.qss, midnight_dark.qss)
  - Apply theme to the QApplication stylesheet
  - Provide tag colors for the active theme
  - Support hot-swap (< 100ms theme switch per spec)

This module does NOT touch: serial I/O, file logging, or business logic.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from app.constants import ThemeID

logger = logging.getLogger(__name__)

# ── Tag Colors Per Theme (spec section 7.3) ──────────────────────────────────

_TAG_COLORS = {
    ThemeID.STUDIO_LIGHT: {
        "CRITICAL":  ("#B71C1C", True),
        "ERROR":     ("#C62828", False),
        "WARNING":   ("#E65100", False),
        "INFO":      ("#1565C0", False),
        "DEBUG":     ("#757575", False),
        "COMMAND":   ("#0D47A1", True),
        "DATA":      ("#212121", False),
        "TIMESTAMP": ("#9E9E9E", False),
    },
    ThemeID.MIDNIGHT_DARK: {
        "CRITICAL":  ("#EF5350", True),
        "ERROR":     ("#E57373", False),
        "WARNING":   ("#FFB74D", False),
        "INFO":      ("#64B5F6", False),
        "DEBUG":     ("#9E9E9E", False),
        "COMMAND":   ("#42A5F5", True),
        "DATA":      ("#E0E0E0", False),
        "TIMESTAMP": ("#757575", False),
    },
}

_THEME_PROPERTIES = {
    ThemeID.STUDIO_LIGHT: {
        "background": "#FAFAFA",
        "panel": "#F5F5F5",
        "border": "#E0E0E0",
        "text_primary": "#212121",
        "text_secondary": "#757575",
        "text_disabled": "#BDBDBD",
        "accent": "#1976D2",
        "accent_hover": "#1565C0",
        "accent_pressed": "#0D47A1",
        "button_bg": "#1976D2",
        "button_text": "#FFFFFF",
        "console_bg": "#FFFFFF",
        "console_text": "#212121",
        "selection": "#BBDEFB",
        "input_bg": "#FFFFFF",
        "input_border": "#BDBDBD",
        "input_focus_border": "#1976D2",
        "status_bg": "#F5F5F5",
        "status_text": "#616161",
        "error": "#D32F2F",
        "success": "#388E3C",
        "warning": "#F57A00",
    },
    ThemeID.MIDNIGHT_DARK: {
        "background": "#1E1E1E",
        "panel": "#252526",
        "border": "#333333",
        "text_primary": "#CCCCCC",
        "text_secondary": "#858585",
        "text_disabled": "#5A5A5A",
        "accent": "#2979FF",
        "accent_hover": "#448AFF",
        "accent_pressed": "#1565C0",
        "button_bg": "#2979FF",
        "button_text": "#FFFFFF",
        "console_bg": "#1E1E1E",
        "console_text": "#D4D4D4",
        "selection": "#264F78",
        "input_bg": "#2D2D2D",
        "input_border": "#3C3C3C",
        "input_focus_border": "#2979FF",
        "status_bg": "#252526",
        "status_text": "#858585",
        "error": "#E57373",
        "success": "#81C784",
        "warning": "#FFB74D",
    },
}


class ThemeManager(QObject):
    """Loads and applies QSS themes with hot-swap support.

    Signals:
        theme_changed(str):  Emitted after theme switch, carries ThemeID value.
    """

    theme_changed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._current_theme: ThemeID = ThemeID.MIDNIGHT_DARK
        self._themes_dir = str(Path(__file__).parent)

    @property
    def current_theme(self) -> ThemeID:
        return self._current_theme

    @property
    def current_theme_value(self) -> str:
        return self._current_theme.value

    def apply_theme(self, theme_id: ThemeID | str) -> bool:
        """Load and apply a QSS theme to the application."""
        if isinstance(theme_id, str):
            theme_id = self._resolve_theme_id(theme_id)

        qss_path = self._get_qss_path(theme_id)
        if not qss_path or not os.path.isfile(qss_path):
            logger.error("Theme file not found: %s", qss_path)
            return False

        try:
            with open(qss_path, encoding="utf-8") as f:
                stylesheet = f.read()

            # Substitute {{ICONS_DIR}} placeholder with absolute path
            icons_dir = str(
                Path(__file__).parent.parent.parent / "resources" / "icons"
            )
            # QSS url() requires forward slashes on all platforms
            icons_dir = icons_dir.replace("\\", "/")
            stylesheet = stylesheet.replace("{{ICONS_DIR}}", icons_dir)

            app = QApplication.instance()
            if app:
                app.setStyleSheet(stylesheet)

            self._current_theme = theme_id
            self.theme_changed.emit(theme_id.value)
            logger.info("Theme applied: %s", theme_id.value)
            return True

        except OSError as e:
            logger.error("Failed to load theme %s: %s", theme_id.value, e)
            return False

    def get_tag_color(self, tag: str) -> tuple[str, bool]:
        """Get (hex_color, is_bold) for a severity tag in the active theme."""
        theme_colors = _TAG_COLORS.get(self._current_theme, {})
        return theme_colors.get(tag, ("#888888", False))

    def get_tag_colors(self) -> dict[str, tuple[str, bool]]:
        """Get all tag colors for the current theme."""
        return dict(_TAG_COLORS.get(self._current_theme, {}))

    def get_property(self, prop: str) -> str:
        """Get a named theme property (color hex value)."""
        props = _THEME_PROPERTIES.get(self._current_theme, {})
        return props.get(prop, "")

    def available_themes(self) -> list[tuple[ThemeID, str]]:
        """Return list of (ThemeID, display_name) for all themes."""
        return [
            (ThemeID.STUDIO_LIGHT, "Studio Light"),
            (ThemeID.MIDNIGHT_DARK, "Midnight Dark"),
        ]

    def _get_qss_path(self, theme_id: ThemeID) -> str | None:
        filenames = {
            ThemeID.STUDIO_LIGHT: "studio_light.qss",
            ThemeID.MIDNIGHT_DARK: "midnight_dark.qss",
        }
        filename = filenames.get(theme_id)
        if not filename:
            return None
        return os.path.join(self._themes_dir, filename)

    @staticmethod
    def _resolve_theme_id(value: str) -> ThemeID:
        for theme in ThemeID:
            if theme.value == value:
                return theme
        return ThemeID.MIDNIGHT_DARK
