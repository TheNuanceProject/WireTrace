# SPDX-License-Identifier: MIT
"""WireTrace log control bar — Log On/Off, Pause/Resume, Clear, Export.

All buttons uniform height (26px) matching connection panel.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QPushButton,
    QWidget,
)

from app.icon_loader import icon

_BTN_H = 26


class LogControlBar(QWidget):
    """Toolbar for log session controls."""

    log_on_clicked = Signal()
    log_off_clicked = Signal()
    pause_clicked = Signal()
    clear_clicked = Signal()
    export_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._is_logging = False
        self._is_paused = False
        self.setObjectName("logControlBar")
        self._setup_ui()
        self._update_button_states()

    def set_logging_state(self, is_logging: bool, is_paused: bool = False) -> None:
        self._is_logging = is_logging
        self._is_paused = is_paused
        self._update_button_states()

    def set_connected(self, connected: bool) -> None:
        self._log_on_btn.setEnabled(connected and not self._is_logging)
        self._clear_btn.setEnabled(connected)
        self._export_btn.setEnabled(connected)
        if not connected:
            self._is_logging = False
            self._is_paused = False
            self._update_button_states()

    def _setup_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(6)

        self._log_on_btn = self._make_btn("Log On", "logOnBtn", "log_on")
        self._log_on_btn.setToolTip("Start a new log session (Ctrl+N)")
        self._log_on_btn.clicked.connect(self.log_on_clicked)

        self._pause_btn = self._make_btn("Pause", "pauseBtn", "pause")
        self._pause_btn.setToolTip("Pause/resume logging")
        self._pause_btn.clicked.connect(self._on_pause_clicked)

        self._log_off_btn = self._make_btn("Log Off", "logOffBtn", "log_off")
        self._log_off_btn.setToolTip("Stop logging and close files")
        self._log_off_btn.clicked.connect(self.log_off_clicked)

        self._clear_btn = self._make_btn("Clear", "clearBtn", "clear")
        self._clear_btn.setToolTip("Clear console display")
        self._clear_btn.clicked.connect(self.clear_clicked)

        self._export_btn = self._make_btn("Export", "exportBtn", "export")
        self._export_btn.setToolTip("Export log data (Ctrl+E)")
        self._export_btn.clicked.connect(self.export_clicked)

        layout.addWidget(self._log_on_btn)
        layout.addWidget(self._pause_btn)
        layout.addWidget(self._log_off_btn)
        layout.addWidget(self._clear_btn)
        layout.addWidget(self._export_btn)
        layout.addStretch()

    def _make_btn(self, text: str, obj_name: str, icon_name: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName(obj_name)
        btn.setFixedHeight(_BTN_H)
        btn.setMinimumWidth(60)
        ic = icon(icon_name)
        if not ic.isNull():
            btn.setIcon(ic)
        return btn

    def _update_button_states(self) -> None:
        if self._is_logging:
            self._log_on_btn.setEnabled(False)
            self._pause_btn.setEnabled(True)
            self._log_off_btn.setEnabled(True)
            if self._is_paused:
                self._pause_btn.setText("Resume")
                ri = icon("resume")
                if not ri.isNull():
                    self._pause_btn.setIcon(ri)
            else:
                self._pause_btn.setText("Pause")
                pi = icon("pause")
                if not pi.isNull():
                    self._pause_btn.setIcon(pi)
        else:
            self._log_on_btn.setEnabled(True)
            self._pause_btn.setEnabled(False)
            self._pause_btn.setText("Pause")
            self._log_off_btn.setEnabled(False)

    def _on_pause_clicked(self) -> None:
        self._is_paused = not self._is_paused
        self._update_button_states()
        self.pause_clicked.emit()
