# SPDX-License-Identifier: GPL-3.0-or-later
"""End-to-end tests of a device tab: real widgets, real threads, real
files, and a loopback port in place of the hardware."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QLineEdit,
    QMessageBox,
    QPushButton,
)

import ui.device_tab as device_tab_module
from app.config import ConfigManager
from app.constants import (
    NAME_SEPARATOR,
    DisplayMode,
    ExportFormat,
    LineEnding,
    ThemeID,
    TimestampMode,
)
from app.port_memory import PortMemory, PortSettings
from core.transport import PortInfo
from tests_gui.support import LoopbackPort, line_text, log_body, wait_until
from ui.device_tab import NEW_TAB_TITLE, DeviceTab
from ui.dialogs.export_dialog import ExportDialog
from ui.dialogs.new_log_dialog import NewLogDialog
from ui.themes.theme_manager import ThemeManager
from ui.widgets.banner import BannerArea
from ui.widgets.command_bar import CommandBar
from ui.widgets.connect_page import ConnectPage
from ui.widgets.console_header import LEVEL_CHOICES, ConsoleHeader
from ui.widgets.console_view import ConsoleView
from ui.widgets.search_bar import SearchBar
from ui.widgets.status_bar import DeviceStatusBar
from ui.widgets.status_dot import LinkState
from ui.widgets.tab_toolbar import TabToolbar
from ui.widgets.toast import Toast

_PORT = "PORT0"
_BAUD = 115200
_BURST_LINES = 5_000
_DESCRIPTION = "Bench adapter"
_NOTICE = "[WireTrace] "


def _port_info(device: str = _PORT, description: str = _DESCRIPTION) -> PortInfo:
    return PortInfo(device, description, "Maker")


@pytest.fixture
def port() -> LoopbackPort:
    return LoopbackPort()


@pytest.fixture
def tab(
    qapp: QApplication,
    config: ConfigManager,
    theme_manager: ThemeManager,
    port: LoopbackPort,
) -> DeviceTab:
    theme_manager.apply_theme(ThemeID.LIGHT)
    device_tab = DeviceTab(theme_manager, config, transport_factory=port.factory)
    device_tab.show()
    qapp.processEvents()
    yield device_tab
    device_tab.ordered_shutdown()
    device_tab.close()
    device_tab.deleteLater()
    qapp.processEvents()


@pytest.fixture
def accept_dialogs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Answer the New Log and Export dialogs as a user accepting them.

    Returns the directory the files are written to.
    """
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    for dialog_class in (NewLogDialog, ExportDialog):
        monkeypatch.setattr(
            dialog_class, "exec", lambda self: QDialog.DialogCode.Accepted,
        )
    monkeypatch.setattr(
        NewLogDialog, "log_directory", property(lambda self: str(output_dir)),
    )
    monkeypatch.setattr(
        ExportDialog, "export_directory", property(lambda self: str(output_dir)),
    )
    return output_dir


