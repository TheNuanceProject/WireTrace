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
    QSizePolicy,
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
    """One message, with optional detail, actions and a close button.

    Every part is placed in the banner before anything is shown, so no
    part can appear on its own as a separate window. Its words wrap to
    the width the banner is given and never make the window wider.
    """

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
        metrics = tokens.METRICS

        layout = QHBoxLayout(self)
        layout.setContentsMargins(metrics.space_3, metrics.space_2, metrics.space_2, metrics.space_2)
        layout.setSpacing(metrics.space_2)
        text = QVBoxLayout()
        text.setSpacing(metrics.space_half)
        layout.addLayout(text, 1)

        self.title_label = plain_label()
        self.title_label.setObjectName("bannerTitle")
        self.title_label.setWordWrap(True)
        text.addWidget(self.title_label)
        self.detail_label = plain_label()
        self.detail_label.setWordWrap(True)
        self.detail_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        text.addWidget(self.detail_label)
        for label in (self.title_label, self.detail_label):
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        self._actions = actions
        self.action_buttons: list[Button] = []
        for index, action in enumerate(actions):
            button = Button(action.label, compact=True)
            button.clicked.connect(lambda _checked=False, i=index: self._actions[i].run())
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

        self.set_content(level, title, detail, actions)

    def fits(self, actions: tuple[BannerAction, ...], dismissible: bool) -> bool:
        """Whether this banner has the buttons to show ``actions`` as they are."""
        return (
            [action.label for action in actions] == [action.label for action in self._actions]
            and dismissible == (self.close_button is not None)
        )

    def set_content(
        self, level: str, title: str, detail: str, actions: tuple[BannerAction, ...],
    ) -> None:
        """Show new words and actions in the same banner; the buttons must fit."""
        set_state(self, LEVEL, level)
        self.title_label.setText(title)
        self.detail_label.setText(detail)
        self.detail_label.setVisible(bool(detail))
        self._actions = actions


class BannerArea(QWidget):
    """Banners stacked one above the other, each known by a key.

    Showing a banner under a key that is already shown updates that
    banner in place, so a repeated problem changes its words instead of
    adding a banner or building a new one.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._banners: dict[str, Banner] = {}
        self._on_dismiss: dict[str, Callable[[], None] | None] = {}
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
        on_dismiss: Callable[[], None] | None = None,
    ) -> Banner:
        """Show a banner under ``key``; ``on_dismiss`` runs when the user closes it."""
        banner = self._banners.get(key)
        if banner is not None and banner.fits(actions, dismissible):
            banner.set_content(level, title, detail, actions)
        else:
            self.clear(key)
            banner = Banner(level, title, detail, actions, dismissible, self)
            banner.dismissed.connect(lambda: self._dismiss(key))
            self._banners[key] = banner
            self._layout.addWidget(banner)
            self.show()
        self._on_dismiss[key] = on_dismiss
        return banner

    def _dismiss(self, key: str) -> None:
        run = self._on_dismiss.get(key)
        self.clear(key)
        if run is not None:
            run()

    def clear(self, key: str) -> None:
        self._on_dismiss.pop(key, None)
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
