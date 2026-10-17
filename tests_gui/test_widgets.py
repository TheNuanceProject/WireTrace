# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests of the widgets a tab is built from, each on its own."""

from __future__ import annotations

from itertools import pairwise

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton, QWidget

import ui.widgets.connect_page as connect_page_module
from app.constants import (
    BAUD_RATE_MAX,
    COMMAND_HISTORY_LIMIT,
    DEFAULT_BAUD_RATE,
    DEFAULT_LINE_ENDING,
    SAVED_COMMAND_SHORTCUTS,
    SAVED_COMMANDS_MAX,
    TOAST_DURATION_MS,
    TOAST_WITH_ACTION_DURATION_MS,
    DisplayMode,
    LineEnding,
    ThemeID,
    TimestampMode,
)
from app.saved_commands import SavedCommands
from core.log_writer import LogProgress
from core.transport import PortInfo
from tests_gui.support import pixel
from ui.dialogs.saved_commands_dialog import SavedCommandsDialog
from ui.themes import tokens
from ui.themes.theme_manager import ThemeManager
from ui.widgets import banner as banners
from ui.widgets.banner import BannerAction, BannerArea
from ui.widgets.command_bar import LINE_ENDING_NAMES, CommandBar
from ui.widgets.connect_page import ConnectPage
from ui.widgets.console_header import LEVEL_CHOICES, ConsoleHeader
from ui.widgets.console_view import ConsoleView
from ui.widgets.search_bar import SearchBar
from ui.widgets.status_bar import DeviceStatusBar, ElidedLabel
from ui.widgets.status_dot import LinkState, StatusDot, state_icon
from ui.widgets.tab_toolbar import TabToolbar
from ui.widgets.toast import Toast

_WIDE = 1400


@pytest.fixture(autouse=True)
def light_theme(qapp: QApplication, theme_manager: ThemeManager) -> None:
    theme_manager.apply_theme(ThemeID.LIGHT)


def _show(qapp: QApplication, widget: QWidget, width: int = _WIDE, height: int | None = None) -> None:
    widget.resize(width, height if height is not None else widget.sizeHint().height())
    widget.show()
    qapp.processEvents()


def _port(device: str, description: str = "Adapter", manufacturer: str = "Maker") -> PortInfo:
    return PortInfo(device, description, manufacturer)


def _button(parent: QWidget, text: str) -> QPushButton:
    return next(button for button in parent.findChildren(QPushButton) if button.text() == text)


