# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests of the diagnostic log with real Qt: it is kept for every run, and
the Help menu opens its folder and copies its summary."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from PySide6.QtGui import QAction, QGuiApplication

import app.application as application_module
import app.constants as constants_module
import app.diagnostic_log as diagnostic_log
import main as main_module
import ui.main_window as main_window_module
from app.constants import DIAGNOSTIC_LOG_SUFFIX, ThemeID
from app.diagnostic_log import DiagnosticLog
from tests_gui.support import LoopbackPort, wait_until
from ui.main_window import MainWindow
from ui.themes.theme_manager import ThemeManager
from ui.widgets.connect_page import ConnectPage
from ui.widgets.toast import Toast
from updater.install import installation_lines
from version import APP_NAME, APP_VERSION


@pytest.fixture
def light(qapp) -> ThemeManager:
    themes = ThemeManager(qapp)
    themes.apply_theme(ThemeID.LIGHT)
    return themes


@pytest.fixture
def diagnostics(tmp_path: Path):
    log = DiagnosticLog(tmp_path / "logs")
    root = logging.getLogger()
    level = root.level
    root.setLevel(logging.INFO)
    log.start(installation_lines())
    yield log
    log.stop()
    root.setLevel(level)


@pytest.fixture
def window(qapp, config, light, diagnostics):
    port = LoopbackPort()
    main_window = MainWindow(config, light, transport_factory=port.factory, diagnostics=diagnostics)
    main_window.show()
    qapp.processEvents()
    yield main_window
    main_window.shutdown_all_tabs()


def _action(window: MainWindow, text: str) -> QAction:
    return next(action for action in window.findChildren(QAction) if action.text() == text)


def test_every_run_keeps_a_diagnostic_log_from_its_first_line_to_its_last(tmp_path, monkeypatch):
    folder = tmp_path / "logs"
    monkeypatch.setattr(constants_module, "get_diagnostic_log_dir", lambda: str(folder))

    class Run:
        def __init__(self, argv, diagnostics) -> None:
            self.diagnostics = diagnostics

        def run(self) -> int:
            logging.getLogger("app.application").warning("an event of the run")
            return 0

    monkeypatch.setattr(application_module, "WireTraceApp", Run)
    monkeypatch.setattr(main_module, "setup_logging", lambda: logging.getLogger().setLevel(logging.INFO))

    assert main_module.main() == 0

    (written,) = list(folder.glob(f"*{DIAGNOSTIC_LOG_SUFFIX}"))
    text = written.read_text(encoding="utf-8")
    assert written.name.endswith(f"-{os.getpid()}.log")
    assert f"{APP_NAME} {APP_VERSION}, installed as source" in text
    assert "an event of the run" in text
    assert text.rstrip().endswith(f"{APP_NAME} is closing")
    assert not any(isinstance(h, diagnostic_log._Queueing) for h in logging.getLogger().handlers)


class TestTheHelpMenu:
    def test_it_offers_the_folder_and_the_summary(self, window):
        for text in ("Open diagnostic logs", "Copy diagnostic summary"):
            assert _action(window, text).isEnabled()

    def test_the_folder_is_opened_in_the_file_manager(self, qapp, window, diagnostics, monkeypatch):
        opened: list[str] = []
        monkeypatch.setattr(
            main_window_module.QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()) or True,
        )

        _action(window, "Open diagnostic logs").trigger()

        assert [Path(path) for path in opened] == [diagnostics.directory]

    def test_a_folder_not_yet_made_is_said(self, qapp, config, light, tmp_path, monkeypatch):
        monkeypatch.setattr(main_window_module, "get_diagnostic_log_dir", lambda: str(tmp_path / "none"))
        main_window = MainWindow(config, light, transport_factory=LoopbackPort().factory)
        main_window.show()

        _action(main_window, "Open diagnostic logs").trigger()

        assert Toast.of(main_window.current_tab()).message == "No diagnostic log has been written yet"
        main_window.shutdown_all_tabs()

    def test_the_summary_names_the_run_its_open_ports_and_its_latest_events(
        self, qapp, window, diagnostics,
    ):
        tab = window.current_tab()
        tab.findChild(ConnectPage).connect_requested.emit("PORT0", 115200)
        wait_until(qapp, lambda: tab.is_connected)
        logging.getLogger("ui.device_tab").warning("Waiting for the board on PORT0 to return")
        wait_until(qapp, lambda: any("Waiting for the board" in line for line in diagnostics.recent()[1]))

        _action(window, "Copy diagnostic summary").trigger()

        text = QGuiApplication.clipboard().text()
        for line in installation_lines():
            assert line in text
        assert f"Diagnostic log: {diagnostics.directory}" in text
        assert "Ports open:\n  PORT0, 115200 baud, connected\n" in text
        problems = text.split("Latest warnings and errors in this run:\n", 1)[1].split("\n\n", 1)[0]
        assert "Waiting for the board on PORT0 to return" in problems
        assert Toast.of(tab).message == "Diagnostic summary copied, ready to paste into a report"
