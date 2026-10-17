# SPDX-License-Identifier: GPL-3.0-or-later
"""The status bar of a tab: connection, line rate, line count and log state."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPainter, QPaintEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QWidget

from app.constants import FIELD_SEPARATOR
from core.log_writer import LogProgress
from ui.formatting import format_count, format_duration, format_size
from ui.themes import tokens
from ui.widgets.status_dot import LinkState, StatusDot
from ui.widgets.styling import SECONDARY, plain_label, plain_tooltip

#: The framing every connection uses: 8 data bits, no parity, 1 stop bit.
_FRAMING = "8N1"
#: Received bytes are read as this encoding.
_ENCODING = "UTF-8"


class ElidedLabel(QLabel):
    """A label that shortens its text in the middle when there is no room.

    The log status keeps its start (the state and the session name) and
    its end (the time and the size), and the full text is in the tooltip.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # The text names ports and files: it is shown as it is.
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def setText(self, text: str) -> None:
        super().setText(text)
        self.setToolTip(plain_tooltip(text) if text else "")
        self.updateGeometry()

    def sizeHint(self) -> QSize:
        metrics = self.fontMetrics()
        return QSize(metrics.horizontalAdvance(self.text()), metrics.height())

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.fontMetrics().height())

    def shown_text(self) -> str:
        """The text as it is drawn at the current width."""
        return self.fontMetrics().elidedText(
            self.text(), Qt.TextElideMode.ElideMiddle, self.width()
        )

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.drawText(
            self.rect(),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            self.shown_text(),
        )
        painter.end()


class DeviceStatusBar(QFrame):
    """Per-tab status bar."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("deviceStatusBar")
        metrics = tokens.METRICS
        self.setFixedHeight(metrics.status_height)

        self._link_state = LinkState.IDLE
        self._dot = StatusDot()
        self._connection = plain_label()
        self._detail = self._secondary_label()
        self._rate = self._secondary_label()
        self._lines = self._secondary_label()
        self._log = ElidedLabel()
        self._log.setProperty(SECONDARY, True)
        self._encoding = self._secondary_label(_ENCODING)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(metrics.space_3, 0, metrics.space_3, 0)
        layout.setSpacing(metrics.space_2)
        layout.addWidget(self._dot)
        layout.addWidget(self._connection)
        layout.addWidget(self._detail)
        for widget in (self._rate, self._lines):
            layout.addWidget(self._separator())
            layout.addWidget(widget)
        layout.addWidget(self._separator())
        layout.addWidget(self._log, 1)
        layout.addWidget(self._encoding)

        self.set_idle()
        self.set_not_logging()
        self.set_data_rate(0)
        self.set_total_lines(0)

    # ── Connection ───────────────────────────────────────────────────────

    def set_idle(self) -> None:
        """No device has been connected in this tab."""
        self._set_link_state(LinkState.IDLE)
        self._connection.setText("No device connected")
        self._detail.setText("")

    def set_connecting(self, port: str) -> None:
        self._set_link_state(LinkState.IDLE)
        self._connection.setText(f"Connecting to {port}")
        self._detail.setText("")

    def set_connected(self, port: str, baud: int) -> None:
        self._set_link_state(LinkState.CONNECTED)
        self._connection.setText(port)
        self._detail.setText(f"{baud} {_FRAMING}")

    def set_disconnected(self, port: str, detail: str) -> None:
        """The connection has ended; ``detail`` says how and when."""
        self._set_link_state(LinkState.IDLE)
        self._connection.setText("Disconnected")
        self._detail.setText(f"{port}, {detail}" if detail else port)
        self.set_data_rate(0)

    # ── Lines ────────────────────────────────────────────────────────────

    def set_data_rate(self, lines_per_second: int) -> None:
        self._rate.setText(f"{lines_per_second:,} lines/s")

    def set_total_lines(self, count: int) -> None:
        self._lines.setText(format_count(count, "line"))

    # ── Log ──────────────────────────────────────────────────────────────

    def set_not_logging(self) -> None:
        self._log.setText("Not logging")
        self._dot.set_state(self._link_state)

    def set_logging(
        self,
        file_name: str,
        elapsed_seconds: float,
        progress: LogProgress | None,
        *,
        paused: bool,
    ) -> None:
        """Show the running log: its file, how long, and how large."""
        parts = ["Paused" if paused else "Logging", file_name, format_duration(elapsed_seconds)]
        if progress is not None:
            size = format_size(progress.part_bytes)
            if progress.split_bytes:
                size += f" of {format_size(progress.split_bytes)}"
            parts.append(f"file {progress.part_number}, {size}")
        self._log.setText(FIELD_SEPARATOR.join(parts))
        self._dot.set_state(LinkState.LOGGING)

    def texts(self) -> list[str]:
        """Every text the bar shows, left to right."""
        labels = (self._connection, self._detail, self._rate, self._lines, self._log, self._encoding)
        return [label.text() for label in labels]

    # ── Internal ─────────────────────────────────────────────────────────

    def _set_link_state(self, state: LinkState) -> None:
        self._link_state = state
        self._dot.set_state(state)

    @staticmethod
    def _secondary_label(text: str = "") -> QLabel:
        label = plain_label(text)
        label.setProperty(SECONDARY, True)
        return label

    @staticmethod
    def _separator() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.VLine)
        line.setFixedHeight(tokens.METRICS.space_4)
        return line