class TestTabToolbar:
    @pytest.fixture
    def toolbar(self, qapp: QApplication) -> TabToolbar:
        bar = TabToolbar()
        bar.set_ports([("PORT0", "PORT0 · Adapter"), ("PORT1", "PORT1")], {"PORT1"})
        _show(qapp, bar)
        return bar

    def test_connect_needs_a_port(self, toolbar):
        hints: list[str] = []
        requests: list[tuple[str, int]] = []
        toolbar.hint.connect(hints.append)
        toolbar.connect_requested.connect(lambda port, baud: requests.append((port, baud)))

        _button(toolbar, "Connect").click()

        assert hints == ["Choose a serial port first"]
        assert requests == []

    def test_connect_asks_for_the_chosen_port_and_baud_rate(self, toolbar):
        requests: list[tuple[str, int]] = []
        toolbar.connect_requested.connect(lambda port, baud: requests.append((port, baud)))
        toolbar.select("PORT0", 9600)

        _button(toolbar, "Connect").click()

        assert requests == [("PORT0", 9600)]

    @pytest.mark.parametrize("text", ["", "0", str(BAUD_RATE_MAX + 1)])
    def test_baud_rate_that_is_not_valid_is_refused_with_the_reason(self, toolbar, text):
        hints: list[str] = []
        requests: list[tuple[str, int]] = []
        toolbar.hint.connect(hints.append)
        toolbar.connect_requested.connect(lambda port, baud: requests.append((port, baud)))
        toolbar.select("PORT0", DEFAULT_BAUD_RATE)
        toolbar.set_baud_text(text)

        _button(toolbar, "Connect").click()

        assert toolbar.selected_baud is None
        assert requests == []
        assert len(hints) == 1 and hints[0].startswith("The baud rate must be a whole number")

    def test_port_open_in_another_tab_is_listed_but_cannot_be_chosen(self, toolbar):
        combo = toolbar.findChild(type(toolbar._port))
        taken = combo.model().item(1)

        assert combo.itemText(1) == "PORT1  (open in another tab)"
        assert not taken.isEnabled()

    def test_port_list_is_read_again_each_time_it_opens(self, toolbar, monkeypatch):
        wanted: list[bool] = []
        toolbar.ports_wanted.connect(lambda: wanted.append(True))
        combo = toolbar._port
        monkeypatch.setattr(type(combo).__mro__[1], "showPopup", lambda self: None)

        combo.showPopup()

        assert wanted == [True]

    def test_buttons_follow_the_connection_and_the_log(self, toolbar):
        def enabled() -> dict[str, bool]:
            return {
                button.text(): button.isEnabled()
                for button in toolbar.findChildren(QPushButton) if button.text()
            }

        assert enabled() == {
            "Connect": True, "Start log": False, "Pause": False, "Clear": False,
            "Export": False, "Plot": False, "Text": False, "HEX": False,
        }

        toolbar.set_connected(True)
        assert enabled() == {
            "Disconnect": True, "Start log": True, "Pause": False, "Clear": True,
            "Export": True, "Plot": True, "Text": True, "HEX": True,
        }

        toolbar.set_logging(True)
        assert toolbar.button_texts() == {
            "connect": "Disconnect", "log": "Stop log", "pause": "Pause",
        }
        assert enabled()["Pause"] is True

        toolbar.set_logging(True, paused=True)
        assert toolbar.button_texts()["pause"] == "Resume"

        toolbar.set_connected(False)
        assert enabled() == {
            "Connect": True, "Start log": False, "Pause": False, "Clear": True,
            "Export": True, "Plot": False, "Text": False, "HEX": False,
        }

    def test_connect_is_the_main_action_only_while_disconnected(self, toolbar):
        assert _button(toolbar, "Connect").property("primary") is True

        toolbar.set_connected(True)

        assert _button(toolbar, "Disconnect").property("primary") is False

    def test_no_button_text_is_cut_off(self, qapp, toolbar):
        toolbar.set_connected(True)
        toolbar.set_logging(True, paused=True)
        qapp.processEvents()

        for button in toolbar.findChildren(QPushButton):
            if button.isVisible() and button.text():
                assert button.width() >= button.sizeHint().width(), button.text()

    def test_buttons_that_do_not_fit_move_into_the_more_menu(self, qapp, toolbar):
        toolbar.set_connected(True)
        assert toolbar.overflowed_labels() == []

        toolbar.resize(toolbar.minimumSizeHint().width(), toolbar.height())
        qapp.processEvents()
        moved = toolbar.overflowed_labels()

        assert moved and moved[0] == "Plot"
        for button in toolbar.findChildren(QPushButton):
            if button.isVisible() and button.text():
                assert button.width() >= button.sizeHint().width(), button.text()
                assert toolbar.rect().contains(button.geometry()), button.text()

        toolbar.resize(_WIDE, toolbar.height())
        qapp.processEvents()
        assert toolbar.overflowed_labels() == []

    def test_entry_in_the_more_menu_does_what_its_button_does(self, qapp, toolbar):
        toggled: list[bool] = []
        toolbar.plot_toggled.connect(toggled.append)
        toolbar.set_connected(True)
        toolbar.resize(toolbar.minimumSizeHint().width(), toolbar.height())
        qapp.processEvents()

        toolbar._more_menu.actions()[0].trigger()

        assert toggled == [True]

    def test_display_mode_is_one_of_two(self, toolbar):
        chosen: list[DisplayMode] = []
        toolbar.display_mode_changed.connect(chosen.append)
        toolbar.set_connected(True)

        _button(toolbar, "HEX").click()

        assert chosen == [DisplayMode.HEX]
        assert _button(toolbar, "HEX").isChecked() and not _button(toolbar, "Text").isChecked()

        toolbar.set_display_mode(DisplayMode.TEXT)
        assert _button(toolbar, "Text").isChecked()
        assert chosen == [DisplayMode.HEX]

    def test_showing_the_plot_as_open_announces_nothing(self, toolbar):
        toggled: list[bool] = []
        toolbar.plot_toggled.connect(toggled.append)

        toolbar.set_plot_open(True)

        assert _button(toolbar, "Plot").isChecked()
        assert toggled == []


