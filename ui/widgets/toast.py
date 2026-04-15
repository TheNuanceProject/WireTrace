# SPDX-License-Identifier: MIT
"""WireTrace toast notification — professional outline-style feedback.

Non-intrusive overlay notification with border/outline design.
Four severity levels: info (blue), success (green), warning (amber), error (red).
Automatically dismisses after a configurable duration.
"""

from __future__ import annotations

from typing import ClassVar

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QLabel, QWidget

_DURATION_MS = 3500


class Toast(QLabel):
    """Outline-style overlay toast notification."""

    STYLES: ClassVar[dict[str, dict[str, str]]] = {
        "info": {
            "border": "#1976D2", "text": "#1976D2", "bg": "#E3F2FD",
            "icon": "ℹ",
        },
        "success": {
            "border": "#2E7D32", "text": "#2E7D32", "bg": "#E8F5E9",
            "icon": "✓",
        },
        "warning": {
            "border": "#E65100", "text": "#E65100", "bg": "#FFF3E0",
            "icon": "⚠",
        },
        "error": {
            "border": "#C62828", "text": "#C62828", "bg": "#FFEBEE",
            "icon": "✕",
        },
    }

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWordWrap(True)
        self.setMinimumHeight(30)
        self.setMinimumWidth(200)
        self.setMaximumWidth(600)
        self.hide()

        font = self.font()
        font.setPointSize(10)
        font.setWeight(QFont.Weight.Medium)
        self.setFont(font)

    def show_toast(self, message: str, level: str = "info") -> None:
        """Show a toast notification with outline styling."""
        style = self.STYLES.get(level, self.STYLES["info"])
        icon = style["icon"]

        self.setText(f"  {icon}  {message}  ")
        self.setStyleSheet(
            f"background-color: {style['bg']}; "
            f"color: {style['text']}; "
            f"border: 1.5px solid {style['border']}; "
            f"border-radius: 4px; "
            f"padding: 6px 16px; "
            f"font-size: 10pt; "
            f"font-weight: 500;"
        )

        parent = self.parentWidget()
        if parent:
            self.adjustSize()
            w = max(self.sizeHint().width(), 240)
            w = min(w, parent.width() - 40)
            h = self.sizeHint().height()
            self.setFixedSize(w, max(h, 30))
            x = (parent.width() - w) // 2
            self.move(x, 6)

        self.show()
        self.raise_()
        QTimer.singleShot(_DURATION_MS, self._dismiss)

    def _dismiss(self) -> None:
        self.hide()

    @staticmethod
    def info(parent: QWidget, message: str) -> Toast:
        t = Toast._get_or_create(parent)
        t.show_toast(message, "info")
        return t

    @staticmethod
    def success(parent: QWidget, message: str) -> Toast:
        t = Toast._get_or_create(parent)
        t.show_toast(message, "success")
        return t

    @staticmethod
    def warning(parent: QWidget, message: str) -> Toast:
        t = Toast._get_or_create(parent)
        t.show_toast(message, "warning")
        return t

    @staticmethod
    def error(parent: QWidget, message: str) -> Toast:
        t = Toast._get_or_create(parent)
        t.show_toast(message, "error")
        return t

    @staticmethod
    def _get_or_create(parent: QWidget) -> Toast:
        for child in parent.children():
            if isinstance(child, Toast):
                return child
        return Toast(parent)
