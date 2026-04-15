# SPDX-License-Identifier: MIT
"""WireTrace command bar — send commands to the serial device.

Features:
  - Bold "Command" label with fixed width (matches Filter label)
  - Editable QComboBox showing recent command history as dropdown
  - Send button
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

_MAX_HISTORY = 50
_LABEL_W = 64  # Must match FilterBar._LABEL_W


class CommandBar(QWidget):
    """Command input bar with dropdown history."""

    command_sent = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("commandBar")
        self._setup_ui()

    def set_enabled(self, enabled: bool) -> None:
        self._combo.setEnabled(enabled)
        self._send_btn.setEnabled(enabled)

    def _setup_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 3, 8, 3)
        layout.setSpacing(6)

        lbl = QLabel("Command")
        f = lbl.font()
        f.setWeight(QFont.Weight.DemiBold)
        f.setPointSize(10)
        lbl.setFont(f)
        lbl.setFixedWidth(_LABEL_W)

        self._combo = QComboBox()
        self._combo.setEditable(True)
        self._combo.setFixedHeight(26)
        self._combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._combo.lineEdit().setPlaceholderText(
            "Type a command and press Enter..."
        )
        self._combo.lineEdit().returnPressed.connect(self._on_send)

        self._send_btn = QPushButton("Send")
        self._send_btn.setObjectName("sendBtn")
        self._send_btn.setFixedSize(56, 26)
        self._send_btn.clicked.connect(self._on_send)

        layout.addWidget(lbl)
        layout.addWidget(self._combo, 1)
        layout.addWidget(self._send_btn)

    def _on_send(self) -> None:
        text = self._combo.currentText().strip()
        if not text:
            return
        idx = self._combo.findText(text)
        if idx >= 0:
            self._combo.removeItem(idx)
        self._combo.insertItem(0, text)
        if self._combo.count() > _MAX_HISTORY:
            self._combo.removeItem(self._combo.count() - 1)
        self._combo.setCurrentText("")
        self.command_sent.emit(text)