class TestConsoleHeader:
    @pytest.fixture
    def header(self, qapp: QApplication) -> ConsoleHeader:
        widget = ConsoleHeader()
        _show(qapp, widget)
        return widget

    def test_levels_count_the_lines_they_stand_for(self, header):
        header.set_level_counts({"ERROR": 2, "CRITICAL": 1, "INFO": 1200, "DATA": 5, "COMMAND": 1})

        assert header.level_texts() == [
            "All  1,209", "Error  3", "Warning  0", "Info  1,200", "Debug  0", "Data  6",
        ]

    def test_choosing_a_level_announces_its_tags_once(self, header):
        chosen: list[object] = []
        header.levels_changed.connect(chosen.append)

        header.findChildren(QPushButton)[1].click()
        header.findChildren(QPushButton)[1].click()

        assert chosen == [frozenset({"ERROR", "CRITICAL"})]
        assert header.level is LEVEL_CHOICES[1]

    def test_levels_become_one_drop_down_when_there_is_no_room(self, qapp, header):
        assert header.levels_collapsed is False

        header.resize(520, header.height())
        qapp.processEvents()
        assert header.levels_collapsed is True
        assert header._levels_dropdown.text() == "All  0"

        chosen: list[object] = []
        header.levels_changed.connect(chosen.append)
        header._levels_menu.actions()[2].trigger()
        assert chosen == [frozenset({"WARNING"})]
        assert header._levels_dropdown.text() == "Warning  0"

        header.resize(_WIDE, header.height())
        qapp.processEvents()
        assert header.levels_collapsed is False
        assert header.findChildren(QPushButton)[2].isChecked()

    def test_nothing_in_the_row_overlaps_at_any_width(self, qapp, header):
        header.set_level_counts({"ERROR": 1200, "WARNING": 640, "INFO": 52000, "DATA": 9100})
        for width in range(tokens.METRICS.window_min_width, _WIDE + 1, 20):
            header.resize(width, header.height())
            qapp.processEvents()
            shown = sorted(
                (child.geometry() for child in header.findChildren(QWidget)
                 if child.parentWidget() is header and child.isVisible()),
                key=lambda rect: rect.x(),
            )
            for left, right in pairwise(shown):
                assert left.right() < right.x(), width
            assert header.rect().contains(shown[-1]), width

    def test_time_button_switches_between_the_two_modes(self, header):
        modes: list[TimestampMode] = []
        header.timestamp_mode_changed.connect(modes.append)
        button = _button(header, "Time of day")

        button.click()
        assert button.text() == "Since previous line"
        button.click()

        assert modes == [TimestampMode.RELATIVE, TimestampMode.ABSOLUTE]

    def test_following_button_asks_and_shows_the_state_it_is_given(self, header):
        asked: list[bool] = []
        header.follow_requested.connect(asked.append)
        button = _button(header, "Following")

        button.click()
        assert asked == [False]

        header.set_following(False)
        assert button.text() == "Not following" and not button.isChecked()

    def test_filter_text_is_announced_and_can_be_set_and_cleared(self, header):
        texts: list[str] = []
        header.filter_changed.connect(texts.append)

        header.set_filter_text("needle")
        header.clear_filter()

        assert texts == ["needle", ""]
        assert header.filter_text == ""


class TestCommandBar:
    @pytest.fixture
    def bar(self, qapp: QApplication) -> CommandBar:
        widget = CommandBar()
        _show(qapp, widget)
        return widget

    def test_enter_sends_and_clears_the_box(self, bar):
        sent: list[str] = []
        bar.command_sent.connect(sent.append)

        bar.set_text("  AT+RST  ")
        QTest.keyClick(bar.findChild(QLineEdit), Qt.Key.Key_Return)

        assert sent == ["AT+RST"]
        assert bar.text == ""

    def test_empty_command_is_not_sent(self, bar):
        sent: list[str] = []
        bar.command_sent.connect(sent.append)

        bar.set_text("   ")
        _button(bar, "Send").click()

        assert sent == []

    def test_history_holds_each_command_once_newest_first(self, bar):
        for command in ("one", "two", "one"):
            bar.set_text(command)
            _button(bar, "Send").click()

        assert bar.history() == ["one", "two"]

    def test_history_is_limited(self, bar):
        for number in range(COMMAND_HISTORY_LIMIT + 5):
            bar.set_text(f"cmd {number}")
            _button(bar, "Send").click()

        history = bar.history()
        assert len(history) == COMMAND_HISTORY_LIMIT
        assert history[0] == f"cmd {COMMAND_HISTORY_LIMIT + 4}"

    def test_line_ending_starts_at_the_default_and_announces_a_change(self, bar):
        changes: list[LineEnding] = []
        bar.line_ending_changed.connect(changes.append)
        assert bar.line_ending is DEFAULT_LINE_ENDING

        bar.set_line_ending(LineEnding.CRLF)
        bar.set_line_ending(LineEnding.CRLF)

        assert changes == [LineEnding.CRLF]

    def test_every_line_ending_has_a_name(self):
        assert set(LINE_ENDING_NAMES) == set(LineEnding)

    def test_bar_without_a_store_has_no_saved_button(self, bar):
        assert not _button(bar, "Saved").isVisible()


