# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests of the main window with real widgets: its tabs, its menus and
how it closes. A loopback port stands in for the hardware."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QMenu, QMessageBox

import ui.main_window as main_window_module
from app.config import ConfigManager
from app.constants import (
    DEFAULT_FONT_SIZE,
    FONT_SIZE_MAX,
    FONT_SIZE_MIN,
    FONT_SIZE_STEP,
    NAME_SEPARATOR,
    DisplayMode,
    LineEnding,
    ThemeID,
    TimestampMode,
)
from core.transport import PortInfo
from tests_gui.support import LoopbackPort, pixel, wait_until
from ui import shortcuts
from ui.device_tab import NEW_TAB_TITLE, DeviceTab
from ui.dialogs.shortcuts_dialog import ShortcutsDialog
from ui.main_window import MainWindow, TabBar, TabWidget
from ui.themes import tokens
from ui.themes.theme_manager import THEME_FOLLOW_SYSTEM, ThemeManager
from ui.widgets.command_bar import CommandBar
from ui.widgets.connect_page import ConnectPage
from ui.widgets.styling import plain_tooltip
from ui.widgets.toast import Toast
from version import APP_ISSUES_URL, WINDOW_TITLE

_PORT = "PORT0"
_BAUD = 115200


@pytest.fixture
def port() -> LoopbackPort:
    return LoopbackPort()


@pytest.fixture
def window(
    qapp: QApplication,
    config: ConfigManager,
    theme_manager: ThemeManager,
    port: LoopbackPort,
) -> MainWindow:
    theme_manager.apply_theme(ThemeID.LIGHT)
    main_window = MainWindow(config, theme_manager, transport_factory=port.factory)
    main_window.show()
    qapp.processEvents()
    yield main_window
    main_window.shutdown_all_tabs()


def _connect(qapp: QApplication, tab: DeviceTab, device: str = _PORT) -> None:
    tab.findChild(ConnectPage).connect_requested.emit(device, _BAUD)
    wait_until(qapp, lambda: tab.is_connected)


def _bar(window: MainWindow) -> TabBar:
    return window.findChild(TabWidget).bar


def _actions(window: MainWindow) -> dict[str, QAction]:
    """Every entry of every menu, by its path: "View/Theme/Dark"."""
    found: dict[str, QAction] = {}

    def walk(prefix: str, actions: list[QAction]) -> None:
        for action in actions:
            if action.isSeparator():
                continue
            path = f"{prefix}{action.text().replace('&', '')}"
            if action.menu() is not None:
                walk(f"{path}/", action.menu().actions())
            else:
                found[path] = action

    walk("", window.menuBar().actions())
    return found


def _menu(window: MainWindow, name: str) -> QMenu:
    """A menu of the menu bar by its name."""
    return next(
        action.menu() for action in window.menuBar().actions()
        if action.text().replace("&", "") == name
    )


def _enabled(window: MainWindow) -> set[str]:
    """Paths of the entries that can be chosen, submenus that are off left out."""
    off = {
        f"{top.text().replace('&', '')}/{sub.text()}/"
        for top in window.menuBar().actions()
        for sub in top.menu().actions()
        if sub.menu() is not None and not sub.menu().isEnabled()
    }
    return {
        path for path, action in _actions(window).items()
        if action.isEnabled() and not any(path.startswith(prefix) for prefix in off)
    }


class TestWindow:
    @pytest.mark.parametrize("theme_id", list(ThemeID))
    def test_window_builds_and_shows_in_every_theme(
        self, qapp, config, theme_manager, theme_id,
    ):
        assert theme_manager.apply_theme(theme_id) is True
        window = MainWindow(config, theme_manager)
        window.show()
        qapp.processEvents()

        assert window.isVisible()
        assert window.windowTitle() == WINDOW_TITLE
        assert [tab.title for tab in window.tabs()] == [NEW_TAB_TITLE]

        window.close()
        qapp.processEvents()
        assert not window.isVisible()

    def test_window_cannot_be_made_smaller_than_its_rows_need(self, window):
        metrics = tokens.METRICS

        assert window.minimumWidth() == metrics.window_min_width
        assert window.minimumHeight() == metrics.window_min_height

    def test_menus_are_organised_by_task(self, window):
        tops = [action.text().replace("&", "") for action in window.menuBar().actions()]

        assert tops == ["File", "Edit", "View", "Device", "Help"]


