# SPDX-License-Identifier: GPL-3.0-or-later
"""Every screen of the application, built for the tests that check that
all of them follow one set of rules."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QWidget

from app.config import ConfigManager
from app.constants import ExportFormat, ThemeID
from app.plot_config import PlotConfig, PlotProfileStore
from tests.update_helpers import release
from tests_gui.support import LoopbackPort, wait_until
from ui.dialogs import messages
from ui.dialogs.about_dialog import AboutDialog
from ui.dialogs.export_dialog import ExportDialog
from ui.dialogs.licenses_dialog import LicensesDialog
from ui.dialogs.new_log_dialog import NewLogDialog
from ui.dialogs.plot_config_dialog import PlotConfigDialog, _CaptureAssistantDialog
from ui.dialogs.preferences_dialog import PAGES, PreferencesDialog
from ui.dialogs.saved_commands_dialog import SavedCommandsDialog
from ui.dialogs.shortcuts_dialog import ShortcutsDialog
from ui.main_window import MainWindow
from ui.themes.theme_manager import ThemeManager
from ui.widgets.connect_page import ConnectPage
from ui.widgets.toast import Toast
from updater.update_dialog import UpdateDialog
from updater.update_manager import UpdateManager

#: Wide enough that no row of the main window hides anything.
WINDOW_SIZE = (1680, 900)

_PORT = "PORT0"
_BAUD = 115200
_SPLIT_BYTES = 100 * 1024 * 1024
_LINES = b"RPM: 1465, Current: 2.4\n[ERROR] failed\n" * 5
_SAMPLE = "RPM: 1465, Current: 2.4"


def _dialogs(
    config: ConfigManager, theme_manager: ThemeManager,
) -> list[tuple[str, Callable[[], QDialog]]]:
    def preferences(page: str) -> QDialog:
        dialog = PreferencesDialog(config, theme_manager, open_plot_config=lambda: None)
        dialog.show_page(page)
        return dialog

    def plot_config(manual: bool) -> QDialog:
        dialog = PlotConfigDialog(
            store=PlotProfileStore(config),
            recent_lines_provider=lambda: [_SAMPLE],
            current_config=PlotConfig.auto(),
        )
        dialog._mode_manual_rb.setChecked(manual)
        return dialog

    def update() -> QDialog:
        return UpdateDialog(release("99.0.0"), UpdateManager(config))

    return [
        ("About", lambda: AboutDialog(theme_manager)),
        ("Export", lambda: ExportDialog("", _PORT, ExportFormat.BOTH)),
        ("Start a log", lambda: NewLogDialog(_PORT, "", ExportFormat.BOTH, _SPLIT_BYTES)),
        ("Shortcuts", ShortcutsDialog),
        ("Saved commands", lambda: SavedCommandsDialog(["AT", "AT+GMR"])),
        ("Configure plot, automatic", lambda: plot_config(manual=False)),
        ("Configure plot, manual", lambda: plot_config(manual=True)),
        ("Capture from sample", lambda: _CaptureAssistantDialog(_SAMPLE, None)),
        ("Update", update),
        ("Text prompt", lambda: messages.TextPrompt("Rename profile", "New name", "Bench")),
        ("Third-party licences", LicensesDialog),
        *((f"Preferences: {page}", lambda page=page: preferences(page)) for page in PAGES),
    ]


def _message_boxes(qapp: QApplication) -> Iterator[tuple[str, QWidget]]:
    """The boxes of ``ui.dialogs.messages``, each shown without blocking."""
    shown: list[QMessageBox] = []

    def show(box: QMessageBox) -> int:
        box.show()
        qapp.processEvents()
        shown.append(box)
        return QDialog.DialogCode.Rejected

    original = QMessageBox.exec
    QMessageBox.exec = show
    try:
        messages.confirm(None, "Delete profile", "Delete it?", detail="More.", proceed="Delete")
        messages.tell(None, "Cannot save profile", "Something to read")
    finally:
        QMessageBox.exec = original
    for name, box in zip(("Confirm", "Tell"), shown, strict=True):
        yield name, box
        box.close()
        box.deleteLater()


@contextmanager
def main_window(
    qapp: QApplication, config: ConfigManager, theme_manager: ThemeManager,
) -> Iterator[tuple[MainWindow, LoopbackPort]]:
    theme_manager.apply_theme(ThemeID.LIGHT)
    port = LoopbackPort()
    window = MainWindow(config, theme_manager, transport_factory=port.factory)
    window.resize(*WINDOW_SIZE)
    window.show()
    qapp.processEvents()
    try:
        yield window, port
    finally:
        window.shutdown_all_tabs()


def start_session(qapp: QApplication, window: MainWindow, port: LoopbackPort) -> None:
    """Connect, receive lines, and open Find, the plot and a toast."""
    tab = window.current_tab()
    tab.findChild(ConnectPage).connect_requested.emit(_PORT, _BAUD)
    wait_until(qapp, lambda: tab.is_connected)
    port.feed(_LINES)
    tab.activate_search()
    tab.toggle_plot()
    Toast.success(tab, "Exported", "Show", lambda: None)
    qapp.processEvents()


def lose_connection(qapp: QApplication, window: MainWindow, port: LoopbackPort) -> None:
    tab = window.current_tab()
    port.transport.drop()
    wait_until(qapp, lambda: not tab.is_connected)
    qapp.processEvents()


def every_screen(
    qapp: QApplication, config: ConfigManager, theme_manager: ThemeManager,
) -> Iterator[tuple[str, QWidget]]:
    """Yield each screen, shown, with its name; it is closed afterwards."""
    with main_window(qapp, config, theme_manager) as (window, port):
        yield "First screen", window
        start_session(qapp, window, port)
        yield "Session", window
        lose_connection(qapp, window, port)
        yield "Connection lost", window
    for name, build in _dialogs(config, theme_manager):
        dialog = build()
        dialog.show()
        qapp.processEvents()
        try:
            yield name, dialog
        finally:
            dialog.close()
            dialog.deleteLater()
    yield from _message_boxes(qapp)