class TestSavedMenu:
    @pytest.fixture
    def store(self, config) -> SavedCommands:
        return SavedCommands(config)

    @pytest.fixture
    def bar(self, qapp: QApplication, store: SavedCommands) -> CommandBar:
        widget = CommandBar(store)
        _show(qapp, widget)
        return widget

    def test_empty_list_offers_saving_and_editing(self, bar):
        assert _button(bar, "Saved").isVisible()
        assert bar.saved_menu_texts() == ["Save the command in the box", "Edit saved commands..."]
        assert not bar._saved_menu.actions()[0].isEnabled()

    def test_command_in_the_box_can_be_saved_once(self, bar, store):
        bar.set_text("get config")
        bar.saved_menu_texts()
        save = bar._saved_menu.actions()[0]
        assert save.isEnabled()

        save.trigger()

        assert store.all() == ["get config"]
        bar.saved_menu_texts()
        assert not bar._saved_menu.actions()[-2].isEnabled()

    def test_choosing_a_command_fills_the_box_and_sends_nothing(self, bar, store):
        sent: list[str] = []
        bar.command_sent.connect(sent.append)
        store.replace(["status", "relay 1 on"])
        texts = bar.saved_menu_texts()

        assert texts[1].startswith("relay 1 on\t")
        bar._saved_menu.actions()[1].trigger()

        assert bar.text == "relay 1 on"
        assert sent == []

    def test_only_the_first_nine_show_a_shortcut(self, bar, store):
        store.replace([f"c{n}" for n in range(1, 13)])

        texts = bar.saved_menu_texts()

        assert all("\t" in text for text in texts[:SAVED_COMMAND_SHORTCUTS])
        assert texts[SAVED_COMMAND_SHORTCUTS:12] == ["c10", "c11", "c12"]

    def test_edit_entry_asks_for_the_dialog(self, bar):
        asked: list[bool] = []
        bar.edit_saved_requested.connect(lambda: asked.append(True))
        bar.saved_menu_texts()

        bar._saved_menu.actions()[-1].trigger()

        assert asked == [True]

    def test_saved_button_follows_the_connection(self, bar):
        bar.set_sending_enabled(False)

        assert not _button(bar, "Saved").isEnabled()


class TestSavedCommandsDialog:
    @pytest.fixture
    def dialog(self, qapp: QApplication) -> SavedCommandsDialog:
        widget = SavedCommandsDialog(["status", "reset", "get config"])
        widget.show()
        qapp.processEvents()
        return widget

    def test_untouched_dialog_returns_the_list(self, dialog):
        assert dialog.commands() == ["status", "reset", "get config"]

    def test_commands_can_be_moved_and_removed(self, dialog):
        dialog._list.setCurrentRow(2)
        _button(dialog, "Move up").click()
        _button(dialog, "Move up").click()
        assert dialog.commands() == ["get config", "status", "reset"]
        assert not _button(dialog, "Move up").isEnabled()

        dialog._list.setCurrentRow(1)
        _button(dialog, "Move down").click()
        _button(dialog, "Remove").click()
        assert dialog.commands() == ["get config", "reset"]

    def test_edited_text_is_cleaned(self, dialog):
        dialog._list.item(0).setText("  status now  ")
        dialog._list.item(1).setText("")
        dialog._list.item(2).setText("status now")

        assert dialog.commands() == ["status now"]

    def test_add_is_offered_until_the_list_is_full(self, qapp):
        dialog = SavedCommandsDialog([f"c{n}" for n in range(SAVED_COMMANDS_MAX - 1)])
        assert _button(dialog, "Add").isEnabled()

        dialog._add_item("last one")

        assert not _button(dialog, "Add").isEnabled()

    def test_nothing_selected_offers_only_add(self, qapp):
        dialog = SavedCommandsDialog([])

        assert _button(dialog, "Add").isEnabled()
        assert not any(
            _button(dialog, text).isEnabled() for text in ("Remove", "Move up", "Move down")
        )


