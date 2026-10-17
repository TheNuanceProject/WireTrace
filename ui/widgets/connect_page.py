# SPDX-License-Identifier: GPL-3.0-or-later
"""The first screen of a tab: choose a serial port and connect.

The list shows every port with its description and, where known, what
it was last used with. It is read again every moment or two, so a
device appears when it is plugged in without anyone pressing Refresh.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QHideEvent, QIntValidator, QKeyEvent, QMouseEvent, QShowEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)

from app.constants import (
    BAUD_RATE_MAX,
    BAUD_RATE_MIN,
    BAUD_RATES,
    DEFAULT_BAUD_RATE,
    PORT_SCAN_INTERVAL_MS,
)
from core.transport import PortInfo
from ui import shortcuts
from ui.formatting import format_count
from ui.themes import tokens
from ui.themes.icons import bind_icon
from ui.widgets.button import Button
from ui.widgets.dot_grid import DotGridWidget
from ui.widgets.styling import (
    INVALID,
    SECONDARY,
    SELECTED,
    plain_label,
    set_primary,
    set_state,
)
from ui.widgets.vertical_scroll import VerticalScrollArea

_NO_PORTS_TITLE = "No serial ports found"
_NO_PORTS_HELP = (
    "Plug a device in and it appears here by itself.\n"
    "If it does not, check the cable and the driver for the device."
)
#: Marks a port that appeared while the screen was open. It says only
#: what WireTrace saw, not why the port appeared.
_NEW_PORT_BADGE = "New"
_NEW_PORT_TOOLTIP = "This port appeared while this screen was open"
#: Shown on Linux only, as one more thing to check and not as the cause.
_LINUX_PERMISSION_NOTE = "On Linux, your user account also needs permission to open serial ports."


class PortRow(QFrame):
    """One port in the list."""

    chosen = Signal(str)
    activated = Signal(str)

    def __init__(
        self,
        port: PortInfo,
        note: str,
        *,
        in_use: bool,
        is_new: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("portRow")
        self.device = port.device
        self.available = not in_use
        metrics = tokens.METRICS
        self.setFixedHeight(metrics.port_row_height)
        self.setEnabled(self.available)
        if self.available:
            self.setCursor(Qt.CursorShape.PointingHandCursor)

        self.name_label = plain_label(port.device)
        self.name_label.setObjectName("portName")
        name = self.name_label
        description = plain_label(port.description or "Serial port")
        detail = plain_label(note or port.manufacturer or "Not used before")
        detail.setProperty(SECONDARY, True)

        text = QVBoxLayout()
        text.setSpacing(metrics.space_half)
        text.addStretch(1)
        text.addWidget(description)
        text.addWidget(detail)
        text.addStretch(1)

        self.badge = plain_label()
        if in_use:
            self.badge.setText("Open in another tab")
            self.badge.setProperty(SECONDARY, True)
        elif is_new:
            self.badge.setText(_NEW_PORT_BADGE)
            self.setToolTip(_NEW_PORT_TOOLTIP)
            self.badge.setObjectName("portBadge")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(metrics.space_4, 0, metrics.space_4, 0)
        layout.setSpacing(metrics.space_4)
        layout.addWidget(name)
        layout.addLayout(text, 1)
        layout.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignVCenter)
        for label in (name, description, detail, self.badge):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def set_selected(self, selected: bool) -> None:
        set_state(self, SELECTED, selected)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.available:
            self.chosen.emit(self.device)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.available:
            self.activated.emit(self.device)


class ConnectPage(DotGridWidget):
    """The first screen of a tab: the port list, the baud rate and Connect.

    Signals:
        connect_requested(str, int):  Port and baud rate to open.
        ports_wanted():               The port list should be read again.
        port_chosen(str):             The user selected a port in the list.
    """

    connect_requested = Signal(str, int)
    ports_wanted = Signal()
    port_chosen = Signal(str)

    def __init__(
        self,
        port_note: Callable[[str], str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, glow=True)
        self.setObjectName("connectPage")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._port_note = port_note or (lambda _device: "")
        self._rows: list[PortRow] = []
        self._selected = ""
        self._known: set[str] | None = None      # None until the first scan
        self._fresh: set[str] = set()
        self._signature: tuple[object, ...] | None = None

        self._scan_timer = QTimer(self)
        self._scan_timer.setInterval(PORT_SCAN_INTERVAL_MS)
        self._scan_timer.timeout.connect(self.ports_wanted)

        self._build()

    # ── Building ─────────────────────────────────────────────────────────

    def _build(self) -> None:
        metrics = tokens.METRICS

        self._title = plain_label("Connect a device")
        self._title.setObjectName("pageTitle")
        self._subtitle = plain_label()
        self._subtitle.setProperty(SECONDARY, True)
        self._subtitle.setWordWrap(True)

        self._list = QFrame()
        self._list.setObjectName("portRows")
        self._list_layout = QVBoxLayout(self._list)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(0)
        self._scroll = VerticalScrollArea(self._list)
        self._scroll.setObjectName("portScroll")

        self._empty = QFrame()
        self._empty.setObjectName("portList")
        empty_title = plain_label(_NO_PORTS_TITLE)
        empty_title.setObjectName("portName")
        help_text = _NO_PORTS_HELP
        if sys.platform.startswith("linux"):
            help_text += f"\n{_LINUX_PERMISSION_NOTE}"
        self._empty_help = plain_label(help_text)
        self._empty_help.setProperty(SECONDARY, True)
        self._empty_help.setWordWrap(True)
        empty_layout = QVBoxLayout(self._empty)
        empty_layout.setContentsMargins(
            metrics.space_6, metrics.space_6, metrics.space_6, metrics.space_6
        )
        empty_layout.setSpacing(metrics.space_2)
        for label in (empty_title, self._empty_help):
            label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            empty_layout.addWidget(label)

        baud_label = plain_label("Baud rate")
        baud_label.setProperty(SECONDARY, True)
        self._baud = QComboBox()
        self._baud.setEditable(True)
        self._baud.setFixedWidth(metrics.baud_field_width)
        self._baud.setAccessibleName("Baud rate")
        for rate in BAUD_RATES:
            self._baud.addItem(str(rate))
        self._baud.setCurrentText(str(DEFAULT_BAUD_RATE))
        self._baud.setValidator(QIntValidator(BAUD_RATE_MIN, BAUD_RATE_MAX, self))
        self._baud.lineEdit().textChanged.connect(self._refresh_connect)
        self._baud.lineEdit().returnPressed.connect(self._connect_selected)

        self._refresh = Button("Refresh")
        self._refresh.setToolTip(shortcuts.with_keys("Read the list of ports again", "refresh_ports"))
        bind_icon(self._refresh, "refresh")
        self._refresh.clicked.connect(self.ports_wanted)

        self._connect = Button("Connect")
        set_primary(self._connect)
        self._connect.clicked.connect(self._connect_selected)

        controls = QHBoxLayout()
        controls.setSpacing(metrics.space_2)
        controls.addWidget(baud_label)
        controls.addWidget(self._baud)
        controls.addWidget(self._refresh)
        controls.addStretch(1)
        controls.addWidget(self._connect)

        rule = QFrame()
        rule.setFrameShape(QFrame.Shape.HLine)
        hints = plain_label("     ".join(
            f"{shortcuts.native_keys(name)}  {shortcuts.SHORTCUTS[name].description}"
            for name in ("new_tab", "start_log", "find")
        ))
        hints.setProperty(SECONDARY, True)

        column = QWidget()
        column.setFixedWidth(metrics.page_column_width)
        column_layout = QVBoxLayout(column)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.setSpacing(metrics.space_2)
        column_layout.addWidget(self._title)
        column_layout.addWidget(self._subtitle)
        column_layout.addSpacing(metrics.space_3)
        column_layout.addWidget(self._scroll)
        column_layout.addWidget(self._empty)
        column_layout.addSpacing(metrics.space_2)
        column_layout.addLayout(controls)
        column_layout.addSpacing(metrics.space_3)
        column_layout.addWidget(rule)
        column_layout.addWidget(hints)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(metrics.space_6, metrics.space_6, metrics.space_6, metrics.space_6)
        outer.setSpacing(0)
        outer.addStretch(2)
        outer.addWidget(column, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(3)

        self.set_ports([], set())
        # That was not a look at the ports: the first real one must not
        # mark every port as new.
        self._known = None

    # ── Public API ───────────────────────────────────────────────────────

    @property
    def selected_port(self) -> str:
        return self._selected

    @property
    def selected_baud(self) -> int | None:
        """The baud rate entered, or None if it is not a valid one."""
        try:
            baud = int(self._baud.currentText().strip())
        except ValueError:
            return None
        return baud if BAUD_RATE_MIN <= baud <= BAUD_RATE_MAX else None

    def set_baud(self, baud: int) -> None:
        self._baud.setCurrentText(str(baud))

    def port_devices(self) -> list[str]:
        return [row.device for row in self._rows]

    def row(self, device: str) -> PortRow | None:
        return next((row for row in self._rows if row.device == device), None)

    def texts(self) -> list[str]:
        """The heading texts shown, for the state the page is in."""
        texts = [self._title.text(), self._subtitle.text()]
        if not self._rows:
            texts.append(self._empty_help.text())
        return texts

    def set_ports(self, ports: list[PortInfo], in_use: set[str]) -> None:
        """Show ``ports``. Ports in ``in_use`` are open in another tab."""
        devices = {port.device for port in ports}
        if self._known is not None:
            self._fresh = (self._fresh | (devices - self._known)) & devices
        newly_plugged = set() if self._known is None else devices - self._known
        self._known = devices

        notes = {port.device: self._port_note(port.device) for port in ports}
        signature = (
            tuple(ports), frozenset(in_use), frozenset(self._fresh), tuple(notes.values()),
        )
        if signature == self._signature:
            return
        self._signature = signature

        for row in self._rows:
            self._list_layout.removeWidget(row)
            row.deleteLater()
        self._rows = []
        for port in ports:
            row = PortRow(
                port, notes[port.device],
                in_use=port.device in in_use,
                is_new=port.device in self._fresh,
            )
            row.chosen.connect(self._select)
            row.activated.connect(self._activate)
            self._list_layout.addWidget(row)
            self._rows.append(row)
        # One width for every port name, so the descriptions line up.
        name_width = max((row.name_label.sizeHint().width() for row in self._rows), default=0)
        for row in self._rows:
            row.name_label.setFixedWidth(name_width)

        available = [row.device for row in self._rows if row.available]
        plugged = [device for device in available if device in newly_plugged]
        if plugged:
            # A port that has just appeared is the one about to be used.
            self._selected = plugged[0]
        elif self._selected not in available:
            self._selected = available[0] if available else ""

        metrics = tokens.METRICS
        visible_rows = min(len(self._rows), metrics.port_rows_visible)
        self._scroll.setFixedHeight(visible_rows * metrics.port_row_height + 2 * metrics.border)
        self._scroll.setVisible(bool(self._rows))
        self._empty.setVisible(not self._rows)
        self._subtitle.setText(
            f"{format_count(len(ports), 'serial port')} found. "
            "The list updates by itself when you plug a device in."
            if ports else "WireTrace is watching for serial ports."
        )
        self._show_selection()
        if self._selected:
            self.port_chosen.emit(self._selected)

    # ── Internal ─────────────────────────────────────────────────────────

    def _select(self, device: str) -> None:
        if device != self._selected:
            self._selected = device
            self._show_selection()
            self.port_chosen.emit(device)

    def _activate(self, device: str) -> None:
        self._select(device)
        self._connect_selected()

    def _show_selection(self) -> None:
        for row in self._rows:
            row.set_selected(row.device == self._selected)
        self._refresh_connect()

    def _refresh_connect(self) -> None:
        text = self._baud.currentText().strip()
        valid = self.selected_baud is not None
        set_state(self._baud.lineEdit(), INVALID, bool(text) and not valid)
        self._connect.setEnabled(bool(self._selected) and valid)

    def _connect_selected(self) -> None:
        baud = self.selected_baud
        if self._selected and baud is not None:
            self.connect_requested.emit(self._selected, baud)

    def _move_selection(self, step: int) -> None:
        available = [row.device for row in self._rows if row.available]
        if not available:
            return
        index = available.index(self._selected) if self._selected in available else -step
        self._select(available[max(0, min(len(available) - 1, index + step))])
        selected = self.row(self._selected)
        if selected is not None:
            self._scroll.ensureWidgetVisible(selected)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key.Key_Up:
            self._move_selection(-1)
        elif key == Qt.Key.Key_Down:
            self._move_selection(1)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._connect_selected()
        else:
            super().keyPressEvent(event)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.ports_wanted.emit()
        self._scan_timer.start()
        self.setFocus()

    def hideEvent(self, event: QHideEvent) -> None:
        super().hideEvent(event)
        self._scan_timer.stop()
