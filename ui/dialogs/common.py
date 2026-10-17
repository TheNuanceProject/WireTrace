# SPDX-License-Identifier: GPL-3.0-or-later
"""Parts every dialog is built from, so that all dialogs share one look.

Sizes come from the design tokens. A dialog that builds its buttons,
headings and margins through this module cannot drift from the others.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ui.themes import tokens
from ui.widgets.button import Button
from ui.widgets.styling import SECONDARY, SMALL, plain_label, set_primary


def dialog_layout(widget: QWidget) -> QVBoxLayout:
    """The outer layout of a dialog: even margins, even gaps."""
    metrics = tokens.METRICS
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(metrics.space_6, metrics.space_6, metrics.space_6, metrics.space_4)
    layout.setSpacing(metrics.space_3)
    return layout


def tighten(layout: QLayout) -> None:
    """Remove the margins of a layout nested inside another."""
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(tokens.METRICS.space_2)


def action_button(text: str, *, primary: bool = False) -> Button:
    """A button of a dialog; ``primary`` marks its main action."""
    button = Button(text)
    if primary:
        set_primary(button)
    return button


def button_row(*buttons: Button, leading: QWidget | None = None) -> QHBoxLayout:
    """Buttons at the right end of a row. ``leading`` sits at the left end."""
    row = QHBoxLayout()
    row.setSpacing(tokens.METRICS.space_2)
    if leading is not None:
        row.addWidget(leading)
    row.addStretch(1)
    for button in buttons:
        row.addWidget(button)
    return row


def heading(text: str, name: str = "sectionTitle") -> QLabel:
    """A heading. ``name`` is its style: sectionTitle, pageTitle or displayTitle."""
    label = plain_label(text)
    label.setObjectName(name)
    return label


def note(
    text: str = "", *, small: bool = False, centred: bool = False, wrap: bool = True,
) -> QLabel:
    """Secondary text: a hint, an explanation, a caption."""
    label = plain_label(text)
    label.setProperty(SECONDARY, True)
    label.setProperty(SMALL, small)
    label.setWordWrap(wrap)
    if centred:
        label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
    return label


def rule() -> QFrame:
    """A horizontal hairline between two parts of a dialog."""
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    return line


class ChoiceRow(QWidget):
    """A row of radio buttons of which exactly one is chosen.

    Signals:
        changed():  Another choice was made.
    """

    changed = Signal()

    def __init__(
        self,
        choices: Sequence[tuple[str, object]],
        current: object,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._group = QButtonGroup(self)
        self._values: dict[QAbstractButton, object] = {}
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(tokens.METRICS.space_4)
        for text, value in choices:
            button = QRadioButton(text)
            self._group.addButton(button)
            self._values[button] = value
            layout.addWidget(button)
        layout.addStretch(1)
        self.set_value(current)
        self._group.buttonClicked.connect(lambda _button: self.changed.emit())

    @property
    def value(self) -> object:
        return self._values[self._group.checkedButton()]

    def set_value(self, value: object) -> None:
        """Choose ``value``; an unknown value chooses the first."""
        buttons = list(self._values)
        chosen = next((b for b in buttons if self._values[b] == value), buttons[0])
        chosen.setChecked(True)

    def texts(self) -> list[str]:
        return [button.text() for button in self._values]
