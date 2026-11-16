# SPDX-License-Identifier: GPL-3.0-or-later
"""Edits the highlight rules: a text to look for and a colour, one row each.

The text field sits in the column of a setting's name and the colour in
the column of its field, so the rows line up with the settings above.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLineEdit, QVBoxLayout, QWidget

from app.constants import HIGHLIGHT_COLOUR_NAMES, HIGHLIGHT_RULES_MAX, HIGHLIGHT_TEXT_MAX
from core.highlight_rules import HighlightRule
from ui.dialogs.common import action_button
from ui.themes import tokens
from ui.themes.icons import bind_icon
from ui.widgets.button import Button


class HighlightEditor(QWidget):
    """The highlight rules, to add, change and remove."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        metrics = tokens.METRICS
        self._rows: list[tuple[QWidget, QLineEdit, QComboBox]] = []
        self._list = QVBoxLayout()
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(metrics.space_2)
        self._add = action_button("Add a rule")
        self._add.clicked.connect(lambda: self._add_row(HighlightRule("", 1), focus=True))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(metrics.space_3)
        layout.addLayout(self._list)
        layout.addWidget(self._add, 0, Qt.AlignmentFlag.AlignLeft)

    def rules(self) -> tuple[HighlightRule, ...]:
        """The rules as edited; rows whose text is blank are left out."""
        return tuple(
            HighlightRule(text.text().strip(), colour.currentData())
            for _, text, colour in self._rows if text.text().strip()
        )

    def set_rules(self, rules: tuple[HighlightRule, ...]) -> None:
        while self._rows:
            self._remove(self._rows[0][0])
        for rule in rules:
            self._add_row(rule)

    def _add_row(self, rule: HighlightRule, *, focus: bool = False) -> None:
        metrics = tokens.METRICS
        row = QWidget()
        text = QLineEdit(rule.text)
        text.setMaxLength(HIGHLIGHT_TEXT_MAX)
        text.setPlaceholderText("Text to look for")
        text.setAccessibleName("Text to look for")
        text.setFixedWidth(metrics.form_label_width)
        colour = QComboBox()
        colour.setAccessibleName("Colour")
        colour.setFixedWidth(metrics.form_field_width)
        for number, name in enumerate(HIGHLIGHT_COLOUR_NAMES, start=1):
            colour.addItem(_swatch(tokens.palette().highlights[number - 1]), name, number)
        colour.setCurrentIndex(max(0, colour.findData(rule.colour)))
        remove = Button(compact=True)
        remove.setObjectName("flatIconButton")
        remove.setAccessibleName("Remove this rule")
        remove.setToolTip("Remove this rule")
        bind_icon(remove, "close_tab", "text_secondary")
        remove.clicked.connect(lambda: self._remove(row))

        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(metrics.space_6)
        layout.addWidget(text)
        layout.addWidget(colour)
        layout.addWidget(remove)
        layout.addStretch(1)
        self._list.addWidget(row)
        self._rows.append((row, text, colour))
        self._add.setEnabled(len(self._rows) < HIGHLIGHT_RULES_MAX)
        if focus:
            text.setFocus()

    def _remove(self, row: QWidget) -> None:
        self._rows = [entry for entry in self._rows if entry[0] is not row]
        self._list.removeWidget(row)
        row.deleteLater()
        self._add.setEnabled(len(self._rows) < HIGHLIGHT_RULES_MAX)


def _swatch(colour: str) -> QIcon:
    """A square of ``colour``, the size of an indicator."""
    size = tokens.METRICS.indicator
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(colour))
    return QIcon(pixmap)
