# SPDX-License-Identifier: GPL-3.0-or-later
"""The end-of-test report: its text, and opening and saving it from a tab."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QFileDialog

from app.config import ConfigManager
from app.constants import ThemeID
from app.modules import RESTARTS, ModuleSettings
from core.session_counters import Disconnect, Restart, SessionFigures
from tests_gui.support import LoopbackPort, wait_until
from ui.device_tab import DeviceTab
from ui.dialogs.session_report_dialog import SessionReportDialog
from ui.features.session_report import session_report
from ui.themes.theme_manager import ThemeManager
from ui.widgets.connect_page import ConnectPage
from ui.widgets.toast import Toast
from updater.usage import Feature

_START = datetime(2026, 9, 1, 8, 0, 0)


def _figures(**changes) -> SessionFigures:
    values = {
        "port": "COM7", "baud": 115200, "started": _START, "lines": 12_480,
        "by_level": {"ERROR": 3, "CRITICAL": 1, "WARNING": 7, "DATA": 12_469},
        "not_recorded": 0, "restart_count": 2,
        "restarts": (
            Restart(_START + timedelta(minutes=5, milliseconds=250), "POWERON_RESET"),
            Restart(_START + timedelta(minutes=9), "Zephyr boot banner"),
        ),
        "disconnect_count": 0, "seconds_away": 0.0, "disconnects": (),
        "ended": _START + timedelta(hours=2, seconds=5), "ended_unexpectedly": True,
    }
    values.update(changes)
    return SessionFigures(**values)


def test_the_report_states_each_figure_once() -> None:
    text = session_report(
        _figures(), _START + timedelta(hours=3),
        title="Pump", framing="8N1, no flow control", restarts_counted=True,
    )

    assert text.splitlines() == [
        "WireTrace session report: Pump",
        "",
        "Port: COM7 at 115,200 baud, 8N1, no flow control",
        "Connected: 2026-09-01 08:00:00",
        "Disconnected: 2026-09-01 10:00:05, the connection was lost",
        "Running time: 02:00:05",
        "",
        "Lines received: 12,480",
        "Errors: 4",
        "Warnings: 7",
        "Lines not recorded (the disk could not keep up): 0",
        "By level: Error 3, Critical 1, Warning 7, Data 12,469",
        "",
        "Times the connection was lost: 0",
        "",
        "Restarts of the board: 2",
        "  2026-09-01 08:05:00.250  POWERON_RESET",
        "  2026-09-01 08:09:00.000  Zephyr boot banner",
    ]


def test_each_loss_of_the_connection_is_listed_with_its_length() -> None:
    lost = _START + timedelta(minutes=20)
    text = session_report(
        _figures(
            disconnect_count=2, seconds_away=5.5,
            disconnects=(
                Disconnect(lost, lost + timedelta(seconds=5.5)),
                Disconnect(lost + timedelta(minutes=30), None),
            ),
        ),
        _START + timedelta(hours=3), title="Pump", framing="8N1, no flow control",
        restarts_counted=True,
    )

    assert "Times the connection was lost: 2\nTime the board was away: 00:00:05\n" in text
    assert "  2026-09-01 08:20:00.000  back at 08:20:05.500, after 5.500 s\n" in text
    assert "  2026-09-01 08:50:00.000  not back\n" in text


def test_a_running_session_is_reported_as_of_now() -> None:
    text = session_report(
        _figures(ended=None, ended_unexpectedly=None), _START + timedelta(minutes=30),
        title="Pump", framing="8N1, no flow control", restarts_counted=True,
    )

    assert "Still connected at: 2026-09-01 08:30:00" in text
    assert "Running time: 00:30:00" in text


def test_restarts_not_counted_are_not_reported_as_none() -> None:
    text = session_report(
        _figures(restart_count=0, restarts=()), _START,
        title="Pump", framing="8N1, no flow control", restarts_counted=False,
    )

    assert "Restarts: not counted (Restart detection is switched off)" in text
    assert "Restarts of the board" not in text


@pytest.fixture
def port() -> LoopbackPort:
    return LoopbackPort()


@pytest.fixture
def tab(
    qapp: QApplication, config: ConfigManager, theme_manager: ThemeManager, port: LoopbackPort,
) -> DeviceTab:
    theme_manager.apply_theme(ThemeID.LIGHT)
    device_tab = DeviceTab(theme_manager, config, transport_factory=port.factory)
    device_tab.show()
    device_tab.findChild(ConnectPage).connect_requested.emit("PORT0", 115200)
    wait_until(qapp, lambda: device_tab.is_connected)
    port.feed(b"rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\nE (1) app: fault\n")
    wait_until(qapp, lambda: device_tab.session.total_lines == 2)
    yield device_tab
    device_tab.ordered_shutdown()


def test_the_tab_opens_the_report_of_its_session(qapp, tab, monkeypatch) -> None:
    shown: list[str] = []
    noted: list[object] = []
    tab.usage_noted.connect(noted.append)
    monkeypatch.setattr(SessionReportDialog, "exec", lambda dialog: shown.append(dialog.text()))

    tab.open_session_report()

    assert Feature.REPORT in noted

    assert "Port: PORT0 at 115,200 baud, 8N1, no flow control" in shown[0]
    assert "Restarts of the board: 1" in shown[0]
    assert "Errors: 1" in shown[0]


def test_the_report_is_saved_as_a_text_file(qapp, tab, monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "report.txt"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(path), ""))

    def save_and_close(dialog: SessionReportDialog) -> None:
        tab.report._save(dialog, dialog.text())
        assert Toast.of(dialog).message == "Saved the report: report.txt"

    monkeypatch.setattr(SessionReportDialog, "exec", save_and_close)

    tab.open_session_report()

    assert path.read_text().startswith("WireTrace session report: PORT0")


def test_switching_restart_detection_off_stops_counting(qapp, tab, port, config) -> None:
    ModuleSettings(config).set_enabled(RESTARTS, False)
    tab.apply_preferences()

    port.feed(b"rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\n")
    wait_until(qapp, lambda: tab.session.total_lines == 3)

    assert tab.report.context.session_figures().restart_count == 1
    assert "not counted" in tab.report.report_text()