class TestBanner:
    @pytest.fixture
    def area(self, qapp: QApplication) -> BannerArea:
        widget = BannerArea()
        _show(qapp, widget, height=200)
        widget.hide()
        return widget

    def test_area_takes_no_room_without_a_banner(self, qapp, area):
        assert area.isHidden()

        area.show_banner("a", banners.WARNING, "Title")
        assert not area.isHidden()

        area.clear("a")
        assert area.isHidden()

    def test_same_key_replaces_and_another_key_stacks(self, area):
        area.show_banner("a", banners.WARNING, "First")
        area.show_banner("a", banners.ERROR, "Second", "Detail")
        area.show_banner("b", banners.ERROR, "Other")

        assert area.keys() == ["a", "b"]
        assert area.banner("a").title_label.text() == "Second"
        assert area.banner("a").property("level") == banners.ERROR

    def test_action_runs_and_close_dismisses(self, qapp, area):
        ran: list[str] = []
        banner = area.show_banner(
            "a", banners.WARNING, "Title", "Detail",
            (BannerAction("Retry", lambda: ran.append("retry")),),
        )

        banner.action_buttons[0].click()
        assert ran == ["retry"]
        assert area.keys() == ["a"]

        banner.close_button.click()
        assert area.keys() == []

    def test_banner_that_must_stay_has_no_close_button(self, area):
        banner = area.show_banner("a", banners.ERROR, "Title", dismissible=False)

        assert banner.close_button is None

    def test_clearing_an_unknown_key_does_nothing(self, area):
        area.clear("missing")
        area.clear_all()

        assert area.keys() == []

    @pytest.mark.parametrize(
        ("level", "row"), [(banners.WARNING, "warning_row"), (banners.ERROR, "error_row")],
    )
    def test_banner_is_tinted_by_its_level(self, qapp, area, level, row):
        banner = area.show_banner("a", level, "Title", "Detail")
        area.show()
        qapp.processEvents()

        colour = pixel(banner, banner.width() // 2, 2)

        assert colour.name().upper() == getattr(tokens.palette(), row)


class TestToast:
    @pytest.fixture
    def parent(self, qapp: QApplication) -> QWidget:
        widget = QWidget()
        _show(qapp, widget, 900, 600)
        return widget

    def test_parent_has_one_toast_and_a_new_message_replaces_the_old(self, parent):
        first = Toast.info(parent, "one")
        second = Toast.success(parent, "two")

        assert first is second is Toast.of(parent)
        assert second.message == "two"
        assert second.property("level") == "success"

    def test_toast_sits_above_the_bottom_rows_and_inside_its_parent(self, qapp, parent):
        toast = Toast.info(parent, "Exported 84,210 lines: Bench_PORT0_2026-01-02.txt")
        qapp.processEvents()
        metrics = tokens.METRICS

        assert toast.isVisible()
        assert parent.rect().contains(toast.geometry())
        assert toast.geometry().bottom() < (
            parent.height() - metrics.status_height - metrics.toolbar_height
        )
        assert abs(toast.geometry().center().x() - parent.width() // 2) <= 1
        assert toast.findChild(QLabel).heightForWidth(toast.findChild(QLabel).width()) <= (
            toast.height()
        )

    def test_long_message_wraps_instead_of_leaving_the_parent(self, qapp, parent):
        toast = Toast.warning(parent, "The folder does not exist yet: " + "/very/long/path" * 20)
        qapp.processEvents()

        assert toast.width() <= tokens.METRICS.toast_max_width
        assert parent.rect().contains(toast.geometry())
        assert toast.height() > Toast.info(QWidget(parent), "short").height()

    def test_a_message_longer_than_the_room_is_cut_and_kept_whole(self, qapp):
        """The export of a log split into many files, in a small window."""
        parent = QWidget()
        _show(qapp, parent, 900, 400)
        names = " + ".join(f"Export_COM15_2026-10-17_16-00-34_{part:03d}.txt" for part in range(36))
        message = f"Exported 332,476 lines: {names}"
        toast = Toast.success(parent, message, "Show", lambda: None)
        qapp.processEvents()
        metrics = tokens.METRICS
        label = toast.findChild(QLabel)

        assert parent.rect().contains(toast.geometry())
        assert toast.geometry().top() >= metrics.space_3
        assert toast.geometry().bottom() < (
            parent.height() - metrics.status_height - metrics.toolbar_height
        )
        assert label.text().endswith("\N{HORIZONTAL ELLIPSIS}")
        assert label.heightForWidth(label.width()) <= toast.contentsRect().height()
        assert toast.message == message
        assert toast.toolTip() == message

    def test_a_message_that_fits_is_shown_whole_without_a_tooltip(self, qapp, parent):
        message = "Exported 84,210 lines: Bench_PORT0_2026-01-02.txt"
        toast = Toast.info(parent, message)
        qapp.processEvents()

        assert toast.findChild(QLabel).text() == message
        assert toast.toolTip() == ""

    def test_toast_moves_with_its_parent(self, qapp, parent):
        toast = Toast.info(parent, "message")
        before = toast.pos()

        parent.resize(700, 500)
        qapp.processEvents()

        assert toast.pos() != before
        assert parent.rect().contains(toast.geometry())

    def test_button_runs_its_action_once_and_closes_the_toast(self, parent):
        ran: list[bool] = []
        toast = Toast.success(parent, "Saved", "Show", lambda: ran.append(True))
        assert toast.action_label == "Show"

        toast.trigger_action()
        toast.trigger_action()

        assert ran == [True]
        assert not toast.isVisible()

    def test_toast_with_a_button_stays_longer(self, parent):
        plain = Toast.info(parent, "one")
        assert plain._timer.interval() == TOAST_DURATION_MS

        Toast.info(parent, "two", "Show", lambda: None)
        assert plain._timer.interval() == TOAST_WITH_ACTION_DURATION_MS

    def test_message_without_an_action_shows_no_button(self, parent):
        toast = Toast.info(parent, "one", "Show", lambda: None)
        Toast.info(parent, "two")

        assert toast.action_label == ""
        assert not toast.findChild(QPushButton).isVisible()


class TestStatusBar:
    @pytest.fixture
    def bar(self, qapp: QApplication) -> DeviceStatusBar:
        widget = DeviceStatusBar()
        _show(qapp, widget)
        return widget

    def test_bar_starts_idle(self, bar):
        assert bar.texts() == ["No device connected", "", "0 lines/s", "0 lines", "Not logging", "UTF-8"]

    def test_connection_states_are_spelled_out(self, bar):
        bar.set_connecting("PORT0")
        assert bar.texts()[:2] == ["Connecting to PORT0", ""]

        bar.set_connected("PORT0", 115200)
        bar.set_data_rate(1200)
        bar.set_total_lines(84210)
        assert bar.texts()[:4] == ["PORT0", "115200 8N1", "1,200 lines/s", "84,210 lines"]

        bar.set_disconnected("PORT0", "lost at 10:00:00")
        assert bar.texts()[:3] == ["Disconnected", "PORT0, lost at 10:00:00", "0 lines/s"]

    def test_log_shows_file_time_part_and_size(self, bar):
        bar.set_connected("PORT0", 115200)
        bar.set_logging(
            "Bench_PORT0.txt", 3725, LogProgress(2, 1536 * 1024, 10 * 1024 * 1024), paused=False,
        )
        assert bar.texts()[4] == (
            "Logging  ·  Bench_PORT0.txt  ·  01:02:05  ·  file 2, 1.5 MB of 10 MB"
        )

        bar.set_logging("Bench_PORT0.txt", 5, LogProgress(1, 2048, 0), paused=True)
        assert bar.texts()[4] == "Paused  ·  Bench_PORT0.txt  ·  00:00:05  ·  file 1, 2 KB"

        bar.set_logging("Bench_PORT0.txt", 5, None, paused=False)
        assert bar.texts()[4] == "Logging  ·  Bench_PORT0.txt  ·  00:00:05"

    def test_dot_shows_logging_then_returns_to_the_connection(self, qapp, bar):
        dot = bar.findChild(StatusDot)
        bar.set_connected("PORT0", 115200)
        qapp.processEvents()
        centre = dot.width() / 2
        assert pixel(dot, centre, centre).name().upper() == tokens.palette().connected

        bar.set_logging("a.txt", 0, None, paused=False)
        qapp.processEvents()
        assert pixel(dot, centre, centre).name().upper() == tokens.palette().logging

        bar.set_not_logging()
        qapp.processEvents()
        assert pixel(dot, centre, centre).name().upper() == tokens.palette().connected

    def test_long_log_name_is_shortened_in_the_middle_and_kept_in_the_tooltip(self, qapp, bar):
        name = "A_very_long_session_name_" * 12 + ".txt"
        bar.set_logging(name, 0, None, paused=False)
        bar.resize(tokens.METRICS.window_min_width, bar.height())
        qapp.processEvents()
        label = bar.findChild(ElidedLabel)

        assert bar.width() == tokens.METRICS.window_min_width
        assert label.shown_text() != label.text()
        assert "…" in label.shown_text()
        assert label.shown_text().startswith("Logging  \u00b7  A_very_long")
        assert label.shown_text().endswith("00:00:00")
        assert name in label.toolTip()


class TestStatusDot:
    @pytest.mark.parametrize("ratio", [1.0, 1.5, 2.0])
    def test_icon_is_drawn_for_the_scaling_of_the_screen(self, qapp, ratio):
        side = tokens.METRICS.status_dot
        pixmap = state_icon(LinkState.CONNECTED, ratio).pixmap(side, side)

        assert not pixmap.isNull()

    def test_idle_dot_is_an_outline(self, qapp):
        dot = StatusDot()
        _show(qapp, dot, dot.sizeHint().width(), dot.sizeHint().height())
        centre = dot.width() / 2

        assert pixel(dot, centre, centre).alpha() == 0 or (
            pixel(dot, centre, centre).name().upper() not in {
                tokens.palette().connected, tokens.palette().logging,
            }
        )


class TestConnectPage:
    @pytest.fixture
    def page(self, qapp: QApplication) -> ConnectPage:
        notes = {"PORT0": "Last used at 9600, LF"}
        widget = ConnectPage(port_note=lambda device: notes.get(device, ""))
        _show(qapp, widget, 1000, 700)
        return widget

    def test_no_ports_explains_what_to_do_without_naming_a_cause(self, page):
        title, subtitle, help_text = page.texts()

        assert title == "Connect a device"
        assert subtitle == "WireTrace is watching for serial ports."
        assert help_text.startswith("Plug a device in and it appears here by itself.")
        assert not _button(page, "Connect").isEnabled()

    @pytest.mark.parametrize(
        ("platform", "mentioned"), [("linux", True), ("win32", False), ("darwin", False)],
    )
    def test_linux_note_is_shown_on_linux_only(self, qapp, monkeypatch, platform, mentioned):
        monkeypatch.setattr(connect_page_module.sys, "platform", platform)

        help_text = ConnectPage().texts()[2]

        assert ("On Linux" in help_text) is mentioned

    def test_ports_are_listed_and_the_first_is_chosen(self, qapp, page):
        chosen: list[str] = []
        page.port_chosen.connect(chosen.append)

        page.set_ports([_port("PORT0"), _port("PORT1", "", "")], set())
        qapp.processEvents()

        assert page.port_devices() == ["PORT0", "PORT1"]
        assert page.selected_port == "PORT0"
        assert chosen == ["PORT0"]
        assert page.texts()[1].startswith("2 serial ports found.")
        assert _button(page, "Connect").isEnabled()
        texts = [label.text() for label in page.row("PORT0").findChildren(QLabel)]
        assert texts[:3] == ["PORT0", "Adapter", "Last used at 9600, LF"]
        texts = [label.text() for label in page.row("PORT1").findChildren(QLabel)]
        assert texts[:3] == ["PORT1", "Serial port", "Not used before"]

    def test_one_port_is_counted_in_the_singular(self, page):
        page.set_ports([_port("PORT0")], set())

        assert page.texts()[1].startswith("1 serial port found.")

    def test_port_names_share_one_width(self, qapp, page):
        page.set_ports([_port("COM3"), _port("/dev/cu.usbserial-14230")], set())
        qapp.processEvents()

        widths = {page.row(device).name_label.width() for device in page.port_devices()}
        assert len(widths) == 1
        assert widths.pop() >= page.row("/dev/cu.usbserial-14230").name_label.sizeHint().width()

    def test_port_that_appears_later_is_marked_and_chosen(self, qapp, page):
        page.set_ports([_port("PORT0")], set())

        page.set_ports([_port("PORT0"), _port("PORT1")], set())

        assert page.selected_port == "PORT1"
        assert page.row("PORT1").badge.text() == "New"
        assert page.row("PORT1").toolTip() == "This port appeared while this screen was open"
        assert page.row("PORT0").badge.text() == ""
        assert page.row("PORT0").toolTip() == ""

    def test_ports_present_at_the_first_look_are_not_marked(self, page):
        page.set_ports([_port("PORT0")], set())

        assert page.row("PORT0").badge.text() == ""

    def test_port_open_in_another_tab_cannot_be_chosen(self, qapp, page):
        page.set_ports([_port("PORT0"), _port("PORT1")], {"PORT0"})
        qapp.processEvents()
        row = page.row("PORT0")

        assert page.selected_port == "PORT1"
        assert row.badge.text() == "Open in another tab"
        QTest.mouseClick(row, Qt.MouseButton.LeftButton)
        assert page.selected_port == "PORT1"

    def test_removed_port_gives_the_choice_to_another(self, page):
        page.set_ports([_port("PORT0"), _port("PORT1")], set())
        page.row("PORT1").chosen.emit("PORT1")

        page.set_ports([_port("PORT0")], set())
        assert page.selected_port == "PORT0"

        page.set_ports([], set())
        assert page.selected_port == ""
        assert not _button(page, "Connect").isEnabled()

    def test_unchanged_list_is_not_rebuilt(self, page):
        page.set_ports([_port("PORT0")], set())
        row = page.row("PORT0")

        page.set_ports([_port("PORT0")], set())

        assert page.row("PORT0") is row

    def test_click_chooses_and_double_click_connects(self, qapp, page):
        requests: list[tuple[str, int]] = []
        page.connect_requested.connect(lambda port, baud: requests.append((port, baud)))
        page.set_ports([_port("PORT0"), _port("PORT1")], set())
        qapp.processEvents()
        row = page.row("PORT1")

        QTest.mouseClick(row, Qt.MouseButton.LeftButton)
        assert page.selected_port == "PORT1"
        assert row.property("selected") is True
        assert page.row("PORT0").property("selected") is False

        QTest.mouseDClick(row, Qt.MouseButton.LeftButton)
        assert requests[-1] == ("PORT1", DEFAULT_BAUD_RATE)

    def test_arrow_keys_choose_and_enter_connects(self, qapp, page):
        requests: list[tuple[str, int]] = []
        page.connect_requested.connect(lambda port, baud: requests.append((port, baud)))
        page.set_ports([_port("PORT0"), _port("PORT1"), _port("PORT2")], {"PORT1"})
        page.setFocus()

        QTest.keyClick(page, Qt.Key.Key_Down)
        assert page.selected_port == "PORT2"
        QTest.keyClick(page, Qt.Key.Key_Down)
        assert page.selected_port == "PORT2"
        QTest.keyClick(page, Qt.Key.Key_Up)
        assert page.selected_port == "PORT0"

        QTest.keyClick(page, Qt.Key.Key_Return)
        assert requests == [("PORT0", DEFAULT_BAUD_RATE)]

    def test_baud_rate_that_is_not_valid_switches_connect_off(self, page):
        page.set_ports([_port("PORT0")], set())
        field = page.findChild(QLineEdit)

        field.setText("fast")
        assert page.selected_baud is None
        assert field.property("invalid") is True
        assert not _button(page, "Connect").isEnabled()

        page.set_baud(57600)
        assert page.selected_baud == 57600
        assert field.property("invalid") is False
        assert _button(page, "Connect").isEnabled()

    def test_list_scrolls_beyond_the_rows_it_shows(self, qapp, page):
        metrics = tokens.METRICS
        page.set_ports([_port(f"PORT{n}") for n in range(metrics.port_rows_visible + 4)], set())
        qapp.processEvents()

        assert page._scroll.height() == (
            metrics.port_rows_visible * metrics.port_row_height + 2 * metrics.border
        )
        assert page._scroll.verticalScrollBar().maximum() > 0

    def test_page_looks_for_ports_while_shown_and_stops_when_hidden(self, qapp, page):
        wanted: list[bool] = []
        page.ports_wanted.connect(lambda: wanted.append(True))
        assert page._scan_timer.isActive()

        page.hide()
        assert not page._scan_timer.isActive()

        page.show()
        assert page._scan_timer.isActive()
        assert wanted == [True]

    def test_refresh_button_asks_for_the_ports(self, page):
        wanted: list[bool] = []
        page.ports_wanted.connect(lambda: wanted.append(True))

        _button(page, "Refresh").click()

        assert wanted == [True]

    def test_dot_grid_is_drawn_behind_the_page_but_not_in_the_list(self, qapp, page):
        page.set_ports([_port("PORT0")], set())
        qapp.processEvents()
        centre = tokens.METRICS.grid_pitch / 2
        origin = page.mapFrom(page.window(), QPoint(0, 0))

        dot = pixel(page, origin.x() + centre, origin.y() + centre)
        assert dot.name().upper() != tokens.palette().paper

        row = page.row("PORT0")
        colours = {
            pixel(row, x, y).name().upper()
            for x in range(row.width() - 80, row.width() - 4)
            for y in range(4, row.height() - 4, 3)
        }
        assert colours == {tokens.palette().divider}


class TestSearchBar:
    @pytest.fixture
    def console(self, qapp: QApplication) -> ConsoleView:
        from datetime import datetime

        from core.records import LineRecord, RecordOrigin

        view = ConsoleView()
        now = datetime(2026, 1, 2, 3, 4, 5)
        view.append_records([
            LineRecord(now, text, "DATA", RecordOrigin.DEVICE)
            for text in ("alpha one", "beta", "alpha two")
        ])
        _show(qapp, view, 800, 300)
        return view

    @pytest.fixture
    def bar(self, qapp: QApplication, console: ConsoleView) -> SearchBar:
        widget = SearchBar()
        widget.set_console(console)
        return widget

    def test_bar_is_hidden_until_asked_for(self, qapp, bar):
        shown: list[bool] = []
        bar.visibility_changed.connect(shown.append)
        assert bar.isHidden()

        bar.activate()
        bar.deactivate()

        assert shown == [True, False]
        assert bar.isHidden()

    def test_search_for_finds_at_once_and_counts(self, bar, console):
        bar.search_for("alpha")

        assert console.match_count == 2
        assert bar._count_label.text() == "1 of 2"

        bar.find_next()
        assert bar._count_label.text() == "2 of 2"
        bar.find_previous()
        assert bar._count_label.text() == "1 of 2"

    def test_no_match_says_so(self, bar):
        bar.search_for("gamma")

        assert bar._count_label.text() == "No results"

    def test_closing_clears_the_search(self, bar, console):
        bar.search_for("alpha")

        bar.findChild(QPushButton, "flatIconButton").click()

        assert bar.isHidden()
        assert console.match_count == 0
        assert bar.search_text == ""


