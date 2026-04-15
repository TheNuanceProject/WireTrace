# SPDX-License-Identifier: MIT
"""WireTrace connection panel — port selection, baud rate, connect/disconnect.

All controls are uniform height (30px) for visual alignment and
full text visibility across all platform DPI settings.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Signal
from PySide6.QtGui import QFont, QIntValidator
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

from app.constants import (
    BAUD_RATE_MAX,
    BAUD_RATE_MIN,
    BAUD_RATES,
    DEFAULT_BAUD_RATE,
)
from app.icon_loader import icon

logger = logging.getLogger(__name__)

_CTRL_H = 30  # Uniform control height — fits text at all DPI scales


class ConnectionPanel(QWidget):
    """Port selection and connection controls."""

    connect_requested = Signal(str, int)
    disconnect_requested = Signal()
    refresh_requested = Signal()
    error_feedback = Signal(str)  # User-facing error for Toast display

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._is_connected = False
        self.setObjectName("connectionPanel")
        self._setup_ui()

    # ── Public API ───────────────────────────────────────────────────────

    def set_ports(
        self,
        ports: list[tuple[str, str]],
        in_use_ports: set[str] | None = None,
    ) -> None:
        current = self._port_combo.currentData() or ""
        self._port_combo.clear()
        in_use = in_use_ports or set()

        for device, display in ports:
            if device in in_use:
                self._port_combo.addItem(
                    f"{display}  (in use)", userData=device
                )
            else:
                self._port_combo.addItem(display, userData=device)

        # Disable in-use items
        model = self._port_combo.model()
        for i in range(self._port_combo.count()):
            device = self._port_combo.itemData(i)
            if device in in_use:
                item = model.item(i)
                if item:
                    item.setEnabled(False)

        # Restore previous selection
        idx = self._port_combo.findData(current)
        if idx >= 0:
            self._port_combo.setCurrentIndex(idx)

    def set_connected(self, connected: bool) -> None:
        self._is_connected = connected
        self._port_combo.setEnabled(not connected)
        self._baud_combo.setEnabled(not connected)
        self._refresh_btn.setEnabled(not connected)

        if connected:
            self._connect_btn.setText("Disconnect")
            self._connect_btn.setProperty("connected", True)
        else:
            self._connect_btn.setText("Connect")
            self._connect_btn.setProperty("connected", False)

        self._connect_btn.style().unpolish(self._connect_btn)
        self._connect_btn.style().polish(self._connect_btn)

    @property
    def selected_port(self) -> str:
        return self._port_combo.currentData() or ""

    @property
    def selected_baud(self) -> int:
        text = self._baud_combo.currentText().strip()
        try:
            baud = int(text)
            if BAUD_RATE_MIN <= baud <= BAUD_RATE_MAX:
                return baud
        except ValueError:
            pass
        return DEFAULT_BAUD_RATE

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    # ── Internal Setup ───────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(6)

        port_label = QLabel("Port")
        pf = port_label.font()
        pf.setWeight(QFont.Weight.DemiBold)
        port_label.setFont(pf)

        self._port_combo = QComboBox()
        self._port_combo.setMinimumWidth(200)
        self._port_combo.setFixedHeight(_CTRL_H)
        self._port_combo.setPlaceholderText("Select a port...")

        baud_label = QLabel("Baud")
        baud_label.setFont(pf)

        self._baud_combo = QComboBox()
        self._baud_combo.setEditable(True)
        self._baud_combo.setMinimumWidth(130)
        self._baud_combo.setFixedHeight(_CTRL_H)

        for rate in BAUD_RATES:
            self._baud_combo.addItem(str(rate))

        default_idx = self._baud_combo.findText(str(DEFAULT_BAUD_RATE))
        if default_idx >= 0:
            self._baud_combo.setCurrentIndex(default_idx)

        validator = QIntValidator(BAUD_RATE_MIN, BAUD_RATE_MAX, self)
        self._baud_combo.setValidator(validator)
        self._baud_combo.lineEdit().textChanged.connect(self._validate_baud)

        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setFixedHeight(_CTRL_H)
        self._connect_btn.setMinimumWidth(100)
        self._connect_btn.clicked.connect(self._on_connect_clicked)

        self._refresh_btn = QPushButton()
        self._refresh_btn.setObjectName("refreshBtn")
        self._refresh_btn.setFixedSize(_CTRL_H, _CTRL_H)
        self._refresh_btn.setToolTip("Refresh port list")
        ri = icon("refresh")
        if ri.isNull():
            self._refresh_btn.setText("⟳")
        else:
            self._refresh_btn.setIcon(ri)
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)

        layout.addWidget(port_label)
        layout.addWidget(self._port_combo)
        layout.addWidget(baud_label)
        layout.addWidget(self._baud_combo)
        layout.addWidget(self._connect_btn)
        layout.addWidget(self._refresh_btn)
        layout.addStretch()

    def _on_connect_clicked(self) -> None:
        if self._is_connected:
            self.disconnect_requested.emit()
            return

        port = self.selected_port
        if not port:
            self.error_feedback.emit(
                "Please select a serial port before connecting"
            )
            return

        if not self._is_baud_valid():
            text = self._baud_combo.currentText().strip()
            if not text:
                self.error_feedback.emit(
                    "Please enter a baud rate before connecting"
                )
            else:
                self.error_feedback.emit(
                    f"Invalid baud rate \"{text}\" — "
                    f"enter a value between {BAUD_RATE_MIN:,} "
                    f"and {BAUD_RATE_MAX:,}"
                )
            self._baud_combo.lineEdit().setFocus()
            self._baud_combo.lineEdit().selectAll()
            return

        self.connect_requested.emit(port, self.selected_baud)

    def _on_refresh_clicked(self) -> None:
        self.refresh_requested.emit()

    def _validate_baud(self, text: str) -> None:
        """Validate baud rate input and show visual feedback."""
        line_edit = self._baud_combo.lineEdit()
        if not text.strip():
            line_edit.setStyleSheet("")
            line_edit.setToolTip("")
            return

        try:
            value = int(text)
            if BAUD_RATE_MIN <= value <= BAUD_RATE_MAX:
                line_edit.setStyleSheet("")
                line_edit.setToolTip("")
                return
        except ValueError:
            pass

        # Invalid — show red border and tooltip
        line_edit.setStyleSheet("border: 1px solid #D32F2F;")
        line_edit.setToolTip(
            f"Baud rate must be an integer between "
            f"{BAUD_RATE_MIN:,} and {BAUD_RATE_MAX:,}"
        )

    def _is_baud_valid(self) -> bool:
        """Return True if the current baud rate text is valid."""
        text = self._baud_combo.currentText().strip()
        try:
            value = int(text)
            return BAUD_RATE_MIN <= value <= BAUD_RATE_MAX
        except ValueError:
            return False
