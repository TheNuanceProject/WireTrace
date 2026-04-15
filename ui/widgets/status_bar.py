# SPDX-License-Identifier: MIT
"""WireTrace per-tab status bar — connection info, data rate, line count, timestamp mode.

Per spec section 6.2/6.5:
  - Shows: ● COM3 @ 115200 │ 1,247 lines/sec │ 45,230 lines │ Δt: 0.003s
  - Timestamp mode indicator (clickable to toggle absolute/relative)
  - Updates in real-time from SerialReader signals

This is a display-only widget — does NOT touch business logic.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QWidget,
)

from app.constants import TimestampMode


class DeviceStatusBar(QWidget):
    """Per-tab status bar showing connection and data metrics.

    Signals:
        timestamp_mode_toggled(TimestampMode): Emitted when user clicks
            the timestamp indicator to toggle mode.
    """

    timestamp_mode_toggled = Signal(object)  # TimestampMode enum

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("deviceStatusBar")

        self._port_name = ""
        self._baud_rate = 0
        self._data_rate = 0
        self._total_lines = 0
        self._timestamp_mode = TimestampMode.ABSOLUTE

        self._setup_ui()
        self._update_display()

    # ── Public API ───────────────────────────────────────────────────────

    def set_connection_info(self, port: str, baud: int) -> None:
        """Update connection information display."""
        self._port_name = port
        self._baud_rate = baud
        self._update_display()

    def set_data_rate(self, lines_per_sec: int) -> None:
        """Update the data rate display."""
        self._data_rate = lines_per_sec
        self._rate_label.setText(f"{lines_per_sec:,} lines/sec")

    def set_total_lines(self, count: int) -> None:
        """Update the total line count display."""
        self._total_lines = count
        self._lines_label.setText(f"{count:,} lines")

    def set_timestamp_mode(self, mode: TimestampMode) -> None:
        """Update the timestamp mode display."""
        self._timestamp_mode = mode
        self._update_timestamp_indicator()

    def set_status(self, message: str) -> None:
        """Set a temporary status message."""
        self._connection_label.setText(message)

    def set_disconnected(self) -> None:
        """Reset status bar to disconnected state."""
        self._port_name = ""
        self._baud_rate = 0
        self._data_rate = 0
        self._total_lines = 0
        self._update_display()

    # ── Internal Setup ───────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 4, 12, 4)
        layout.setSpacing(0)

        # Connection indicator
        self._connection_label = QLabel("Ready")
        self._connection_label.setMinimumWidth(160)

        # Separator
        sep1 = QLabel("│")
        sep1.setProperty("secondary", True)
        sep1.setContentsMargins(10, 0, 10, 0)

        # Data rate
        self._rate_label = QLabel("0 lines/sec")
        self._rate_label.setMinimumWidth(100)

        # Separator
        sep2 = QLabel("│")
        sep2.setProperty("secondary", True)
        sep2.setContentsMargins(10, 0, 10, 0)

        # Total lines
        self._lines_label = QLabel("0 lines")
        self._lines_label.setMinimumWidth(80)

        # Separator
        sep3 = QLabel("│")
        sep3.setProperty("secondary", True)
        sep3.setContentsMargins(10, 0, 10, 0)

        # Timestamp mode (clickable)
        self._timestamp_label = QLabel()
        self._timestamp_label.setCursor(self._timestamp_label.cursor())
        self._timestamp_label.setToolTip("Click to toggle timestamp format")
        self._timestamp_label.mousePressEvent = self._on_timestamp_clicked
        self._update_timestamp_indicator()

        layout.addWidget(self._connection_label)
        layout.addWidget(sep1)
        layout.addWidget(self._rate_label)
        layout.addWidget(sep2)
        layout.addWidget(self._lines_label)
        layout.addWidget(sep3)
        layout.addWidget(self._timestamp_label)
        layout.addStretch()

    # ── Internal ─────────────────────────────────────────────────────────

    def _update_display(self) -> None:
        """Refresh the connection info display."""
        if self._port_name and self._baud_rate:
            self._connection_label.setText(
                f"● {self._port_name} @ {self._baud_rate}"
            )
        else:
            self._connection_label.setText("Ready")
            self._rate_label.setText("0 lines/sec")
            self._lines_label.setText("0 lines")

    def _update_timestamp_indicator(self) -> None:
        """Update the timestamp mode indicator text."""
        if self._timestamp_mode == TimestampMode.ABSOLUTE:
            self._timestamp_label.setText("🕐  Timestamp")
        else:
            self._timestamp_label.setText("Δt  Elapsed")

    def _on_timestamp_clicked(self, event) -> None:
        """Toggle timestamp mode on click."""
        if self._timestamp_mode == TimestampMode.ABSOLUTE:
            new_mode = TimestampMode.RELATIVE
        else:
            new_mode = TimestampMode.ABSOLUTE

        self._timestamp_mode = new_mode
        self._update_timestamp_indicator()
        self.timestamp_mode_toggled.emit(new_mode)
