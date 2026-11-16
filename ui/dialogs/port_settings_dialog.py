# SPDX-License-Identifier: GPL-3.0-or-later
"""The Port settings dialog: framing, flow control and the lines set at open."""

from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QComboBox, QVBoxLayout, QWidget

from core.serial_settings import (
    DATA_BITS,
    FLOW_CONTROL_NAMES,
    PARITY_NAMES,
    FlowControl,
    LineSettings,
    parities_for,
    stop_bits_for,
)
from ui.dialogs.common import action_button, button_row, dialog_layout, heading, note, setting_row
from ui.themes import tokens
from ui.widgets.dot_grid import DotGridDialog
from ui.widgets.least_size import keep_least_size

_RTS_BY_FLOW_CONTROL = "With hardware flow control, the driver sets RTS."


class PortSettingsDialog(DotGridDialog):
    """Edits the line settings of one port. Read ``settings`` after it is accepted."""

    def __init__(self, port: str, settings: LineSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Port settings")
        self.setModal(True)
        metrics = tokens.METRICS

        self._data_bits = self._combo()
        for bits in DATA_BITS:
            self._data_bits.addItem(str(bits), bits)
        self._parity = self._combo()
        for parity in parities_for():
            self._parity.addItem(PARITY_NAMES[parity], parity)
        self._stop_bits = self._combo()
        for stop_bits in stop_bits_for():
            self._stop_bits.addItem(stop_bits.value, stop_bits)
        self._flow_control = self._combo()
        for flow_control in FlowControl:
            self._flow_control.addItem(FLOW_CONTROL_NAMES[flow_control], flow_control)
        self._flow_control.currentIndexChanged.connect(self._refresh_rts)
        self._dtr = QCheckBox("On")
        self._rts = QCheckBox("On")
        self._rts_hint = note(small=True)

        rts_field = QWidget()
        rts_layout = QVBoxLayout(rts_field)
        rts_layout.setContentsMargins(0, 0, 0, 0)
        rts_layout.setSpacing(metrics.space_half)
        rts_layout.addWidget(self._rts)
        rts_layout.addWidget(self._rts_hint)

        usual = action_button("Usual settings")
        usual.setToolTip("8 data bits, no parity, 1 stop bit, no flow control, DTR and RTS on")
        usual.clicked.connect(lambda: self._show(LineSettings()))
        cancel = action_button("Cancel")
        cancel.clicked.connect(self.reject)
        save = action_button("Save", primary=True)
        save.setDefault(True)
        save.clicked.connect(self.accept)

        layout = dialog_layout(self)
        layout.addWidget(heading(port, "pageTitle"))
        layout.addWidget(note("Used each time this port is connected. The device decides "
                              "what it needs; most use 8 data bits, no parity and 1 stop bit."))
        for label, hint, field in (
            ("Data bits", "Bits in each character.", self._data_bits),
            ("Parity", "An extra bit for checking each character.", self._parity),
            ("Stop bits", "Bits that end each character.", self._stop_bits),
            ("Flow control", "How the device and the computer pause each other.",
             self._flow_control),
            ("DTR at open", "Many boards reset when DTR or RTS changes.", self._dtr),
            ("RTS at open", "Set as soon as the port opens.", rts_field),
        ):
            layout.addLayout(setting_row(label, hint, field))
        layout.addStretch(1)
        layout.addLayout(button_row(cancel, save, leading=usual))
        keep_least_size(self, metrics.dialog_medium_width)
        self._show(settings)

    @property
    def settings(self) -> LineSettings:
        return LineSettings(
            data_bits=self._data_bits.currentData(),
            parity=self._parity.currentData(),
            stop_bits=self._stop_bits.currentData(),
            flow_control=self._flow_control.currentData(),
            dtr_on_open=self._dtr.isChecked(),
            rts_on_open=self._rts.isChecked(),
        )

    def _show(self, settings: LineSettings) -> None:
        for combo, value in (
            (self._data_bits, settings.data_bits),
            (self._parity, settings.parity),
            (self._stop_bits, settings.stop_bits),
            (self._flow_control, settings.flow_control),
        ):
            combo.setCurrentIndex(max(0, combo.findData(value)))
        self._dtr.setChecked(settings.dtr_on_open)
        self._rts.setChecked(settings.rts_on_open)
        self._refresh_rts()

    def _refresh_rts(self) -> None:
        manual = self._flow_control.currentData() is not FlowControl.HARDWARE
        self._rts.setEnabled(manual)
        self._rts_hint.setText("" if manual else _RTS_BY_FLOW_CONTROL)
        self._rts_hint.setVisible(not manual)

    @staticmethod
    def _combo() -> QComboBox:
        combo = QComboBox()
        combo.setFixedWidth(tokens.METRICS.form_field_width)
        return combo