class TestTabs:
    def test_new_tab_opens_in_front(self, qapp, window):
        first = window.current_tab()

        second = window.new_tab()

        assert window.tabs() == [first, second]
        assert window.current_tab() is second

    def test_closing_a_tab_shuts_it_down_and_removes_it(self, qapp, window):
        first = window.current_tab()
        second = window.new_tab()

        window.close_tab(0)

        assert window.tabs() == [second]
        assert first._shut_down is True

    def test_closing_the_tab_in_front_shows_the_next_without_an_error(self, qapp, window, capfd):
        """Qt shows the next tab's first screen, which reads the ports in use,
        while the closed tab is still on the tab bar but no longer a page."""
        first = window.current_tab()
        second = window.new_tab()

        window.close_tab(window.tabs().index(second))
        qapp.processEvents()

        assert "Traceback" not in capfd.readouterr().err
        assert window.tabs() == [first]
        assert window.current_tab() is first

    def test_closing_the_last_tab_opens_a_new_one(self, qapp, window):
        only = window.current_tab()

        window.close_current_tab()

        assert len(window.tabs()) == 1
        assert window.current_tab() is not only
        assert window.current_tab().title == NEW_TAB_TITLE

    def test_tab_that_refuses_to_close_stays(self, qapp, window, monkeypatch):
        tab = window.current_tab()
        monkeypatch.setattr(DeviceTab, "confirm_close", lambda self: False)

        window.close_current_tab()

        assert window.tabs() == [tab]
        assert tab._shut_down is False

    def test_tab_and_window_title_name_the_connected_port(self, qapp, window, ports):
        ports.append(PortInfo(_PORT, "Bench adapter", ""))
        tabs = window.findChild(TabWidget)

        _connect(qapp, window.current_tab())

        assert tabs.tabText(0) == f"{_PORT}{NAME_SEPARATOR}Bench adapter"
        assert tabs.tabToolTip(0) == plain_tooltip(f"{_PORT}{NAME_SEPARATOR}Bench adapter")
        assert window.windowTitle() == f"{_PORT}{NAME_SEPARATOR}Bench adapter - {WINDOW_TITLE}"

        window.new_tab()
        assert window.windowTitle() == WINDOW_TITLE
        tabs.setCurrentIndex(0)
        assert window.windowTitle() == f"{_PORT}{NAME_SEPARATOR}Bench adapter - {WINDOW_TITLE}"

    def test_dot_on_the_tab_shows_the_connection(self, qapp, window):
        tabs = window.findChild(TabWidget)
        side = tokens.METRICS.status_dot

        def centre_colour() -> str:
            image = tabs.tabIcon(0).pixmap(side, side).toImage()
            return image.pixelColor(image.width() // 2, image.height() // 2).name().upper()

        assert centre_colour() != tokens.palette().connected
        _connect(qapp, window.current_tab())
        assert centre_colour() == tokens.palette().connected

    def test_dots_are_redrawn_when_the_theme_changes(self, qapp, window, theme_manager):
        tabs = window.findChild(TabWidget)
        side = tokens.METRICS.status_dot
        _connect(qapp, window.current_tab())

        theme_manager.apply_theme(ThemeID.DARK)

        image = tabs.tabIcon(0).pixmap(side, side).toImage()
        centre = image.pixelColor(image.width() // 2, image.height() // 2).name().upper()
        assert centre == tokens.DARK.connected

    def test_port_open_in_one_tab_cannot_be_chosen_in_another(self, qapp, window, ports):
        ports.extend([PortInfo(_PORT, "", ""), PortInfo("PORT1", "", "")])
        _connect(qapp, window.current_tab())

        second = window.new_tab()
        qapp.processEvents()
        page = second.findChild(ConnectPage)

        assert window.get_connected_ports() == {_PORT}
        assert page.row(_PORT).available is False
        assert page.row(_PORT).badge.text() == "Open in another tab"
        assert page.selected_port == "PORT1"

    def test_new_tab_button_sits_after_the_last_tab(self, qapp, window):
        bar = _bar(window)
        button = bar.findChild(type(bar._add), "flatIconButton")

        before = button.x()
        window.new_tab()
        qapp.processEvents()

        assert button.x() > before
        assert button.x() > bar.tabRect(1).right()
        assert button.geometry().right() <= bar.width()

    def test_new_tab_button_stays_in_reach_when_tabs_overflow(self, qapp, window):
        for _ in range(30):
            window.new_tab()
        qapp.processEvents()
        bar = _bar(window)

        assert bar.rect().contains(bar._add.geometry())

        bar._add.click()
        assert len(window.tabs()) == 32


class TestRename:
    def test_name_is_typed_on_the_tab_itself(self, qapp, window):
        tab = window.current_tab()
        _connect(qapp, tab)
        bar = _bar(window)
        editor = bar.findChild(QLineEdit, "tabNameEditor")

        window.rename_current_tab()
        assert editor.isVisible()
        assert bar.tabRect(0).contains(editor.geometry())

        QTest.keyClicks(editor, "Pump")
        QTest.keyClick(editor, Qt.Key.Key_Return)

        assert not editor.isVisible()
        assert tab.custom_name == "Pump"
        assert window.findChild(TabWidget).tabText(0) == f"{_PORT}{NAME_SEPARATOR}Pump"

    def test_escape_leaves_the_name_as_it_was(self, qapp, window):
        tab = window.current_tab()
        tab.rename("Pump")
        bar = _bar(window)
        editor = bar.findChild(QLineEdit, "tabNameEditor")

        window.rename_current_tab()
        assert editor.text() == "Pump"
        QTest.keyClicks(editor, "Valve")
        QTest.keyClick(editor, Qt.Key.Key_Escape)

        assert not editor.isVisible()
        assert tab.custom_name == "Pump"

    def test_double_click_on_a_tab_starts_renaming(self, qapp, window):
        bar = _bar(window)

        QTest.mouseDClick(bar, Qt.MouseButton.LeftButton, pos=bar.tabRect(0).center())

        assert bar.editing is True

    def test_switching_tabs_keeps_what_was_typed(self, qapp, window):
        first = window.current_tab()
        window.new_tab()
        tabs = window.findChild(TabWidget)
        tabs.setCurrentIndex(0)
        editor = _bar(window).findChild(QLineEdit, "tabNameEditor")

        window.rename_current_tab()
        QTest.keyClicks(editor, "Pump")
        tabs.setCurrentIndex(1)

        assert first.custom_name == "Pump"
        assert not editor.isVisible()

    def test_closing_the_tab_being_renamed_renames_nothing(self, qapp, window):
        window.new_tab()
        editor = _bar(window).findChild(QLineEdit, "tabNameEditor")

        window.rename_tab(1)
        QTest.keyClicks(editor, "Pump")
        window.close_tab(1)

        assert not editor.isVisible()
        assert [tab.custom_name for tab in window.tabs()] == [""]


class TestShortcuts:
    def test_every_shortcut_in_the_table_reaches_a_menu_entry(self, window):
        bound = window.shortcut_actions()

        assert set(bound) == set(shortcuts.SHORTCUTS)
        for name, action in bound.items():
            expected = [shortcuts.keys(name), *shortcuts.ALTERNATE_KEYS.get(name, ())]
            assert action.shortcuts() == [QKeySequence(keys) for keys in expected], name

    def test_no_menu_entry_has_a_shortcut_outside_the_table(self, window):
        from_table = {id(action) for action in window.shortcut_actions().values()}

        stray = [
            path for path, action in _actions(window).items()
            if not action.shortcut().isEmpty() and id(action) not in from_table
        ]
        assert stray == []

    def test_entries_are_not_moved_by_macos_guessing_from_their_text(self, window):
        roles = {
            path: action.menuRole() for path, action in _actions(window).items()
            if action.menuRole() is not QAction.MenuRole.NoRole
        }

        assert roles == {
            "File/Preferences...": QAction.MenuRole.PreferencesRole,
            "File/Exit": QAction.MenuRole.QuitRole,
            "Help/About WireTrace": QAction.MenuRole.AboutRole,
        }

    def test_shortcut_list_shows_the_whole_table(self, qapp, window):
        dialog = ShortcutsDialog(window)

        assert dialog.rows() == [
            (shortcuts.native_keys(name), shortcut.description)
            for name, shortcut in shortcuts.SHORTCUTS.items()
        ]

    def test_help_menu_opens_the_shortcut_list(self, window, monkeypatch):
        opened: list[ShortcutsDialog] = []
        monkeypatch.setattr(ShortcutsDialog, "exec", lambda dialog: opened.append(dialog))

        _actions(window)["Help/Keyboard shortcuts"].trigger()

        assert len(opened) == 1


_SESSION_ONLY = frozenset({
    "File/Export session...",
    "Edit/Copy without timestamps",
    "Edit/Find...",
    "Edit/Find next",
    "Edit/Find previous",
    "Edit/Filter lines",
    "Edit/Close Find and clear the filter",
    "Edit/Clear console",
    "View/Timestamps/Time of day",
    "View/Timestamps/Since previous line",
    "View/Follow new lines",
    "Device/Line ending for commands/None",
    "Device/Line ending for commands/LF",
    "Device/Line ending for commands/CR",
    "Device/Line ending for commands/CR LF",
})
_CONNECTED_ONLY = frozenset({
    "File/Start log...",
    "View/Display/Text",
    "View/Display/HEX",
    "View/Live plot",
    "Device/Disconnect",
})
_NOT_CONNECTED_ONLY = frozenset({"Device/Connect", "Device/Refresh ports"})


class TestMenuState:
    def test_new_tab_offers_only_what_it_can_do(self, window):
        enabled = _enabled(window)

        assert not enabled & (_SESSION_ONLY | _CONNECTED_ONLY | {"File/Stop log"})
        assert enabled >= _NOT_CONNECTED_ONLY

    def test_connected_tab_offers_the_session_actions(self, qapp, window):
        _connect(qapp, window.current_tab())

        enabled = _enabled(window)

        assert enabled >= _SESSION_ONLY | _CONNECTED_ONLY
        assert not enabled & (_NOT_CONNECTED_ONLY | {"File/Stop log"})

    def test_disconnected_tab_keeps_what_works_on_the_kept_console(self, qapp, window):
        tab = window.current_tab()
        _connect(qapp, tab)
        tab.disconnect_device()
        wait_until(qapp, lambda: not tab.is_connected)

        enabled = _enabled(window)

        assert enabled >= _SESSION_ONLY | _NOT_CONNECTED_ONLY
        assert not enabled & _CONNECTED_ONLY

    def test_menus_follow_the_tab_in_front(self, qapp, window):
        first = window.current_tab()
        _connect(qapp, first)
        first.set_timestamp_mode(TimestampMode.RELATIVE)
        first.set_display_mode(DisplayMode.HEX)
        first.set_line_ending(LineEnding.CRLF)
        first.set_following(False)
        actions = _actions(window)

        def ticked() -> set[str]:
            return {path for path, action in actions.items() if action.isChecked()}

        assert {
            "View/Timestamps/Since previous line",
            "View/Display/HEX",
            "Device/Line ending for commands/CR LF",
        } <= ticked()
        assert "View/Follow new lines" not in ticked()

        window.new_tab()

        assert {
            "View/Timestamps/Time of day",
            "View/Display/Text",
            "Device/Line ending for commands/LF",
            "View/Follow new lines",
        } <= ticked()

    def test_a_tab_in_the_background_does_not_change_the_menus(self, qapp, window):
        first = window.current_tab()
        _connect(qapp, first)
        window.new_tab()
        actions = _actions(window)

        first.set_timestamp_mode(TimestampMode.RELATIVE)

        assert actions["View/Timestamps/Time of day"].isChecked()

    def test_menu_entries_act_on_the_tab_in_front(self, qapp, window):
        tab = window.current_tab()
        _connect(qapp, tab)
        actions = _actions(window)

        actions["View/Timestamps/Since previous line"].trigger()
        actions["View/Display/HEX"].trigger()
        actions["Device/Line ending for commands/CR"].trigger()
        actions["View/Follow new lines"].trigger()
        actions["View/Live plot"].trigger()

        assert tab.session.timestamp_mode is TimestampMode.RELATIVE
        assert tab.session.display_mode is DisplayMode.HEX
        assert tab.line_ending is LineEnding.CR
        assert tab.following is False
        assert tab.is_plot_visible() is True
        assert actions["View/Live plot"].isChecked()

        actions["Device/Disconnect"].trigger()
        wait_until(qapp, lambda: not tab.is_connected)
        assert not actions["View/Live plot"].isEnabled()

    def test_connect_entry_uses_the_port_chosen_on_the_first_screen(self, qapp, window, ports):
        ports.append(PortInfo(_PORT, "", ""))
        tab = window.current_tab()
        tab.refresh_ports()

        _actions(window)["Device/Connect"].trigger()
        wait_until(qapp, lambda: tab.is_connected)

        assert tab.port_name == _PORT


class TestView:
    def test_theme_chosen_in_the_menu_is_applied_and_saved(self, window, config, theme_manager):
        actions = _actions(window)

        actions["View/Theme/Dark"].trigger()

        assert theme_manager.current_theme is ThemeID.DARK
        assert config.theme == ThemeID.DARK.value
        assert actions["View/Theme/Dark"].isChecked()
        assert not actions["View/Theme/Light"].isChecked()

        actions["View/Theme/System"].trigger()
        assert config.theme == THEME_FOLLOW_SYSTEM

    def test_text_size_changes_in_every_tab_and_stays_in_range(self, qapp, window, config):
        second = window.new_tab()
        actions = _actions(window)
        start = config.font_size

        actions["View/Larger text"].trigger()
        assert config.font_size == start + FONT_SIZE_STEP
        assert {tab.console.font().pixelSize() for tab in window.tabs()} == {config.font_size}

        for _ in range(FONT_SIZE_MAX):
            actions["View/Larger text"].trigger()
        assert config.font_size == FONT_SIZE_MAX

        for _ in range(FONT_SIZE_MAX):
            actions["View/Smaller text"].trigger()
        assert config.font_size == FONT_SIZE_MIN

        actions["View/Reset text size"].trigger()
        assert config.font_size == DEFAULT_FONT_SIZE
        assert second.console.font().pixelSize() == DEFAULT_FONT_SIZE

    def test_hide_blank_lines_is_saved_and_applies_to_every_tab(self, qapp, window, config, port):
        tab = window.current_tab()
        _connect(qapp, tab)
        port.feed(b"one\n\ntwo\n")
        wait_until(qapp, lambda: tab.console.total_count == 3)
        action = _actions(window)["View/Hide blank lines"]
        assert action.isChecked() is config.hide_blank_lines

        action.trigger()

        assert config.hide_blank_lines is True
        assert tab.console.visible_count == 2


class TestSavedCommands:
    def test_shortcut_puts_the_command_in_the_box_and_sends_nothing(self, qapp, window, port):
        tab = window.current_tab()
        _connect(qapp, tab)
        tab.saved_commands.replace(["status", "reset"])

        window.shortcut_actions()[shortcuts.saved_command_name(2)].trigger()

        assert tab.findChild(CommandBar).text == "reset"
        assert port.transport.written == []

    def test_shortcut_without_a_command_says_so(self, qapp, window):
        tab = window.current_tab()
        _connect(qapp, tab)

        window.shortcut_actions()[shortcuts.saved_command_name(3)].trigger()

        assert Toast.of(tab).message == "There is no saved command 3"
        assert tab.findChild(CommandBar).text == ""

    def test_shortcut_while_disconnected_explains_why_nothing_happens(self, qapp, window):
        tab = window.current_tab()
        tab.saved_commands.replace(["status"])

        window.shortcut_actions()[shortcuts.saved_command_name(1)].trigger()

        assert Toast.of(tab).message == "Commands can be sent only while a device is connected"

    def test_device_menu_lists_what_is_saved_when_it_opens(self, qapp, window):
        tab = window.current_tab()
        _connect(qapp, tab)
        menu = next(
            action.menu() for action in _menu(window, "Device").actions()
            if action.text() == "Saved commands"
        )

        menu.aboutToShow.emit()
        assert [a.text() for a in menu.actions()] == ["Edit saved commands..."]

        tab.saved_commands.replace([f"cmd {n} & more" for n in range(1, 12)])
        menu.aboutToShow.emit()
        texts = [a.text() for a in menu.actions() if not a.isSeparator()]

        assert texts[0] == f"cmd 1 && more\t{shortcuts.native_keys('saved_command_1')}"
        assert texts[9] == "cmd 10 && more"
        assert texts[-1] == "Edit saved commands..."
        assert all(a.shortcut().isEmpty() for a in menu.actions())

        menu.actions()[3].trigger()
        assert tab.findChild(CommandBar).text == "cmd 4 & more"

    def test_commands_saved_in_one_tab_are_offered_in_every_tab(self, qapp, window):
        first = window.current_tab()
        second = window.new_tab()

        first.saved_commands.add("status")

        assert second.findChild(CommandBar).saved_menu_texts()[0].startswith("status")

    def test_edit_dialog_replaces_the_list(self, qapp, window, monkeypatch):
        from ui.dialogs.saved_commands_dialog import SavedCommandsDialog

        tab = window.current_tab()
        tab.saved_commands.replace(["status", "reset"])
        monkeypatch.setattr(
            SavedCommandsDialog, "exec", lambda dialog: QDialog.DialogCode.Accepted,
        )
        monkeypatch.setattr(SavedCommandsDialog, "commands", lambda dialog: ["reset", "new"])

        tab.edit_saved_commands()

        assert tab.saved_commands.all() == ["reset", "new"]

    def test_cancelled_edit_changes_nothing(self, qapp, window, monkeypatch):
        from ui.dialogs.saved_commands_dialog import SavedCommandsDialog

        tab = window.current_tab()
        tab.saved_commands.replace(["status"])
        monkeypatch.setattr(
            SavedCommandsDialog, "exec", lambda dialog: QDialog.DialogCode.Rejected,
        )
        monkeypatch.setattr(SavedCommandsDialog, "commands", lambda dialog: [])

        tab.edit_saved_commands()

        assert tab.saved_commands.all() == ["status"]


class TestPreferences:
    def test_saved_preferences_reach_every_tab_and_are_confirmed(
        self, qapp, window, config, monkeypatch,
    ):
        from ui.dialogs.preferences_dialog import PreferencesDialog

        second = window.new_tab()

        def change_and_save(dialog: PreferencesDialog) -> QDialog.DialogCode:
            dialog._font_size.setValue(DEFAULT_FONT_SIZE + 4)
            dialog._hide_blank.setChecked(True)
            dialog._apply()
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(PreferencesDialog, "exec", change_and_save)

        _actions(window)["File/Preferences..."].trigger()

        assert {tab.console.font().pixelSize() for tab in window.tabs()} == {DEFAULT_FONT_SIZE + 4}
        assert _actions(window)["View/Hide blank lines"].isChecked()
        assert Toast.of(second).message == "Preferences saved"

    def test_cancelled_preferences_confirm_nothing(self, qapp, window, monkeypatch):
        from ui.dialogs.preferences_dialog import PreferencesDialog

        monkeypatch.setattr(PreferencesDialog, "exec", lambda dialog: QDialog.DialogCode.Rejected)

        _actions(window)["File/Preferences..."].trigger()

        assert not Toast.of(window.current_tab()).isVisible()

    def test_preferences_can_open_the_plot_configuration_of_the_tab(
        self, qapp, window, monkeypatch,
    ):
        from ui.dialogs.plot_config_dialog import PlotConfigDialog
        from ui.dialogs.preferences_dialog import PreferencesDialog

        opened: list[QDialog] = []
        monkeypatch.setattr(PlotConfigDialog, "exec", lambda dialog: opened.append(dialog))

        def press_configure(dialog: PreferencesDialog) -> QDialog.DialogCode:
            dialog._configure_plot.click()
            return QDialog.DialogCode.Rejected

        monkeypatch.setattr(PreferencesDialog, "exec", press_configure)

        _actions(window)["File/Preferences..."].trigger()

        assert len(opened) == 1


class TestEdit:
    def test_copy_takes_the_selection_of_the_console(self, qapp, window, port):
        tab = window.current_tab()
        _connect(qapp, tab)
        port.feed(b"one\ntwo\n")
        wait_until(qapp, lambda: tab.console.total_count == 2)
        tab.console.setFocus()
        actions = _actions(window)

        actions["Edit/Select all"].trigger()
        actions["Edit/Copy without timestamps"].trigger()
        assert qapp.clipboard().text() == "one\ntwo"

        actions["Edit/Copy"].trigger()
        assert [line.split()[-1] for line in qapp.clipboard().text().splitlines()] == ["one", "two"]

    def test_copy_and_select_all_work_in_the_text_box_being_typed_in(self, qapp, window):
        tab = window.current_tab()
        _connect(qapp, tab)
        window.activateWindow()
        actions = _actions(window)
        actions["Edit/Filter lines"].trigger()
        field = qapp.focusWidget()
        assert isinstance(field, QLineEdit)
        field.setText("needle")

        actions["Edit/Select all"].trigger()
        actions["Edit/Copy"].trigger()

        assert field.selectedText() == "needle"
        assert qapp.clipboard().text() == "needle"
        assert tab.console.has_selection is False

    def test_open_edit_menu_switches_off_what_has_nothing_to_act_on(self, qapp, window, port):
        tab = window.current_tab()
        _connect(qapp, tab)
        tab.console.setFocus()
        actions = _actions(window)
        edit = next(a.menu() for a in window.menuBar().actions() if a.text() == "&Edit")

        edit.aboutToShow.emit()
        assert not actions["Edit/Copy"].isEnabled()
        assert not actions["Edit/Select all"].isEnabled()

        edit.aboutToHide.emit()
        assert actions["Edit/Copy"].isEnabled()
        assert actions["Edit/Select all"].isEnabled()

        port.feed(b"one\n")
        wait_until(qapp, lambda: tab.console.total_count == 1)
        tab.console.select_all()
        edit.aboutToShow.emit()
        assert actions["Edit/Copy"].isEnabled()
        assert actions["Edit/Copy without timestamps"].isEnabled()

    def test_find_opens_and_escape_closes_it(self, qapp, window):
        from ui.widgets.search_bar import SearchBar

        tab = window.current_tab()
        _connect(qapp, tab)
        actions = _actions(window)

        actions["Edit/Find..."].trigger()
        assert tab.findChild(SearchBar).isVisible()

        actions["Edit/Close Find and clear the filter"].trigger()
        assert not tab.findChild(SearchBar).isVisible()


class TestHelp:
    @pytest.fixture
    def opened_urls(self, monkeypatch: pytest.MonkeyPatch) -> list[QUrl]:
        urls: list[QUrl] = []
        monkeypatch.setattr(
            main_window_module.QDesktopServices, "openUrl", lambda url: urls.append(url) or True,
        )
        return urls

    def test_report_a_problem_opens_the_issue_page(self, window, opened_urls):
        _actions(window)["Help/Report a problem"].trigger()

        assert [url.toString() for url in opened_urls] == [APP_ISSUES_URL]

    def test_page_that_cannot_be_opened_is_named_so_it_can_be_typed(self, window, monkeypatch):
        monkeypatch.setattr(main_window_module.QDesktopServices, "openUrl", lambda url: False)

        _actions(window)["Help/Report a problem"].trigger()

        assert APP_ISSUES_URL in Toast.of(window.current_tab()).message

    def test_user_guide_opens_in_the_browser(self, window, opened_urls, tmp_path, monkeypatch):
        import app.help_loader as help_loader

        guide = tmp_path / "guide.html"
        guide.write_text("<html></html>", encoding="utf-8")
        monkeypatch.setattr(help_loader, "prepare_user_guide", lambda: guide)

        _actions(window)["Help/User guide"].trigger()

        assert [Path(url.toLocalFile()) for url in opened_urls] == [guide]

    def test_missing_user_guide_is_reported(self, window, opened_urls, monkeypatch):
        import app.help_loader as help_loader

        def missing() -> None:
            raise help_loader.UserGuideNotFoundError("gone")

        monkeypatch.setattr(help_loader, "prepare_user_guide", missing)

        _actions(window)["Help/User guide"].trigger()

        assert opened_urls == []
        assert Toast.of(window.current_tab()).message.startswith("The user guide is not installed")

    def test_damaged_user_guide_is_reported(self, window, opened_urls, monkeypatch):
        import app.help_loader as help_loader

        def damaged() -> None:
            raise help_loader.UserGuideError("a picture is missing")

        monkeypatch.setattr(help_loader, "prepare_user_guide", damaged)

        _actions(window)["Help/User guide"].trigger()

        assert opened_urls == []
        assert Toast.of(window.current_tab()).message.startswith("The user guide is damaged")

    def test_about_opens_the_about_dialog(self, window, monkeypatch):
        from ui.dialogs.about_dialog import AboutDialog

        opened: list[QDialog] = []
        monkeypatch.setattr(AboutDialog, "exec", lambda dialog: opened.append(dialog))

        _actions(window)["Help/About WireTrace"].trigger()

        assert len(opened) == 1


class TestClosing:
    @pytest.fixture
    def answer(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
        """Answer the exit question; ``answer["with"]`` is the button to press."""
        state: dict[str, object] = {"with": "Cancel", "asked": []}

        def press(box: QMessageBox) -> None:
            state["asked"].append((box.text(), box.informativeText()))
            next(b for b in box.buttons() if b.text() == state["with"]).click()

        monkeypatch.setattr(QMessageBox, "exec", press)
        return state

    def test_idle_window_closes_without_asking_and_saves_its_place(
        self, qapp, window, config, answer,
    ):
        window.resize(1100, 700)
        qapp.processEvents()
        tab = window.current_tab()

        event = QCloseEvent()
        window.closeEvent(event)

        assert event.isAccepted()
        assert answer["asked"] == []
        assert (config.window_width, config.window_height) == (1100, 700)
        assert tab._shut_down is True

    def test_connected_window_asks_and_stays_open_on_cancel(self, qapp, window, answer):
        tab = window.current_tab()
        _connect(qapp, tab)

        event = QCloseEvent()
        window.closeEvent(event)

        assert not event.isAccepted()
        assert answer["asked"] == [
            (f"1 device is connected: {_PORT}", "Exiting closes every connection."),
        ]
        assert tab.is_connected

    def test_exit_closes_every_connection(self, qapp, window, answer):
        tab = window.current_tab()
        _connect(qapp, tab)
        answer["with"] = "Exit"

        event = QCloseEvent()
        window.closeEvent(event)

        assert event.isAccepted()
        assert tab._shut_down is True
        assert not tab.is_connected

    def test_question_names_what_is_logging_and_exporting(
        self, qapp, window, answer, monkeypatch,
    ):
        first = window.current_tab()
        _connect(qapp, first)
        second = window.new_tab()
        _connect(qapp, second, "PORT1")
        monkeypatch.setattr(DeviceTab, "is_logging", property(lambda self: True))
        monkeypatch.setattr(DeviceTab, "is_exporting", property(lambda self: True))

        window.closeEvent(QCloseEvent())

        text, detail = answer["asked"][0]
        assert text == f"2 devices are still logging: {_PORT}, PORT1"
        assert "written to the files first" in detail
        assert "cancels the export" in detail


    @pytest.mark.parametrize("attempt", range(6))
    def test_window_with_a_plot_is_destroyed_without_harm(
        self, qapp, config, theme_manager, port, attempt,
    ):
        """A window that holds a plot is destroyed safely, plot included.
        A wrong order of destruction would fault only some of the time,
        so this is tried several times."""
        window = MainWindow(config, theme_manager, transport_factory=port.factory)
        window.show()
        tab = window.current_tab()
        _connect(qapp, tab)
        tab.toggle_plot()
        port.feed(b"".join(b"temp: %d, rpm: %d\n" % (n, n * 2) for n in range(50)))
        wait_until(qapp, lambda: tab.console.total_count == 50)
        qapp.processEvents()

        window.shutdown_all_tabs()
        window.close()
        window.deleteLater()
        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qapp.processEvents()

        assert not any(isinstance(widget, MainWindow) for widget in qapp.topLevelWidgets())


class TestTabMenu:
    def test_right_click_outside_a_tab_opens_nothing(self, window, monkeypatch):
        bar = _bar(window)
        monkeypatch.setattr(
            main_window_module.QMenu, "exec", lambda *args: pytest.fail("no menu expected"),
        )

        bar.customContextMenuRequested.emit(QPoint(bar.width() - 1, bar.height() - 1))


def test_window_draws_the_first_screen_on_the_paper_colour(qapp, window):
    corner = pixel(window.current_tab(), 2, window.current_tab().height() - 2)

    assert corner.name().upper() == tokens.palette().paper
