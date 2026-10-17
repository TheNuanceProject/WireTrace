# SPDX-License-Identifier: GPL-3.0-or-later
"""The toolbar of a tab: connection, logging, console actions, display mode.

When the window is too narrow for every button, the least used ones
move into a "More" menu at the right end, so nothing is ever cut off.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Signal
from PySide6.QtGui import QIntValidator, QResizeEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLayout,
    QMenu,
    QWidget,
)

from app.constants import (
    BAUD_RATE_MAX,
    BAUD_RATE_MIN,
    BAUD_RATES,
    DEFAULT_BAUD_RATE,
    DisplayMode,
)
from ui import shortcuts
from ui.themes import tokens
from ui.themes.icons import bind_icon
from ui.widgets.button import Button, join_buttons
from ui.widgets.styling import INVALID, set_primary, set_state


class PortCombo(QComboBox):
    """A port list that asks to be refreshed each time it is opened."""

    about_to_open = Signal()

    def showPopup(self) -> None:
        self.about_to_open.emit()
        super().showPopup()


class TabToolbar(QFrame):
    """The toolbar of a device tab.

    Signals:
        connect_requested(str, int):  Port and baud rate to open.
        disconnect_requested():       Close the connection.
        ports_wanted():               The port list should be read again.
        log_toggled():                Start a log, or stop the running one.
        pause_toggled():              Pause the log, or resume it.
        clear_requested():            Clear the console.
        export_requested():           Open Export session.
        plot_toggled(bool):           Show or hide the plot.
        display_mode_changed(object): The ``DisplayMode`` chosen.
        search_requested():           Open the search row.
        hint(str):                    Why a click could not be acted on.
    """

    connect_requested = Signal(str, int)
    disconnect_requested = Signal()
    ports_wanted = Signal()
    log_toggled = Signal()
    pause_toggled = Signal()
    clear_requested = Signal()
    export_requested = Signal()
    plot_toggled = Signal(bool)
    display_mode_changed = Signal(object)
    search_requested = Signal()
    hint = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("tabToolbar")
        metrics = tokens.METRICS
        self.setFixedHeight(metrics.toolbar_height)

        self._connected = False
        self._logging = False
        self._paused = False
        self._has_session = False

        self._build_connection()
        self._build_actions()
        self._build_display()

        self._more = Button()
        self._more.setToolTip("More")
        self._more.setAccessibleName("More")
        self._more.setObjectName("iconButton")
        bind_icon(self._more, "more")
        self._more_menu = QMenu(self)
        self._more.setMenu(self._more_menu)

        layout = QHBoxLayout(self)
        # The row decides for itself what to show at the width it is
        # given, so its contents must not set a least width for the window.
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        layout.setContentsMargins(metrics.space_3, 0, metrics.space_3, 0)
        layout.setSpacing(metrics.space_2)
        dividers = (self._divider(), self._divider())
        for widget in (self._port, self._baud, self._connect, dividers[0], self._log, self._pause,
                       dividers[1], self._clear, self._export, self._plot):
            layout.addWidget(widget)
        layout.addStretch(1)
        layout.addWidget(self._display)
        layout.addWidget(self._search)
        layout.addWidget(self._more)
        # Every widget of the row, for working out what fits.
        self._row: tuple[QWidget, ...] = (
            self._port, self._baud, self._connect, dividers[0], self._log, self._pause,
            dividers[1], self._clear, self._export, self._plot,
            self._display, self._search, self._more,
        )

        # What gives way when the row is too narrow, first to go first:
        # the widget that leaves the row, the buttons it stands for, and
        # the label in the More menu (a button's own text when empty).
        self._overflow: tuple[tuple[QWidget, tuple[Button, ...], str], ...] = (
            (self._plot, (self._plot,), "Plot"),
            (self._export, (self._export,), "Export session"),
            (self._clear, (self._clear,), "Clear console"),
            (self._display, (self._text_mode, self._hex_mode), ""),
            (self._search, (self._search,), "Find"),
        )
        self._refresh_states()

    # ── Building ─────────────────────────────────────────────────────────

    def _build_connection(self) -> None:
        metrics = tokens.METRICS
        self._port = PortCombo()
        self._port.setMinimumWidth(metrics.port_field_min_width)
        self._port.setMaximumWidth(metrics.port_field_width)
        self._port.setPlaceholderText("Choose a port")
        self._port.setAccessibleName("Serial port")
        self._port.about_to_open.connect(self.ports_wanted)

        self._baud = QComboBox()
        self._baud.setEditable(True)
        self._baud.setFixedWidth(metrics.baud_field_width)
        self._baud.setAccessibleName("Baud rate")
        for rate in BAUD_RATES:
            self._baud.addItem(str(rate))
        self._baud.setCurrentText(str(DEFAULT_BAUD_RATE))
        self._baud.setValidator(QIntValidator(BAUD_RATE_MIN, BAUD_RATE_MAX, self))
        self._baud.lineEdit().textChanged.connect(self._mark_baud)

        self._connect = Button()
        self._connect.clicked.connect(self._on_connect_clicked)

    def _build_actions(self) -> None:
        self._log = self._button("", "log_on", self.log_toggled)
        self._pause = self._button("", "pause", self.pause_toggled)
        self._clear = self._button("Clear", "clear", self.clear_requested)
        self._clear.setToolTip("Clear the console. An export then starts from here.")
        self._export = self._button("Export", "export", self.export_requested)
        self._export.setToolTip(shortcuts.with_keys(
            "Save everything since you connected, or since the last Clear", "export",
        ))
        self._plot = self._button("Plot", "plot")
        self._plot.setCheckable(True)
        self._plot.setToolTip(shortcuts.with_keys("Show or hide the live plot", "plot"))
        self._plot.toggled.connect(self.plot_toggled)

    def _build_display(self) -> None:
        self._text_mode = Button("Text")
        self._hex_mode = Button("HEX")
        group = QButtonGroup(self)
        group.setExclusive(True)
        for button, mode in (
            (self._text_mode, DisplayMode.TEXT),
            (self._hex_mode, DisplayMode.HEX),
        ):
            button.setCheckable(True)
            button.clicked.connect(lambda _checked, m=mode: self.display_mode_changed.emit(m))
            group.addButton(button)
        self._text_mode.setChecked(True)
        self._text_mode.setToolTip("Show received data as text")
        self._hex_mode.setToolTip("Show received data as bytes in hexadecimal")
        self._display = join_buttons(self._text_mode, self._hex_mode)

        self._search = Button()
        self._search.setToolTip(shortcuts.with_keys("Find text in the console", "find"))
        self._search.setAccessibleName("Find")
        self._search.setObjectName("iconButton")
        bind_icon(self._search, "search")
        self._search.clicked.connect(self.search_requested)

    def _button(self, text: str, icon_name: str, signal: Signal | None = None) -> Button:
        button = Button(text)
        bind_icon(button, icon_name)
        if signal is not None:
            button.clicked.connect(signal)
        return button

    @staticmethod
    def _divider() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.VLine)
        line.setFixedHeight(tokens.METRICS.space_6)
        return line

    # ── State ────────────────────────────────────────────────────────────

    def set_ports(self, ports: list[tuple[str, str]], in_use: set[str]) -> None:
        """Fill the port list with ``(device, display name)`` pairs."""
        current = self.selected_port
        self._port.blockSignals(True)
        self._port.clear()
        for device, display in ports:
            taken = device in in_use
            self._port.addItem(f"{display}  (open in another tab)" if taken else display, device)
            if taken:
                self._port.model().item(self._port.count() - 1).setEnabled(False)
        self._port.setCurrentIndex(self._port.findData(current))
        self._port.blockSignals(False)

    def select(self, port: str, baud: int) -> None:
        """Show ``port`` and ``baud`` as the chosen connection."""
        index = self._port.findData(port)
        if index < 0:
            self._port.addItem(port, port)
            index = self._port.count() - 1
        self._port.setCurrentIndex(index)
        self._baud.setCurrentText(str(baud))

    def set_connected(self, connected: bool) -> None:
        self._connected = connected
        self._has_session = self._has_session or connected
        if not connected:
            self._logging = self._paused = False
        self._refresh_states()

    def set_logging(self, logging: bool, paused: bool = False) -> None:
        self._logging = logging
        self._paused = logging and paused
        self._refresh_states()

    def set_plot_open(self, is_open: bool) -> None:
        """Show the Plot button as on or off without announcing a toggle."""
        self._plot.blockSignals(True)
        self._plot.setChecked(is_open)
        self._plot.blockSignals(False)

    def set_display_mode(self, mode: DisplayMode) -> None:
        (self._hex_mode if mode is DisplayMode.HEX else self._text_mode).setChecked(True)

    @property
    def selected_port(self) -> str:
        return self._port.currentData() or ""

    @property
    def selected_baud(self) -> int | None:
        """The baud rate entered, or None if it is not a valid one."""
        try:
            baud = int(self._baud.currentText().strip())
        except ValueError:
            return None
        return baud if BAUD_RATE_MIN <= baud <= BAUD_RATE_MAX else None

    def set_baud_text(self, text: str) -> None:
        self._baud.setCurrentText(text)

    def button_texts(self) -> dict[str, str]:
        return {"connect": self._connect.text(), "log": self._log.text(), "pause": self._pause.text()}

    def overflowed_labels(self) -> list[str]:
        """Labels of the entries currently in the More menu."""
        return [action.text() for action in self._more_menu.actions()]

    def _refresh_states(self) -> None:
        connected, logging = self._connected, self._logging
        self._port.setEnabled(not connected)
        self._baud.setEnabled(not connected)
        self._connect.setText("Disconnect" if connected else "Connect")
        # Connect is the main action; Disconnect is an ordinary button.
        set_primary(self._connect, not connected)

        self._log.setText("Stop log" if logging else "Start log")
        self._log.setToolTip(
            "Stop logging and close the files" if logging
            else shortcuts.with_keys("Start writing received lines to a file", "start_log")
        )
        bind_icon(self._log, "log_off" if logging else "log_on", "logging" if not logging else "text")
        self._log.setEnabled(connected)

        self._pause.setText("Resume" if self._paused else "Pause")
        self._pause.setToolTip(
            "Resume writing to the log" if self._paused
            else "Stop writing to the log for a while. Lines are still received."
        )
        bind_icon(self._pause, "resume" if self._paused else "pause")
        self._pause.setEnabled(logging)

        # The console and its recording stay after a disconnect, so they
        # can still be cleared and exported.
        self._clear.setEnabled(self._has_session)
        self._export.setEnabled(self._has_session)
        self._plot.setEnabled(connected)
        self._text_mode.setEnabled(connected)
        self._hex_mode.setEnabled(connected)
        self._search.setEnabled(self._has_session)
        self._fit()

    # ── Connection clicks ────────────────────────────────────────────────

    def _mark_baud(self) -> None:
        text = self._baud.currentText().strip()
        invalid = bool(text) and self.selected_baud is None
        set_state(self._baud.lineEdit(), INVALID, invalid)
        self._baud.lineEdit().setToolTip(
            f"Enter a whole number from {BAUD_RATE_MIN:,} to {BAUD_RATE_MAX:,}" if invalid else ""
        )

    def _on_connect_clicked(self) -> None:
        if self._connected:
            self.disconnect_requested.emit()
            return
        if not self.selected_port:
            self.hint.emit("Choose a serial port first")
            return
        baud = self.selected_baud
        if baud is None:
            self.hint.emit(
                f"The baud rate must be a whole number from {BAUD_RATE_MIN:,} to {BAUD_RATE_MAX:,}"
            )
            self._baud.lineEdit().setFocus()
            self._baud.lineEdit().selectAll()
            return
        self.connect_requested.emit(self.selected_port, baud)

    # ── Narrow windows ───────────────────────────────────────────────────

    def _row_width(self, shown: list[QWidget]) -> int:
        """The width the row needs to show ``shown``, the port list at its least."""
        layout = self.layout()
        margins = layout.contentsMargins()
        widths = sum(
            widget.minimumWidth() if widget is self._port else widget.sizeHint().width()
            for widget in shown
        )
        return margins.left() + margins.right() + widths + layout.spacing() * len(shown)

    def _fit(self) -> None:
        """Move buttons that do not fit into the More menu."""
        for widget, _, _ in self._overflow:
            widget.setVisible(True)
        self._more.setVisible(False)
        self._more_menu.clear()

        def needed() -> int:
            return self._row_width([widget for widget in self._row if not widget.isHidden()])

        for widget, buttons, label in self._overflow:
            if needed() <= self.width():
                break
            self._more.setVisible(True)
            widget.setVisible(False)
            for button in buttons:
                self._add_to_more(button, label)

    def _add_to_more(self, button: Button, label: str) -> None:
        action = self._more_menu.addAction(label or button.text())
        action.setEnabled(button.isEnabled())
        if button.isCheckable():
            action.setCheckable(True)
            action.setChecked(button.isChecked())
        action.triggered.connect(button.click)

    def minimumSizeHint(self) -> QSize:
        """The width of what never gives way: the connection, the log
        buttons and More, which holds everything else."""
        gives_way = {widget for widget, _, _ in self._overflow}
        essentials = [widget for widget in self._row if widget not in gives_way]
        return QSize(self._row_width(essentials), tokens.METRICS.toolbar_height)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._fit()
