# SPDX-License-Identifier: GPL-3.0-or-later
"""The status bar of a tab: connection, input lines, line rate, line count
and log state; and, at its right, a newer release to update to, or since
when updates have not been checked."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QPainter, QPaintEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QWidget

from app.constants import FIELD_SEPARATOR
from core.log_writer import LogProgress
from core.serial_settings import LineStates
from ui.formatting import format_count, format_duration, format_size, input_line_texts
from ui.themes import tokens
from ui.tour.targets import Target, mark
from ui.widgets.button import Button
from ui.widgets.status_dot import LinkState, StatusDot
from ui.widgets.styling import ACCENT, QUIET, SECONDARY, plain_label, plain_tooltip, set_state

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
    """Per-tab status bar.

    Signals:
        update_requested():  "Update to ..." was pressed.
        check_requested():   "Check now" was pressed.
    """

    update_requested = Signal()
    check_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("deviceStatusBar")
        mark(self, Target.STATUS_BAR)
        metrics = tokens.METRICS
        self.setFixedHeight(metrics.status_height)

        self._link_state = LinkState.IDLE
        self._dot = StatusDot()
        self._connection = plain_label()
        self._detail = self._secondary_label()
        self._input_lines = [
            mark(self._secondary_label(name), Target.INPUT_LINES) for name, _ in input_line_texts(None)
        ]
        self._inputs_separator = self._separator()
        self._rate = self._secondary_label()
        self._lines = self._secondary_label()
        self._log = ElidedLabel()
        self._log.setProperty(SECONDARY, True)
        mark(self._log, Target.LOG_STATE)
        self._encoding = self._secondary_label(_ENCODING)
        self._update_separator = self._separator()
        self._update = self._quiet_button(accent=True)
        self._update.clicked.connect(self.update_requested)
        self._failing_separator = self._separator()
        self._failing = self._secondary_label()
        self._check_now = self._quiet_button("Check now")
        self._check_now.setToolTip("Check for updates now")
        self._check_now.clicked.connect(self.check_requested)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(metrics.space_3, 0, metrics.space_3, 0)
        layout.setSpacing(metrics.space_2)
        layout.addWidget(self._dot)
        layout.addWidget(self._connection)
        layout.addWidget(self._detail)
        layout.addWidget(self._inputs_separator)
        for label in self._input_lines:
            layout.addWidget(label)
        for widget in (self._rate, self._lines):
            layout.addWidget(self._separator())
            layout.addWidget(widget)
        layout.addWidget(self._separator())
        layout.addWidget(self._log, 1)
        layout.addWidget(self._encoding)
        for widget in (
            self._update_separator, self._update,
            self._failing_separator, self._failing, self._check_now,
        ):
            layout.addWidget(widget)

        self._offered: str | None = None
        self._failing_since: str | None = None
        self.set_idle()
        self.set_line_states(None)
        self.set_not_logging()
        self.set_data_rate(0)
        self.set_total_lines(0)
        self._show_updates()

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

    def set_connected(self, port: str, baud: int, framing: str) -> None:
        self._set_link_state(LinkState.CONNECTED)
        self._connection.setText(port)
        self._detail.setText(f"{baud} {framing}")

    def set_line_states(self, states: LineStates | None) -> None:
        """Show the input lines of the open port; hidden while not connected.

        A line that is on is drawn in the main text colour, one that is
        off or not reported in the secondary colour.
        """
        shown = states is not None
        self._inputs_separator.setVisible(shown)
        for label, (name, state) in zip(self._input_lines, input_line_texts(states), strict=True):
            label.setVisible(shown)
            set_state(label, SECONDARY, state != "on")
            label.setToolTip(f"{name} is {state}")

    def set_waiting(self, port: str, since: str) -> None:
        """The connection was lost and the tab waits for the board."""
        self._set_link_state(LinkState.WAITING)
        self._connection.setText("Waiting")
        self._detail.setText(f"for {port} since {since}")
        self.set_data_rate(0)

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

    # ── Updates ──────────────────────────────────────────────────────────

    def set_update_offer(self, version: str | None) -> None:
        """Offer the newer release ``version``, until it is None."""
        self._offered = version
        self._show_updates()

    def set_checks_failing(self, since: str | None) -> None:
        """Say that updates have not been checked since the day ``since``,
        until it is None. Not shown while a release is offered, which is
        already the way forward."""
        self._failing_since = since
        self._show_updates()

    def update_texts(self) -> list[str]:
        """What the update part of the bar shows, left to right."""
        widgets = (self._update, self._failing, self._check_now)
        return [widget.text() for widget in widgets if not widget.isHidden()]

    def _show_updates(self) -> None:
        offered = self._offered is not None
        if offered:
            self._update.setText(f"Update to {self._offered}")
            self._update.setToolTip(f"See what is new in {self._offered} and update")
        failing = not offered and self._failing_since is not None
        if failing:
            self._failing.setText(f"Updates not checked since {self._failing_since}")
        for widget in (self._update_separator, self._update):
            widget.setVisible(offered)
        for widget in (self._failing_separator, self._failing, self._check_now):
            widget.setVisible(failing)

    def texts(self) -> list[str]:
        """Every text the bar shows, left to right."""
        labels = (self._connection, self._detail, self._rate, self._lines, self._log, self._encoding)
        return [label.text() for label in labels]

    def input_line_states(self) -> dict[str, bool]:
        """Each input line shown, and whether it is drawn as on."""
        return {
            label.text(): not label.property(SECONDARY)
            for label in self._input_lines if not label.isHidden()
        }

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
    def _quiet_button(text: str = "", *, accent: bool = False) -> Button:
        button = Button(text, compact=True)
        set_state(button, QUIET, True)
        if accent:
            set_state(button, ACCENT, True)
        return button

    @staticmethod
    def _separator() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.VLine)
        line.setFixedHeight(tokens.METRICS.space_4)
        return line
