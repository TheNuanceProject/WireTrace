# SPDX-License-Identifier: MIT
"""WireTrace filter bar — real-time line filtering for console display.

Layout matches CommandBar exactly:
  - Margins: 8, 3, 8, 3
  - Spacing: 6
  - Label: bold, 64px fixed width
  - Input: height 26

Does NOT affect disk log.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QWidget,
)

_LABEL_W = 64  # Must match CommandBar._LABEL_W


class FilterBar(QWidget):
    """Compact filter bar for real-time console line filtering."""

    filter_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("filterBar")
        self._setup_ui()

    def focus_input(self) -> None:
        self._input.setFocus()

    def clear_filter(self) -> None:
        self._input.clear()

    def update_counts(self, visible: int, total: int) -> None:
        if self._input.text().strip():
            self._count_label.setText(f"{visible}/{total}")
            self._count_label.show()
        else:
            self._count_label.hide()

    def _setup_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 3, 8, 3)
        layout.setSpacing(6)

        lbl = QLabel("Filter")
        f = lbl.font()
        f.setWeight(QFont.Weight.DemiBold)
        f.setPointSize(10)
        lbl.setFont(f)
        lbl.setFixedWidth(_LABEL_W)

        self._input = QLineEdit()
        self._input.setPlaceholderText("Type to filter lines...")
        self._input.setMaximumWidth(260)
        self._input.setFixedHeight(26)
        self._input.setClearButtonEnabled(True)
        self._input.textChanged.connect(self._on_changed)

        self._count_label = QLabel("")
        self._count_label.setMinimumWidth(48)
        self._count_label.hide()

        layout.addWidget(lbl)
        layout.addWidget(self._input)
        layout.addWidget(self._count_label)
        layout.addStretch()

    def _on_changed(self, text: str) -> None:
        self.filter_changed.emit(text)
        if not text.strip():
            self._count_label.hide()