@pytest.fixture
def opened_folders(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record the folders the tab asks the system to open.

    As paths: a URL's local file is written with forward slashes on every
    system, so its text would differ from a Windows path's.
    """
    opened: list[Path] = []

    def open_url(url: QUrl) -> bool:
        opened.append(Path(url.toLocalFile()))
        return True

    monkeypatch.setattr(device_tab_module.QDesktopServices, "openUrl", open_url)
    return opened


def _console(tab: DeviceTab) -> ConsoleView:
    return tab.findChild(ConsoleView)


def _toolbar(tab: DeviceTab) -> TabToolbar:
    return tab.findChild(TabToolbar)


def _banners(tab: DeviceTab) -> BannerArea:
    return tab.findChild(BannerArea)


def _status(tab: DeviceTab) -> list[str]:
    return tab.findChild(DeviceStatusBar).texts()


def _request_connection(tab: DeviceTab) -> None:
    """Ask for a connection the way the screen in front does."""
    source = _toolbar(tab) if tab.has_session else tab.findChild(ConnectPage)
    source.connect_requested.emit(_PORT, _BAUD)


def _connect(qapp: QApplication, tab: DeviceTab) -> None:
    _request_connection(tab)
    wait_until(qapp, lambda: tab.is_connected)


def _disconnect(qapp: QApplication, tab: DeviceTab) -> None:
    _toolbar(tab).disconnect_requested.emit()
    wait_until(qapp, lambda: not tab.is_connected)


def _start_log(tab: DeviceTab) -> None:
    assert not tab.is_logging
    _toolbar(tab).log_toggled.emit()


def _stop_log(tab: DeviceTab) -> None:
    assert tab.is_logging
    _toolbar(tab).log_toggled.emit()


def _wait_for_lines(qapp: QApplication, tab: DeviceTab, count: int) -> None:
    wait_until(qapp, lambda: _console(tab).total_count >= count)


def _wait_for_last_line(qapp: QApplication, tab: DeviceTab, text: str) -> None:
    wait_until(qapp, lambda: _console_texts(tab)[-1:] == [text])


def _console_texts(tab: DeviceTab) -> list[str]:
    return [record.text for record in _console(tab).shown_records()]


def _only_file(directory: Path, pattern: str) -> Path:
    files = sorted(directory.glob(pattern))
    assert len(files) == 1, files
    return files[0]


class TestFirstScreen:
    def test_new_tab_asks_for_a_port(self, tab):
        page = tab.findChild(ConnectPage)

        assert page.isVisible()
        assert not _console(tab).isVisible()
        assert tab.has_session is False
        assert tab.title == NEW_TAB_TITLE
        assert tab.link_state is LinkState.IDLE
        assert page.texts()[0] == "Connect a device"

    def test_ports_are_listed_with_what_they_were_last_used_with(self, tab, ports, config):
        PortMemory(config).remember(_PORT, baud_rate=9600, line_ending="CRLF")
        ports.extend([_port_info(), _port_info("PORT1", "Other adapter")])
        tab.refresh_ports()
        page = tab.findChild(ConnectPage)

        assert page.port_devices() == [_PORT, "PORT1"]
        assert page.selected_port == _PORT
        assert page.selected_baud == 9600
        assert "Last used at 9600, CR LF" in [
            label.text() for label in page.row(_PORT).findChildren(type(page.row(_PORT).badge))
        ]

    def test_choosing_another_port_offers_its_baud_rate(self, tab, ports, config):
        PortMemory(config).remember("PORT1", baud_rate=57600)
        ports.extend([_port_info(), _port_info("PORT1")])
        tab.refresh_ports()
        page = tab.findChild(ConnectPage)

        page.row("PORT1").chosen.emit("PORT1")

        assert page.selected_port == "PORT1"
        assert page.selected_baud == 57600

    def test_connect_device_uses_the_port_chosen_on_the_page(self, qapp, tab, ports, port):
        ports.append(_port_info())
        tab.refresh_ports()

        tab.connect_device()
        wait_until(qapp, lambda: tab.is_connected)

        assert tab.port_name == _PORT

    def test_connect_device_without_a_port_says_what_is_missing(self, tab, port):
        tab.connect_device()

        assert port.transport is None
        assert Toast.of(tab).message == "Choose a serial port and a valid baud rate first"


class TestConnection:
    def test_connecting_reveals_the_session_and_shows_lines(self, qapp, tab, port, ports):
        ports.append(_port_info())
        titles: list[str] = []
        states: list[LinkState] = []
        tab.title_changed.connect(titles.append)
        tab.link_state_changed.connect(states.append)

        _connect(qapp, tab)
        port.feed(b"boot ok\nERROR: sensor missing\n")
        _wait_for_lines(qapp, tab, 2)

        assert tab.port_name == _PORT
        assert tab.has_session is True
        assert titles == [f"{_PORT}{NAME_SEPARATOR}{_DESCRIPTION}"]
        assert states == [LinkState.CONNECTED]
        assert _console(tab).isVisible()
        assert not tab.findChild(ConnectPage).isVisible()
        assert _console_texts(tab) == ["boot ok", "ERROR: sensor missing"]
        assert _status(tab)[:2] == [_PORT, f"{_BAUD} 8N1"]
        assert _toolbar(tab).button_texts()["connect"] == "Disconnect"

    def test_port_without_a_description_is_named_by_its_device(self, qapp, tab, port):
        _connect(qapp, tab)

        assert tab.title == _PORT

    def test_refused_port_is_reported_and_the_first_screen_stays(self, qapp, tab, port):
        port.refuse_with = "Access denied"
        _request_connection(tab)
        wait_until(qapp, lambda: not tab._connecting)

        banner = _banners(tab).banner("connect")
        assert tab.is_connected is False
        assert tab.has_session is False
        assert banner.title_label.text() == f"Could not open {_PORT}"
        assert banner.detail_label.text() == "Access denied"
        assert _status(tab)[0] == "No device connected"

    def test_a_successful_connection_removes_the_report_of_a_failed_one(self, qapp, tab, port):
        port.refuse_with = "Access denied"
        _request_connection(tab)
        wait_until(qapp, lambda: not tab._connecting)
        port.refuse_with = ""

        _connect(qapp, tab)

        assert _banners(tab).keys() == []

    def test_connect_request_after_shutdown_is_ignored(self, qapp, tab, port):
        tab.ordered_shutdown()
        _request_connection(tab)
        qapp.processEvents()

        assert port.transport is None
        assert tab._connecting is False

    def test_disconnect_keeps_the_console_and_says_so(self, qapp, tab, port):
        states: list[LinkState] = []
        _connect(qapp, tab)
        port.feed(b"one\ntwo\n")
        _wait_for_lines(qapp, tab, 2)
        tab.link_state_changed.connect(states.append)

        _disconnect(qapp, tab)

        assert tab.port_name == ""
        assert tab.has_session is True
        assert tab.title == _PORT
        assert states == [LinkState.IDLE]
        assert _console(tab).isVisible()
        assert _console_texts(tab) == ["one", "two"]
        assert _status(tab)[0] == "Disconnected"
        assert _status(tab)[1].startswith(f"{_PORT}, closed at ")
        assert _toolbar(tab).button_texts()["connect"] == "Connect"
        assert Toast.of(tab).message == f"Disconnected from {_PORT}"
        assert _banners(tab).keys() == []

    def test_lines_still_on_their_way_are_shown_after_the_disconnect(self, qapp, tab, port):
        _connect(qapp, tab)
        tab._display_timer.stop()
        port.feed(b"".join(f"line {n}\n".encode() for n in range(200)))
        wait_until(qapp, lambda: tab.session.total_lines == 200)

        _disconnect(qapp, tab)

        assert _console_texts(tab) == [f"line {n}" for n in range(200)]

    def test_unplugged_device_is_reported_in_the_tab(self, qapp, tab, port, monkeypatch):
        monkeypatch.setattr(
            QMessageBox, "exec", lambda box: pytest.fail("a lost connection must not open a dialog"),
        )
        _connect(qapp, tab)
        port.feed(b"last words\n")
        _wait_for_lines(qapp, tab, 1)

        port.transport.drop()
        wait_until(qapp, lambda: not tab.is_connected)

        banner = _banners(tab).banner("lost")
        assert banner.title_label.text().startswith(f"The connection to {_PORT} was lost at ")
        assert [button.text() for button in banner.action_buttons] == [
            "Export session", "Connect again",
        ]
        assert _console_texts(tab) == ["last words", f"{_NOTICE}Connection lost."]
        assert _status(tab)[1].startswith(f"{_PORT}, lost at ")

    def test_connect_again_on_the_banner_reconnects(self, qapp, tab, port):
        _connect(qapp, tab)
        port.transport.drop()
        wait_until(qapp, lambda: not tab.is_connected)

        _banners(tab).banner("lost").action_buttons[-1].click()
        wait_until(qapp, lambda: tab.is_connected)

        assert _banners(tab).keys() == []
        assert tab.port_name == _PORT

    def test_reconnecting_starts_with_an_empty_console(self, qapp, tab, port):
        _connect(qapp, tab)
        port.feed(b"first session\n")
        _wait_for_lines(qapp, tab, 1)
        _disconnect(qapp, tab)

        _connect(qapp, tab)
        port.feed(b"second session\n")
        _wait_for_lines(qapp, tab, 1)

        assert _console_texts(tab) == ["second session"]

    def test_serial_error_that_keeps_the_connection_is_a_banner(self, qapp, tab, port):
        _connect(qapp, tab)

        tab._on_serial_error("Framing error")

        banner = _banners(tab).banner("serial")
        assert banner.title_label.text() == f"A communication error occurred on {_PORT}"
        assert banner.detail_label.text() == "Framing error. The connection is still open."
        banner.close_button.click()
        assert _banners(tab).keys() == []


class TestLogging:
    def test_log_holds_every_line_up_to_the_disconnect(
        self, qapp, tab, port, accept_dialogs,
    ):
        _connect(qapp, tab)
        _start_log(tab)
        assert tab.is_logging

        expected = [f"line {number}" for number in range(_BURST_LINES)]
        port.feed("".join(f"{text}\n" for text in expected).encode())
        port.feed(b"unterminated tail")
        _disconnect(qapp, tab)

        log = _only_file(accept_dialogs, "*.txt")
        assert [line_text(line) for line in log_body(str(log))] == [
            *expected, "unterminated tail",
        ]
        assert tab.is_logging is False

    def test_closing_the_tab_while_logging_keeps_the_whole_log(
        self, qapp, tab, port, accept_dialogs,
    ):
        _connect(qapp, tab)
        _start_log(tab)
        expected = [f"line {number}" for number in range(_BURST_LINES)]
        port.feed("".join(f"{text}\n" for text in expected).encode())

        tab.ordered_shutdown()

        log = _only_file(accept_dialogs, "*.txt")
        assert [line_text(line) for line in log_body(str(log))] == expected

    def test_device_lost_while_the_new_log_dialog_is_open_starts_no_log(
        self, qapp, tab, port, accept_dialogs, monkeypatch,
    ):
        _connect(qapp, tab)

        def unplug_then_accept(dialog):
            port.transport.drop()
            wait_until(qapp, lambda: not tab.is_connected)
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(NewLogDialog, "exec", unplug_then_accept)
        _start_log(tab)

        assert tab.is_logging is False
        assert list(accept_dialogs.iterdir()) == []
        assert Toast.of(tab).message == (
            "Logging was not started: the device is no longer connected"
        )

        _connect(qapp, tab)
        assert tab.is_logging is False
        monkeypatch.setattr(NewLogDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
        _start_log(tab)
        port.feed(b"logged after reconnect\n")
        _wait_for_lines(qapp, tab, 1)
        _stop_log(tab)
        log = _only_file(accept_dialogs, "*.txt")
        assert [line_text(line) for line in log_body(str(log))] == [
            "logged after reconnect",
        ]

    def test_log_off_closes_the_file_and_the_session_goes_on(
        self, qapp, tab, port, accept_dialogs,
    ):
        _connect(qapp, tab)
        _start_log(tab)
        port.feed(b"logged\n")
        _wait_for_lines(qapp, tab, 1)
        _stop_log(tab)
        port.feed(b"not logged\n")
        _wait_for_lines(qapp, tab, 2)

        log = _only_file(accept_dialogs, "*.txt")
        assert [line_text(line) for line in log_body(str(log))] == ["logged"]
        assert tab.is_connected and not tab.is_logging

    def test_paused_lines_are_shown_but_not_logged_and_the_gap_is_marked(
        self, qapp, tab, port, accept_dialogs,
    ):
        _connect(qapp, tab)
        _start_log(tab)
        port.feed(b"before\n")
        _wait_for_lines(qapp, tab, 1)
        _toolbar(tab).pause_toggled.emit()
        assert _status(tab)[4].startswith("Paused")
        assert _toolbar(tab).button_texts()["pause"] == "Resume"
        port.feed(b"during\n")
        _wait_for_lines(qapp, tab, 2)
        _toolbar(tab).pause_toggled.emit()
        port.feed(b"after\n")
        _wait_for_lines(qapp, tab, 3)
        _stop_log(tab)

        log = _only_file(accept_dialogs, "*.txt")
        assert [line_text(line) for line in log_body(str(log))] == [
            "before",
            "[WireTrace] Logging paused",
            "[WireTrace] Logging resumed",
            "after",
        ]
        assert _console_texts(tab) == ["before", "during", "after"]

    def test_log_is_split_at_the_preferred_size(
        self, qapp, tab, port, config, accept_dialogs, monkeypatch,
    ):
        part_bytes = 64 * 1024
        monkeypatch.setattr(
            type(config), "log_split_bytes", property(lambda self: part_bytes),
        )
        _connect(qapp, tab)
        _start_log(tab)
        expected = [f"line {number:06d} " + "x" * 80 for number in range(_BURST_LINES)]
        port.feed("".join(f"{text}\n" for text in expected).encode())
        _disconnect(qapp, tab)

        parts = sorted(accept_dialogs.glob("*.txt"))
        assert len(parts) > 1
        assert all(part.stat().st_size <= part_bytes for part in parts)
        written = [
            line_text(line) for part in parts for line in log_body(str(part))
        ]
        assert written == expected


    def test_running_log_is_shown_in_the_status_bar_and_on_the_tab(
        self, qapp, tab, port, accept_dialogs, opened_folders,
    ):
        states: list[LinkState] = []
        _connect(qapp, tab)
        tab.link_state_changed.connect(states.append)
        assert _status(tab)[4] == "Not logging"

        _start_log(tab)
        log = _only_file(accept_dialogs, "*.txt")
        port.feed(b"one\n")
        _wait_for_lines(qapp, tab, 1)
        wait_until(qapp, lambda: "file 1" in _status(tab)[4])

        parts = _status(tab)[4].split("  \u00b7  ")
        assert parts[:2] == ["Logging", log.name]
        assert parts[3].startswith("file 1, ")
        assert tab.link_state is LinkState.LOGGING
        assert _toolbar(tab).button_texts()["log"] == "Stop log"

        toast = Toast.of(tab)
        assert toast.message == f"Logging to {log.name}"
        assert toast.action_label == "Show"
        toast.trigger_action()
        assert opened_folders == [Path(accept_dialogs)]

        _stop_log(tab)
        assert _status(tab)[4] == "Not logging"
        assert states == [LinkState.LOGGING, LinkState.CONNECTED]
        assert Toast.of(tab).message == f"The log was closed with 1 line: {log.name}."

    def test_log_that_cannot_start_is_reported_in_a_banner(
        self, qapp, tab, port, accept_dialogs, monkeypatch,
    ):
        blocker = accept_dialogs / "not-a-folder"
        blocker.write_text("")
        monkeypatch.setattr(
            NewLogDialog, "log_directory", property(lambda self: str(blocker)),
        )
        _connect(qapp, tab)

        _start_log(tab)

        assert tab.is_logging is False
        assert _banners(tab).banner("log-end").title_label.text() == "Logging could not be started"

    def test_log_that_cannot_be_written_shows_a_banner_until_it_recovers(
        self, qapp, tab, port, accept_dialogs,
    ):
        _connect(qapp, tab)
        _start_log(tab)

        tab._on_log_write_failed("No space left on device")

        banner = _banners(tab).banner("log-write")
        assert banner.title_label.text() == "The log cannot be written: No space left on device"
        assert banner.close_button is None
        assert [button.text() for button in banner.action_buttons] == [
            "Open log folder", "Stop log",
        ]

        tab._on_log_write_recovered()

        assert _banners(tab).keys() == []
        assert Toast.of(tab).message == "The log is being written again"
        assert _console_texts(tab) == [
            f"{_NOTICE}The log cannot be written: No space left on device. Still trying.",
            f"{_NOTICE}The log is being written again.",
        ]

    def test_open_log_folder_opens_the_folder_of_the_running_log(
        self, qapp, tab, port, accept_dialogs, opened_folders, tmp_path,
    ):
        tab.open_log_folder()
        assert opened_folders == []
        assert Toast.of(tab).message.startswith("The folder does not exist yet: ")

        _connect(qapp, tab)
        _start_log(tab)
        tab.open_log_folder()

        assert opened_folders == [Path(accept_dialogs)]


    def test_the_folder_a_log_was_started_in_is_offered_for_the_next_one(
        self, qapp, tab, port, config, monkeypatch, tmp_path,
    ):
        usual, chosen = tmp_path / "usual", tmp_path / "chosen"
        usual.mkdir()
        chosen.mkdir()
        config.default_log_directory = str(usual)
        offered: list[str] = []

        def accept(dialog: NewLogDialog) -> QDialog.DialogCode:
            offered.append(dialog.log_directory)
            dialog._target.directory_input.setText(str(chosen))
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(NewLogDialog, "exec", accept)
        _connect(qapp, tab)

        _start_log(tab)
        _stop_log(tab)
        _start_log(tab)
        _stop_log(tab)

        assert offered == [str(usual), str(chosen)]
        assert ConfigManager().default_log_directory == str(chosen)
        assert len(list(chosen.glob("*.txt"))) == 2
        assert list(usual.iterdir()) == []

    def test_a_log_that_could_not_be_started_does_not_change_the_folder(
        self, qapp, tab, port, config, monkeypatch, tmp_path,
    ):
        usual = tmp_path / "usual"
        usual.mkdir()
        blocker = tmp_path / "a file, not a folder"
        blocker.write_text("x", encoding="utf-8")
        config.default_log_directory = str(usual)
        monkeypatch.setattr(NewLogDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
        monkeypatch.setattr(NewLogDialog, "log_directory", property(lambda self: str(blocker)))
        _connect(qapp, tab)

        _start_log(tab)

        assert not tab.is_logging
        assert config.default_log_directory == str(usual)

    def test_log_is_written_as_the_preferences_ask(
        self, qapp, tab, port, config, monkeypatch, tmp_path,
    ):
        seen: dict[str, object] = {}
        folder = tmp_path / "preferred"
        folder.mkdir()
        config.default_log_directory = str(folder)
        config.default_log_format = ExportFormat.BOTH
        config.log_line_ending = LineEnding.CRLF

        def accept(dialog: NewLogDialog) -> QDialog.DialogCode:
            seen["format"] = dialog.export_format
            seen["folder"] = dialog.log_directory
            seen["remark"] = dialog.remark()
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(NewLogDialog, "exec", accept)
        _connect(qapp, tab)

        _start_log(tab)
        port.feed(b"temp: 21.5, rpm: -40\n")
        _wait_for_lines(qapp, tab, 1)
        _stop_log(tab)

        assert seen["format"] is ExportFormat.BOTH
        assert seen["folder"] == str(folder)
        assert f"every {config.log_split_size_mb} MB" in seen["remark"]
        text = _only_file(folder, "*.txt").read_bytes()
        assert text.endswith(b"temp: 21.5, rpm: -40\r\n")
        assert text.count(b"\n") == text.count(b"\r\n")
        assert _only_file(folder, "*.csv").read_bytes().endswith(b"\r\n")


class TestExport:
    def test_export_covers_the_session_without_logging(
        self, qapp, tab, port, accept_dialogs,
    ):
        _connect(qapp, tab)
        port.feed(b"one\ntwo\n")
        _wait_for_lines(qapp, tab, 2)

        _toolbar(tab).export_requested.emit()
        wait_until(qapp, lambda: not tab._pipeline.is_exporting)

        exported = _only_file(accept_dialogs, "*.txt")
        assert [line_text(line) for line in log_body(str(exported))] == ["one", "two"]

    def test_export_holds_lines_the_console_no_longer_does(
        self, qapp, tab, port, config, accept_dialogs, monkeypatch,
    ):
        console_limit = 1_000
        monkeypatch.setattr(
            type(config), "max_console_lines", property(lambda self: console_limit),
        )
        tab.apply_preferences()
        _connect(qapp, tab)
        expected = [f"line {number}" for number in range(_BURST_LINES)]
        port.feed("".join(f"{text}\n" for text in expected).encode())
        wait_until(qapp, lambda: tab.session.total_lines == _BURST_LINES)
        wait_until(qapp, lambda: _console_texts(tab)[-1:] == expected[-1:])
        assert _console(tab).total_count == console_limit

        tab.export_session()
        wait_until(qapp, lambda: not tab._pipeline.is_exporting)

        exported = _only_file(accept_dialogs, "*.txt")
        assert [line_text(line) for line in log_body(str(exported))] == expected

    def test_clear_empties_the_console_and_moves_the_export_start(
        self, qapp, tab, port, accept_dialogs,
    ):
        _connect(qapp, tab)
        port.feed(b"before clear\n")
        _wait_for_lines(qapp, tab, 1)
        _toolbar(tab).clear_requested.emit()
        assert _console(tab).total_count == 0

        port.feed(b"after clear\n")
        _wait_for_lines(qapp, tab, 1)
        _toolbar(tab).export_requested.emit()
        wait_until(qapp, lambda: not tab._pipeline.is_exporting)

        exported = _only_file(accept_dialogs, "*.txt")
        assert [line_text(line) for line in log_body(str(exported))] == ["after clear"]

    def test_nothing_to_export_writes_no_file(self, qapp, tab, port, accept_dialogs):
        _connect(qapp, tab)
        _toolbar(tab).export_requested.emit()

        # The request is refused on the spot; no export thread exists
        # that could still create a file.
        assert not tab.is_exporting
        assert list(accept_dialogs.iterdir()) == []
        assert Toast.of(tab).message == "There is nothing to export: no data has been recorded"

    def test_session_is_still_exportable_after_a_failed_reconnect(
        self, qapp, tab, port, accept_dialogs,
    ):
        _connect(qapp, tab)
        port.feed(b"one\ntwo\n")
        _wait_for_lines(qapp, tab, 2)
        _disconnect(qapp, tab)
        port.refuse_with = "Access denied"
        _request_connection(tab)
        wait_until(qapp, lambda: not tab._connecting)
        assert _status(tab)[1] == f"{_PORT}, could not be opened"

        tab.export_session()
        wait_until(qapp, lambda: not tab.is_exporting)

        exported = _only_file(accept_dialogs, "*.txt")
        assert [line_text(line) for line in log_body(str(exported))] == ["one", "two"]

    def test_csv_export_has_the_detected_columns(
        self, qapp, tab, port, accept_dialogs, monkeypatch,
    ):
        monkeypatch.setattr(
            ExportDialog, "export_format", property(lambda self: ExportFormat.CSV),
        )
        _connect(qapp, tab)
        port.feed(b"temp: 21.5, rpm: -40\ntemp: 21.6, rpm: -41\n")
        _wait_for_lines(qapp, tab, 2)

        _toolbar(tab).export_requested.emit()
        wait_until(qapp, lambda: not tab._pipeline.is_exporting)

        exported = _only_file(accept_dialogs, "*.csv")
        rows = [
            row for row in exported.read_text(encoding="utf-8").split("\n")
            if row and not row.startswith("#")
        ]
        assert rows[0] == "Timestamp,temp,rpm"
        assert [row.split(",")[1:] for row in rows[1:]] == [
            ["21.5", "-40"], ["21.6", "-41"],
        ]


    def test_finished_export_offers_its_folder(
        self, qapp, tab, port, accept_dialogs, opened_folders,
    ):
        _connect(qapp, tab)
        port.feed(b"one\ntwo\n")
        _wait_for_lines(qapp, tab, 2)

        tab.export_session()
        wait_until(qapp, lambda: Toast.of(tab).message.startswith("Exported"))

        exported = _only_file(accept_dialogs, "*.txt")
        assert Toast.of(tab).message == f"Exported 2 lines: {exported.name}"
        Toast.of(tab).trigger_action()
        assert opened_folders == [Path(accept_dialogs)]


    def test_export_choices_are_remembered_and_offered_next_time(
        self, qapp, tab, port, config, monkeypatch, tmp_path,
    ):
        folder = tmp_path / "exports"
        offered: list[tuple[str, ExportFormat]] = []

        def accept(dialog: ExportDialog) -> QDialog.DialogCode:
            offered.append((dialog.export_directory, dialog.export_format))
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(ExportDialog, "exec", accept)
        _connect(qapp, tab)
        port.feed(b"temp: 1, rpm: 2\n")
        _wait_for_lines(qapp, tab, 1)

        tab.export_session()
        wait_until(qapp, lambda: not tab.is_exporting)
        assert offered == [(config.default_log_directory, ExportFormat.TXT)]

        monkeypatch.setattr(
            ExportDialog, "export_directory", property(lambda self: str(folder)),
        )
        monkeypatch.setattr(
            ExportDialog, "export_format", property(lambda self: ExportFormat.CSV),
        )
        tab.export_session()
        wait_until(qapp, lambda: not tab.is_exporting)

        assert config.export_directory == str(folder)
        assert config.export_format is ExportFormat.CSV
        assert [path.suffix for path in folder.iterdir()] == [".csv"]

    def test_cancelled_export_remembers_nothing(self, qapp, tab, port, config, monkeypatch):
        monkeypatch.setattr(ExportDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
        monkeypatch.setattr(
            ExportDialog, "export_format", property(lambda self: ExportFormat.BOTH),
        )
        _connect(qapp, tab)

        tab.export_session()

        assert config.export_format is ExportFormat.TXT

    def test_export_uses_the_line_ending_of_the_preferences(
        self, qapp, tab, port, config, accept_dialogs,
    ):
        config.log_line_ending = LineEnding.CRLF
        _connect(qapp, tab)
        port.feed(b"one\ntwo\n")
        _wait_for_lines(qapp, tab, 2)

        tab.export_session()
        wait_until(qapp, lambda: not tab.is_exporting)

        data = _only_file(accept_dialogs, "*.txt").read_bytes()
        assert data.endswith(b"two\r\n")
        assert data.count(b"\n") == data.count(b"\r\n")


class TestCommands:
    def test_command_reaches_the_device_and_the_console(self, qapp, tab, port):
        _connect(qapp, tab)
        tab.findChild(CommandBar).command_sent.emit("AT+RST")
        _wait_for_lines(qapp, tab, 1)

        assert port.transport.written == [b"AT+RST\n"]
        assert _console_texts(tab) == [">>> AT+RST"]

    def test_command_is_sent_with_the_chosen_line_ending(self, qapp, tab, port):
        endings: list[LineEnding] = []
        tab.line_ending_changed.connect(endings.append)
        _connect(qapp, tab)

        tab.set_line_ending(LineEnding.CRLF)
        tab.findChild(CommandBar).command_sent.emit("AT")
        _wait_for_lines(qapp, tab, 1)

        assert endings == [LineEnding.CRLF]
        assert tab.line_ending is LineEnding.CRLF
        assert port.transport.written == [b"AT\r\n"]

    def test_commands_can_be_sent_only_while_connected(self, qapp, tab, port):
        bar = tab.findChild(CommandBar)
        send = next(b for b in bar.findChildren(QPushButton) if b.text() == "Send")
        _connect(qapp, tab)
        assert send.isEnabled()

        _disconnect(qapp, tab)
        assert not send.isEnabled()

        bar.command_sent.emit("AT")
        assert Toast.of(tab).message == "The command was not sent: no device is connected"

    def test_command_that_fails_is_reported_in_a_banner(self, qapp, tab, port):
        _connect(qapp, tab)

        tab._on_command_failed("Write timeout")

        banner = _banners(tab).banner("command")
        assert banner.title_label.text() == "The command was not sent"
        assert banner.detail_label.text() == "Write timeout"

        tab.findChild(CommandBar).command_sent.emit("AT")
        assert _banners(tab).keys() == []


class TestPortMemory:
    def test_connection_settings_are_remembered_for_the_port(self, qapp, tab, port, config):
        _connect(qapp, tab)
        tab.set_line_ending(LineEnding.CR)
        tab.rename("Pump")

        assert PortMemory(config).get(_PORT) == PortSettings(
            baud_rate=_BAUD, line_ending="CR", tab_name="Pump",
        )

    def test_a_port_comes_back_with_what_it_was_last_used_with(
        self, qapp, tab, port, config, theme_manager,
    ):
        PortMemory(config).remember(_PORT, line_ending="CRLF", tab_name="Pump")

        _connect(qapp, tab)

        assert tab.line_ending is LineEnding.CRLF
        assert tab.custom_name == "Pump"
        assert tab.title == f"{_PORT}{NAME_SEPARATOR}Pump"

    def test_a_name_given_before_connecting_gives_way_to_the_port(self, qapp, tab, port):
        tab.rename("Scratch")
        assert tab.title == "Scratch"

        _connect(qapp, tab)

        assert tab.title == _PORT

    def test_an_empty_name_returns_to_the_description(self, qapp, tab, port, ports, config):
        ports.append(_port_info())
        _connect(qapp, tab)
        titles: list[str] = []
        tab.title_changed.connect(titles.append)

        tab.rename("  Pump  ")
        tab.rename("")

        assert titles == [f"{_PORT}{NAME_SEPARATOR}Pump", f"{_PORT}{NAME_SEPARATOR}{_DESCRIPTION}"]
        assert PortMemory(config).get(_PORT).tab_name == ""


class TestDisplay:
    def test_hex_mode_shows_bytes_as_rows(self, qapp, tab, port):
        _connect(qapp, tab)
        tab.set_display_mode(DisplayMode.HEX)
        port.feed(bytes(range(16)))
        _wait_for_lines(qapp, tab, 1)

        assert _console_texts(tab) == [
            "00000000  00 01 02 03 04 05 06 07  08 09 0A 0B 0C 0D 0E 0F  "
            "|................|",
        ]

    def test_blank_lines_can_be_hidden_and_stay_in_the_session(
        self, qapp, tab, port, config, accept_dialogs,
    ):
        _connect(qapp, tab)
        port.feed(b"one\n\ntwo\n")
        _wait_for_lines(qapp, tab, 3)
        assert _console_texts(tab) == ["one", "", "two"]

        config.hide_blank_lines = True
        tab.apply_preferences()
        assert _console_texts(tab) == ["one", "two"]

        _toolbar(tab).export_requested.emit()
        wait_until(qapp, lambda: not tab._pipeline.is_exporting)
        exported = _only_file(accept_dialogs, "*.txt")
        body = exported.read_text(encoding="utf-8")
        assert body.count("] \n") == 1

    def test_lines_the_console_skips_are_reported_where_they_were(
        self, qapp, tab, port,
    ):
        _connect(qapp, tab)
        port.feed(b"before\n")
        _wait_for_lines(qapp, tab, 1)

        # The window falls behind: lines arrive while nothing is drawn.
        tab._display_timer.stop()
        port.feed(b"".join(f"line {n}\n".encode() for n in range(500)))
        wait_until(qapp, lambda: tab.session.total_lines == 501)
        tab._pipeline.limit_display_backlog(10)
        tab._drain_display()

        assert _console_texts(tab) == [
            "before",
            "[WireTrace] 490 lines were not shown: the display could not keep up. "
            "They are in the session and in the log.",
            *[f"line {n}" for n in range(490, 500)],
        ]

    def test_notices_are_shown_whatever_the_filter(self, qapp, tab, port):
        _connect(qapp, tab)
        port.feed(b"alpha\nbeta\n")
        _wait_for_lines(qapp, tab, 2)
        _console(tab).set_filter("alpha")

        tab._display_timer.stop()
        port.feed(b"".join(f"alpha {n}\n".encode() for n in range(100)))
        wait_until(qapp, lambda: tab.session.total_lines == 102)
        tab._pipeline.limit_display_backlog(1)
        tab._drain_display()

        assert _console_texts(tab) == [
            "alpha",
            "[WireTrace] 99 lines were not shown: the display could not keep up. "
            "They are in the session and in the log.",
            "alpha 99",
        ]

    def test_console_shows_exactly_as_many_lines_as_its_limit(
        self, qapp, tab, port, config, monkeypatch,
    ):
        console_limit = 1_000
        monkeypatch.setattr(
            type(config), "max_console_lines", property(lambda self: console_limit),
        )
        tab.apply_preferences()
        _connect(qapp, tab)
        for start in range(0, 1_500, 100):
            port.feed(b"".join(f"line {n}\n".encode() for n in range(start, start + 100)))
            _wait_for_last_line(qapp, tab, f"line {start + 99}")

        assert _console(tab).total_count == console_limit
        assert _console(tab).visible_count == console_limit
        assert _console_texts(tab) == [f"line {n}" for n in range(500, 1_500)]

    def test_search_follows_the_console_when_it_is_rendered_again(
        self, qapp, tab, port,
    ):
        _connect(qapp, tab)
        port.feed(b"alpha one\nbeta\nalpha two\n")
        _wait_for_lines(qapp, tab, 3)
        search = tab.findChild(SearchBar)
        search.activate()
        search.findChild(QLineEdit).setText("alpha")
        assert _console(tab).match_count == 0, "the search waits for typing to pause"
        tab.search_next()
        assert _console(tab).match_count == 2
        assert _console(tab).current_match == 0
        tab.search_next()
        assert _console(tab).current_match == 1

        tab.set_timestamp_mode(TimestampMode.RELATIVE)

        assert _console(tab).match_count == 2
        assert search._count_label.text() == "1 of 2"
        tab.search_next()
        assert search._count_label.text() == "2 of 2"
        tab.search_prev()
        assert search._count_label.text() == "1 of 2"

    def test_filtered_console_shows_only_lines_it_still_holds(
        self, qapp, tab, port, config, monkeypatch,
    ):
        console_limit = 1_000
        monkeypatch.setattr(
            type(config), "max_console_lines", property(lambda self: console_limit),
        )
        tab.apply_preferences()
        _connect(qapp, tab)
        _console(tab).set_filter("even")
        for start in range(0, 3_000, 100):
            port.feed(b"".join(
                f"{'even' if n % 2 == 0 else 'odd'} {n}\n".encode()
                for n in range(start, start + 100)
            ))
            _wait_for_last_line(qapp, tab, f"even {start + 98}")

        held_and_matching = [f"even {n}" for n in range(2_000, 3_000, 2)]
        assert _console_texts(tab) == held_and_matching
        # Rendering again changes nothing: the document already agreed
        # with the lines held.
        tab.set_timestamp_mode(TimestampMode.RELATIVE)
        assert _console_texts(tab) == held_and_matching

    def test_notice_takes_the_time_of_the_line_it_follows(self, qapp, tab, port):
        _connect(qapp, tab)
        port.feed(b"one\n")
        _wait_for_lines(qapp, tab, 1)
        tab._on_writer_error("something unexpected")

        stamps = [record.timestamp for record in _console(tab).shown_records()]
        assert len(stamps) == 2
        assert stamps[0] == stamps[1]

    def test_search_is_reset_when_the_console_is_cleared(self, qapp, tab, port):
        _connect(qapp, tab)
        port.feed(b"alpha one\nalpha two\n")
        _wait_for_lines(qapp, tab, 2)
        search = tab.findChild(SearchBar)
        search.activate()
        search.findChild(QLineEdit).setText("alpha")
        wait_until(qapp, lambda: _console(tab).match_count == 2)

        _toolbar(tab).clear_requested.emit()

        assert _console(tab).match_count == 0
        assert search._count_label.text() == "No results"

    def test_filter_is_applied_once_typing_pauses(self, qapp, tab, port):
        _connect(qapp, tab)
        port.feed(b"alpha\nbeta\nalphabet\n")
        _wait_for_lines(qapp, tab, 3)

        tab.findChild(ConsoleHeader).set_filter_text("alpha")
        assert _console(tab).visible_count == 3
        wait_until(qapp, lambda: _console(tab).visible_count == 2)

        assert _console_texts(tab) == ["alpha", "alphabet"]
        assert _console(tab).total_count == 3

    def test_level_button_shows_only_lines_of_that_level(self, qapp, tab, port):
        _connect(qapp, tab)
        port.feed(b"[INFO] boot\n[ERROR] sensor missing\nplain data\n[CRITICAL] brownout\n")
        _wait_for_lines(qapp, tab, 4)
        header = tab.findChild(ConsoleHeader)

        assert header.level_texts() == [
            "All  4", "Error  2", "Warning  0", "Info  1", "Debug  0", "Data  1",
        ]

        error = next(choice for choice in LEVEL_CHOICES if choice.key == "error")
        header._choose(error)

        assert _console_texts(tab) == ["[ERROR] sensor missing", "[CRITICAL] brownout"]
        assert _console(tab).total_count == 4

        header._choose(LEVEL_CHOICES[0])
        assert len(_console_texts(tab)) == 4

    def test_new_lines_are_counted_while_the_view_is_held(self, qapp, tab, port):
        following: list[bool] = []
        tab.following_changed.connect(following.append)
        _connect(qapp, tab)
        port.feed(b"one\n")
        _wait_for_lines(qapp, tab, 1)
        button = tab.findChild(QPushButton, "newLinesButton")
        assert tab.following is True
        assert not button.isVisible()

        tab.set_following(False)
        port.feed(b"two\nthree\n")
        _wait_for_lines(qapp, tab, 3)

        assert following == [False]
        assert button.isVisible()
        assert button.text() == "2 new lines"

        button.click()

        assert following == [False, True]
        assert not button.isVisible()

    def test_display_and_time_choices_are_announced(self, qapp, tab, port):
        modes: list[object] = []
        tab.display_mode_changed.connect(modes.append)
        tab.timestamp_mode_changed.connect(modes.append)
        _connect(qapp, tab)

        tab.set_display_mode(DisplayMode.HEX)
        tab.set_timestamp_mode(TimestampMode.RELATIVE)

        assert modes == [DisplayMode.HEX, TimestampMode.RELATIVE]
        assert tab.session.display_mode is DisplayMode.HEX
        assert tab.session.timestamp_mode is TimestampMode.RELATIVE

    def test_font_preferences_reach_the_console(self, qapp, tab, config):
        config.font_size = config.font_size + 3
        tab.apply_preferences()

        assert _console(tab).font().pixelSize() == config.font_size

    def test_plot_receives_values_while_its_panel_is_open(self, qapp, tab, port):
        _connect(qapp, tab)
        tab.toggle_plot()
        port.feed(b"".join(b"temp: %d, rpm: %d\n" % (n, n * 2) for n in range(50)))
        _wait_for_lines(qapp, tab, 50)

        assert sorted(tab._plot_engine.columns) == ["rpm", "temp"]


class TestConsoleMenu:
    @staticmethod
    def _entries(tab: DeviceTab) -> dict[str, QAction]:
        """The entries of the console menu by their text."""
        return {
            action.text().split("\t")[0]: action
            for action in tab.console_menu().actions() if action.text()
        }

    def test_nothing_selected_leaves_only_what_needs_no_selection(self, qapp, tab, port):
        _connect(qapp, tab)
        port.feed(b"alpha one\nbeta\n")
        _wait_for_lines(qapp, tab, 2)

        entries = self._entries(tab)

        enabled = {text for text, action in entries.items() if action.isEnabled()}
        assert enabled == {"Select all", "Clear console"}

    def test_selected_phrase_can_become_the_filter_or_the_search(self, qapp, tab, port):
        _connect(qapp, tab)
        port.feed(b"alpha\nbeta\nalpha again\n")
        _wait_for_lines(qapp, tab, 3)
        console = _console(tab)
        console.set_filter("beta")
        console.select_all()

        entries = self._entries(tab)
        assert all(action.isEnabled() for action in entries.values())
        console.set_filter("")

        entries['Find "beta"'].trigger()
        assert tab.findChild(SearchBar).isVisible()
        assert console.match_count == 1

        entries['Show only lines with "beta"'].trigger()
        wait_until(qapp, lambda: console.visible_count == 1)
        assert _console_texts(tab) == ["beta"]

    def test_long_selection_is_shortened_in_the_menu(self, qapp, tab, port):
        _connect(qapp, tab)
        port.feed(b"abcdefghijklmnopqrstuvwxyz0123456789\n")
        _wait_for_lines(qapp, tab, 1)
        _console(tab).select_all()

        assert any(
            text.startswith('Find "') and text.endswith('..."') for text in self._entries(tab)
        )

    def test_selection_is_exported_to_the_chosen_file(
        self, qapp, tab, port, tmp_path, monkeypatch, opened_folders,
    ):
        target = tmp_path / "picked" / "selection.txt"
        monkeypatch.setattr(
            QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(target), ""),
        )
        _connect(qapp, tab)
        port.feed(b"one\ntwo\n")
        _wait_for_lines(qapp, tab, 2)
        _console(tab).select_all()

        tab.export_selection()

        lines = target.read_text(encoding="utf-8").splitlines()
        assert [line.split()[-1] for line in lines] == ["one", "two"]
        assert Toast.of(tab).message == "Saved 2 lines: selection.txt"

    def test_export_of_a_selection_can_be_cancelled(self, qapp, tab, port, monkeypatch):
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: ("", ""))
        _connect(qapp, tab)
        port.feed(b"one\n")
        _wait_for_lines(qapp, tab, 1)
        _console(tab).select_all()

        tab.export_selection()

        assert not Toast.of(tab).isVisible()


class TestClosing:
    def test_closing_while_logging_asks_first(
        self, qapp, tab, port, accept_dialogs, monkeypatch,
    ):
        _connect(qapp, tab)
        _start_log(tab)
        asked: list[str] = []

        def keep(box: QMessageBox) -> None:
            asked.append(box.text())
            box.done(0)

        monkeypatch.setattr(QMessageBox, "exec", keep)

        assert tab.confirm_close() is False
        assert asked == [f"{_PORT} is still logging"]
        assert tab.is_logging

    def test_closing_during_an_export_asks_first(
        self, qapp, tab, port, accept_dialogs, monkeypatch,
    ):
        _connect(qapp, tab)
        port.feed(b"one\n")
        _wait_for_lines(qapp, tab, 1)
        _disconnect(qapp, tab)
        assert tab.confirm_close() is True

        asked: list[str] = []

        def cancel(box):
            asked.append(box.text())
            box.done(0)

        monkeypatch.setattr(QMessageBox, "exec", cancel)
        monkeypatch.setattr(type(tab), "is_exporting", property(lambda self: True))
        assert tab.confirm_close() is False
        assert asked == ["An export is still being written"]
