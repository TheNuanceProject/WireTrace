# SPDX-License-Identifier: GPL-3.0-or-later
"""Banners: messages about something the user must notice.

A banner stays until the problem is over or the user dismisses it, and
can offer the actions that help. Something that merely finished is a
toast; something that needs a decision is a dialog. An information
banner asks something that can wait, without blocking anything.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)

from ui.themes import tokens
from ui.themes.icons import bind_icon
from ui.widgets.button import Button
from ui.widgets.styling import LEVEL, plain_label, set_state

INFO = "info"
WARNING = "warning"
ERROR = "error"


@dataclass(frozen=True)
class BannerAction:
    """A button on a banner."""

    label: str
    run: Callable[[], None]


class Banner(QFrame):
    """One message, with optional detail, actions and a close button."""

    dismissed = Signal()

    def __init__(
        self,
        level: str,
        title: str,
        detail: str,
        actions: tuple[BannerAction, ...],
        dismissible: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("banner")
        set_state(self, LEVEL, level)
        metrics = tokens.METRICS

        text = QVBoxLayout()
        text.setSpacing(metrics.space_half)
        self.title_label = plain_label(title)
        self.title_label.setObjectName("bannerTitle")
        self.title_label.setWordWrap(True)
        text.addWidget(self.title_label)
        self.detail_label = plain_label(detail)
        self.detail_label.setWordWrap(True)
        self.detail_label.setVisible(bool(detail))
        self.detail_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        text.addWidget(self.detail_label)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(metrics.space_3, metrics.space_2, metrics.space_2, metrics.space_2)
        layout.setSpacing(metrics.space_2)
        layout.addLayout(text, 1)

        self.action_buttons: list[Button] = []
        for action in actions:
            button = Button(action.label, compact=True)
            button.clicked.connect(action.run)
            layout.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
            self.action_buttons.append(button)

        self.close_button: Button | None = None
        if dismissible:
            close = Button(compact=True)
            close.setObjectName("flatIconButton")
            close.setAccessibleName("Dismiss")
            close.setToolTip("Dismiss")
            bind_icon(close, "close_tab", "text_secondary")
            close.clicked.connect(self.dismissed)
            layout.addWidget(close, 0, Qt.AlignmentFlag.AlignVCenter)
            self.close_button = close


class BannerArea(QWidget):
    """Banners stacked one above the other, each known by a key.

    Showing a banner under a key that is already shown replaces it, so
    a repeated problem updates its banner instead of adding another.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._banners: dict[str, Banner] = {}
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self.hide()

    def show_banner(
        self,
        key: str,
        level: str,
        title: str,
        detail: str = "",
        actions: tuple[BannerAction, ...] = (),
        *,
        dismissible: bool = True,
    ) -> Banner:
        self.clear(key)
        banner = Banner(level, title, detail, actions, dismissible, self)
        banner.dismissed.connect(lambda: self.clear(key))
        self._banners[key] = banner
        self._layout.addWidget(banner)
        self.show()
        return banner

    def clear(self, key: str) -> None:
        banner = self._banners.pop(key, None)
        if banner is not None:
            self._layout.removeWidget(banner)
            banner.hide()
            banner.deleteLater()
        self.setVisible(bool(self._banners))

    def clear_all(self) -> None:
        for key in list(self._banners):
            self.clear(key)

    def banner(self, key: str) -> Banner | None:
        return self._banners.get(key)

    def keys(self) -> list[str]:
        return list(self._banners)
