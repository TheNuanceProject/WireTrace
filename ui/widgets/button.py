# SPDX-License-Identifier: GPL-3.0-or-later
"""The button every part of the interface uses.

One rule for size. A button is regular or compact; each size has one
height, one padding and one least width. A button that carries only an
icon is a square of its size's height. Joined buttons share one width.

The stylesheet gives the height, the padding and the icon squares. The
least width is given here, as part of the size the button asks for, so
that a layout can never make a button narrower than its text.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget

from ui.themes import tokens
from ui.widgets.styling import COMPACT, SEGMENT, set_state

#: Object names of the buttons that carry only an icon.
ICON_ONLY_NAMES = ("iconButton", "flatIconButton")

_SEGMENT_SIDES = ("left", "right")


class Button(QPushButton):
    """A push button that follows the size rule."""

    def __init__(
        self, text: str = "", parent: QWidget | None = None, *, compact: bool = False,
    ) -> None:
        super().__init__(text, parent)
        # The keyboard moves the focus to a button; a click does not, so
        # the focus ring shows where the keyboard is and never after a click.
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        if compact:
            set_state(self, COMPACT, True)

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        if self.objectName() in ICON_ONLY_NAMES or self.property(SEGMENT):
            return hint
        metrics = tokens.METRICS
        least = (
            metrics.button_min_width_compact if self.property(COMPACT)
            else metrics.button_min_width
        )
        return hint.expandedTo(QSize(least, 0))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()


class _JoinedButtons(QWidget):
    """Buttons side by side as one control, every one as wide as the widest."""

    def __init__(self, buttons: tuple[Button, ...]) -> None:
        super().__init__()
        self._buttons = buttons
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        for button in buttons:
            layout.addWidget(button, 1)

    def sizeHint(self) -> QSize:
        hints = [button.sizeHint() for button in self._buttons]
        return QSize(
            max(hint.width() for hint in hints) * len(hints),
            max(hint.height() for hint in hints),
        )

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()


def join_buttons(left: Button, right: Button) -> QWidget:
    """Join two buttons into one control: Text | HEX."""
    buttons = (left, right)
    for button, side in zip(buttons, _SEGMENT_SIDES, strict=True):
        set_state(button, SEGMENT, side)
    return _JoinedButtons(buttons)
