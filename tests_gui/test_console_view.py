# SPDX-License-Identifier: GPL-3.0-or-later
"""The console widget, drawn and driven through real Qt."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QAccessible, QColor, QFont, QGuiApplication, QTextLayout
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.constants import (
    CONSOLE_TAB_WIDTH,
    SERIAL_MAX_LINE_BYTES,
    TAG_COMMAND,
    TAG_DATA,
    ThemeID,
    TimestampMode,
)
from core.framing import HexFramer
from core.records import LineRecord, command_record, notice_record
from tests_gui.support import pixel
from ui.themes import tokens
from ui.themes.fonts import register_bundled_fonts
from ui.themes.theme_manager import ThemeManager
from ui.widgets import console_view as console_module
from ui.widgets.console_accessibility import install_console_accessibility
from ui.widgets.console_view import ConsoleView, Position

START = datetime(2026, 10, 2, 22, 1, 50, 935000)
PREFIX_COLS = 19          # 12 time + 2 + 3 level + 2
_TIME_END = 12

#: Lines per second the console must accept on any machine the suite
#: runs on. Measured rates are several times higher; the gate catches a
#: change that makes the cost per line depend on the lines held.
MIN_APPEND_RATE = 50_000
#: A repaint must not depend on the lines held either.
MAX_REPAINT_SECONDS = 0.1


def record(text: str, tag: str = TAG_DATA, offset_ms: int = 0) -> LineRecord:
    return LineRecord(START + timedelta(milliseconds=offset_ms), text, tag)


def numbered(count: int, start: int = 0) -> list[LineRecord]:
    return [record(f"line {n}", offset_ms=n) for n in range(start, start + count)]


@pytest.fixture
def console(qapp: QApplication, theme_manager: ThemeManager) -> ConsoleView:
    register_bundled_fonts()
    theme_manager.apply_theme(ThemeID.LIGHT)
    view = ConsoleView()
    view.resize(900, 300)
    view.show()
    qapp.processEvents()
    return view


def texts(view: ConsoleView) -> list[str]:
    return [r.text for r in view.shown_records()]


def point_at(view: ConsoleView, line: int, col: int) -> QPoint:
    """Viewport point at the left edge of a column of a visible line."""
    x = view._left_margin() + col * view._cell_width + 1
    y = line * view._row_height + view._row_height // 2
    return QPoint(round(x), y)


# ── Rows ─────────────────────────────────────────────────────────────────


class TestRows:
    def test_row_shows_time_level_and_message(self, console):
        console.append_records([record("boot ok", "INFO")])
        assert console.display_lines() == ["22:01:50.935  INF  boot ok"]

    @pytest.mark.parametrize(
        ("tag", "label"),
        [("CRITICAL", "CRT"), ("ERROR", "ERR"), ("WARNING", "WRN"), ("INFO", "INF"),
         ("DEBUG", "DBG"), (TAG_DATA, "   "), (TAG_COMMAND, "   "), ("UNKNOWN", "   ")],
    )
    def test_level_column_per_tag(self, console, tag, label):
        console.append_records([record("x", tag)])
        assert console.display_lines()[0] == f"22:01:50.935  {label}  x"

    def test_message_is_never_altered(self, console):
        raw = "[INFO]  spaced   text \\x1b kept"
        console.append_records([record(raw, "INFO")])
        assert console.shown_records()[0].text == raw
        assert console.display_lines()[0].endswith(raw)

    def test_relative_time_is_measured_from_the_previous_line(self, console):
        console.append_records([record("a"), record("b", offset_ms=150), record("c", offset_ms=1650)])
        console.set_timestamp_mode(TimestampMode.RELATIVE)
        assert [line[:PREFIX_COLS] for line in console.display_lines()] == [
            "    +0.000 s       ", "    +0.150 s       ", "    +1.500 s       ",
        ]

    def test_relative_time_counts_hidden_lines_too(self, console):
        console.append_records([record("keep"), record("drop", offset_ms=100), record("keep", offset_ms=300)])
        console.set_filter("keep")
        console.set_timestamp_mode(TimestampMode.RELATIVE)
        assert console.display_lines()[1].startswith("    +0.200 s")

    def test_time_running_backwards_keeps_its_sign(self, console):
        console.append_records([record("a", offset_ms=500), record("b", offset_ms=400)])
        console.set_timestamp_mode(TimestampMode.RELATIVE)
        assert console.display_lines()[1].startswith("    -0.100 s")

    def test_long_relative_time_widens_only_its_own_row(self, console):
        console.append_records([record("a"), record("b", offset_ms=123_456_789_000)])
        console.set_timestamp_mode(TimestampMode.RELATIVE)
        assert console.display_lines()[1] == "+123456789.000 s       b"

    def test_tab_is_drawn_to_the_next_tab_stop(self, console):
        console.append_records([record("a\tbb\tc")])
        gap = " " * (CONSOLE_TAB_WIDTH - 1)
        assert console.display_lines()[0].endswith(f"a{gap}bb{' ' * (CONSOLE_TAB_WIDTH - 2)}c")

    def test_empty_batch_changes_nothing(self, console):
        counts: list[int] = []
        console.line_count_changed.connect(counts.append)
        console.append_records([])
        assert counts == [] and console.total_count == 0


class TestStore:
    def test_oldest_lines_leave_when_the_limit_is_reached(self, console):
        console.set_max_lines(100)
        console.append_records(numbered(250))
        assert console.total_count == console.visible_count == 100
        assert texts(console) == [f"line {n}" for n in range(150, 250)]

    def test_one_batch_larger_than_the_limit(self, console):
        console.set_max_lines(10)
        console.append_records(numbered(1_000))
        assert texts(console) == [f"line {n}" for n in range(990, 1_000)]

    def test_filtered_rows_stay_in_step_with_the_store(self, console):
        console.set_max_lines(100)
        console.set_filter("even")
        for start in range(0, 500, 50):
            console.append_records([
                record(f"{'even' if n % 2 == 0 else 'odd'} {n}") for n in range(start, start + 50)
            ])
        assert texts(console) == [f"even {n}" for n in range(400, 500, 2)]
        console.set_filter("")
        assert console.visible_count == 100

    def test_lowering_the_limit_keeps_the_newest(self, console):
        console.append_records(numbered(50))
        console.set_max_lines(10)
        assert texts(console) == [f"line {n}" for n in range(40, 50)]
        assert sum(console.level_counts().values()) == 10

    def test_oldest_lines_leave_when_the_characters_held_reach_their_bound(
        self, console, monkeypatch,
    ):
        monkeypatch.setattr(console_module, "CONSOLE_MAX_CHARACTERS", 1_000)
        console.set_max_lines(100_000)

        console.append_records([record(f"{n:03d}" + "x" * 97, offset_ms=n) for n in range(25)])

        assert console.total_count == console.visible_count == 10
        assert [text[:3] for text in texts(console)] == [f"{n:03d}" for n in range(15, 25)]
        assert sum(console.level_counts().values()) == 10

    def test_one_line_longer_than_the_bound_is_still_shown(self, console, monkeypatch):
        monkeypatch.setattr(console_module, "CONSOLE_MAX_CHARACTERS", 1_000)
        console.append_records(numbered(5))

        console.append_records([record("y" * 5_000)])

        assert texts(console) == ["y" * 5_000]

    def test_clearing_starts_the_count_of_characters_again(self, console, monkeypatch):
        monkeypatch.setattr(console_module, "CONSOLE_MAX_CHARACTERS", 1_000)
        console.append_records([record("x" * 900)])
        console.clear_console()

        console.append_records([record("a" * 400), record("b" * 400)])

        assert console.total_count == 2

    @pytest.mark.parametrize("limit", [0, -5])
    def test_invalid_limit_is_ignored(self, console, limit):
        console.append_records(numbered(5))
        console.set_max_lines(limit)
        assert console.total_count == 5

    def test_clear_removes_everything(self, console):
        console.append_records(numbered(20))
        console.select_all()
        console.set_search("line")
        console.clear_console()
        assert console.total_count == console.visible_count == console.match_count == 0
        assert not console.has_selection
        assert console.level_counts() == {}
        assert console.last_timestamp is None

    def test_last_timestamp_is_the_newest_line(self, console):
        console.append_records([record("a"), record("b", offset_ms=40)])
        assert console.last_timestamp == START + timedelta(milliseconds=40)


class TestFilters:
    def test_text_filter_ignores_letter_case_and_outer_spaces(self, console):
        console.append_records([record("Alpha"), record("beta"), record("ALPHABET")])
        console.set_filter("  alpha ")
        assert texts(console) == ["Alpha", "ALPHABET"]
        assert console.filter_text == "alpha"

    def test_level_filter(self, console):
        console.append_records([record("e", "ERROR"), record("d"), record("w", "WARNING")])
        console.set_levels(["ERROR", "WARNING"])
        assert texts(console) == ["e", "w"]
        console.set_levels([])
        assert texts(console) == []
        console.set_levels(None)
        assert console.visible_count == 3

    def test_filters_combine(self, console):
        console.append_records([record("x one", "ERROR"), record("y one", "ERROR"), record("x two")])
        console.set_levels(["ERROR"])
        console.set_filter("x")
        assert texts(console) == ["x one"]

    def test_notice_is_shown_whatever_the_filters(self, console):
        console.append_records([record("data"), notice_record("12 lines were skipped", START)])
        console.set_filter("nothing matches this")
        console.set_levels([])
        console.set_hide_blank_lines(True)
        assert len(texts(console)) == 1 and "12 lines were skipped" in texts(console)[0]

    def test_blank_lines_can_be_hidden(self, console):
        console.append_records([record("a"), record(""), record("   "), record("b")])
        console.set_hide_blank_lines(True)
        assert texts(console) == ["a", "b"]
        assert console.total_count == 4

    def test_level_counts_cover_held_lines_and_skip_notices(self, console):
        console.set_max_lines(4)
        console.append_records([
            record("e", "ERROR"), record("e", "ERROR"), record("d"),
            notice_record("note", START), record("i", "INFO"),
        ])
        assert console.level_counts() == {"ERROR": 1, TAG_DATA: 1, "INFO": 1}

    def test_command_echo_is_a_row(self, console):
        console.append_records([command_record("status", START)])
        assert console.display_lines() == ["22:01:50.935       >>> status"]


# ── Following ────────────────────────────────────────────────────────────


class TestFollowing:
    def test_view_follows_the_newest_line(self, qapp, console):
        console.append_records(numbered(500))
        bar = console.verticalScrollBar()
        assert console.following and bar.value() == bar.maximum() > 0

    def test_scrolling_up_stops_following_and_counts_new_lines(self, qapp, console):
        console.append_records(numbered(500))
        states: list[bool] = []
        unseen: list[int] = []
        console.following_changed.connect(states.append)
        console.unseen_lines_changed.connect(unseen.append)

        bar = console.verticalScrollBar()
        bar.setValue(100)
        console.append_records(numbered(30, start=500))
        console.append_records(numbered(12, start=530))

        assert states == [False]
        assert bar.value() == 100
        assert unseen == [30, 42] and console.unseen_lines == 42

        console.follow()
        assert states == [False, True]
        assert unseen[-1] == 0 and bar.value() == bar.maximum()

    def test_scrolling_back_to_the_end_resumes_following(self, console):
        console.append_records(numbered(500))
        bar = console.verticalScrollBar()
        bar.setValue(10)
        assert not console.following
        bar.setValue(bar.maximum())
        assert console.following

    def test_view_stays_on_its_rows_while_older_rows_leave(self, console):
        console.set_max_lines(300)
        console.append_records(numbered(300))
        bar = console.verticalScrollBar()
        bar.setValue(200)
        top = console.shown_records()[bar.value()].text

        console.append_records(numbered(50, start=300))

        assert console.shown_records()[bar.value()].text == top

    def test_rows_scrolled_out_of_the_store_leave_the_view_at_the_top(self, console):
        console.set_max_lines(300)
        console.append_records(numbered(300))
        console.verticalScrollBar().setValue(20)
        console.append_records(numbered(200, start=300))
        assert console.verticalScrollBar().value() == 0
        assert not console.following

    def test_auto_follow_can_be_switched_off(self, console):
        console.set_auto_follow(False)
        console.append_records(numbered(500))
        assert not console.following
        assert console.verticalScrollBar().value() == 0
        console.set_auto_follow(True)
        assert console.following

    def test_resizing_keeps_following(self, qapp, console):
        console.append_records(numbered(500))
        console.resize(900, 520)
        qapp.processEvents()
        bar = console.verticalScrollBar()
        assert console.following and bar.value() == bar.maximum()


# ── Selection ────────────────────────────────────────────────────────────


class TestSelection:
    @pytest.fixture
    def filled(self, console):
        console.append_records([
            record("alpha one"), record("beta\ttwo", offset_ms=10), record("gamma three", offset_ms=20),
        ])
        return console

    def select(self, view, start: tuple[int, int], end: tuple[int, int]) -> None:
        view._set_selection(
            Position(view._row_base + start[0], start[1]),
            Position(view._row_base + end[0], end[1]),
        )

    def test_nothing_is_selected_at_first(self, filled):
        assert not filled.has_selection and filled.selected_text() == ""

    def test_selection_inside_one_row(self, filled):
        self.select(filled, (0, PREFIX_COLS), (0, PREFIX_COLS + 5))
        assert filled.selected_text() == "alpha"

    def test_selection_across_rows_is_as_shown(self, filled):
        self.select(filled, (0, PREFIX_COLS + 6), (2, PREFIX_COLS + 5))
        assert filled.selected_text() == "one\n22:01:50.945       beta\ttwo\n22:01:50.955       gamma"

    def test_selection_made_backwards_is_the_same(self, filled):
        self.select(filled, (2, PREFIX_COLS + 5), (0, PREFIX_COLS + 6))
        assert filled.selected_text().startswith("one\n")

    def test_messages_only(self, filled):
        self.select(filled, (0, 3), (2, PREFIX_COLS + 5))
        assert filled.selected_messages() == "alpha one\nbeta\ttwo\ngamma"

    def test_selection_ending_in_the_time_column_has_no_message(self, filled):
        self.select(filled, (0, PREFIX_COLS), (1, 5))
        assert filled.selected_text() == "alpha one\n22:01"
        assert filled.selected_messages() == "alpha one\n"

    @pytest.mark.parametrize(
        ("first", "last", "expected"),
        [(0, 4, "beta"), (0, 8, "beta\t"), (4, 8, "\t"), (5, 9, "\tt"), (0, 6, "beta"),
         (8, 11, "two"), (0, 99, "beta\ttwo")],
    )
    def test_tab_is_copied_as_a_tab(self, filled, first, last, expected):
        self.select(filled, (1, PREFIX_COLS + first), (1, PREFIX_COLS + last))
        assert filled.selected_text() == expected

    def test_select_all(self, filled):
        filled.select_all()
        assert filled.selected_text() == "\n".join(
            line.replace("beta    two", "beta\ttwo") for line in filled.display_lines()
        )

    def test_select_all_with_no_rows(self, console):
        console.select_all()
        assert not console.has_selection

    def test_copy_puts_the_text_on_the_clipboard(self, filled):
        self.select(filled, (0, PREFIX_COLS), (0, PREFIX_COLS + 9))
        filled.copy()
        assert QGuiApplication.clipboard().text() == "alpha one"
        self.select(filled, (0, 0), (0, PREFIX_COLS + 9))
        filled.copy_messages()
        assert QGuiApplication.clipboard().text() == "alpha one"

    def test_copy_without_a_selection_leaves_the_clipboard(self, filled):
        QGuiApplication.clipboard().setText("kept")
        filled.copy()
        assert QGuiApplication.clipboard().text() == "kept"

    def test_selection_follows_its_rows_as_older_rows_leave(self, console):
        console.set_max_lines(10)
        console.append_records(numbered(10))
        self.select(console, (8, PREFIX_COLS), (9, PREFIX_COLS + 6))
        console.append_records(numbered(5, start=10))
        assert console.selected_text() == "line 8\n22:01:50.944       line 9"

    def test_selection_is_cut_at_the_first_row_still_held(self, console):
        console.set_max_lines(10)
        console.append_records(numbered(10))
        self.select(console, (0, PREFIX_COLS), (9, PREFIX_COLS + 6))
        console.append_records(numbered(5, start=10))
        assert console.selected_messages().split("\n") == [f"line {n}" for n in range(5, 10)]

    def test_selection_is_dropped_when_all_its_rows_leave(self, console):
        console.set_max_lines(10)
        console.append_records(numbered(10))
        self.select(console, (0, 0), (1, 5))
        changes: list[None] = []
        console.selection_changed.connect(lambda: changes.append(None))
        console.append_records(numbered(10, start=10))
        assert not console.has_selection and changes

    def test_changing_the_rows_drops_the_selection(self, filled):
        self.select(filled, (0, 0), (1, 5))
        filled.set_filter("alpha")
        assert not filled.has_selection

    def test_drag_selects_text(self, qapp, filled):
        viewport = filled.viewport()
        QTest.mousePress(viewport, Qt.MouseButton.LeftButton, pos=point_at(filled, 0, PREFIX_COLS))
        QTest.mouseMove(viewport, point_at(filled, 2, PREFIX_COLS + 5))
        QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=point_at(filled, 2, PREFIX_COLS + 5))
        assert filled.selected_messages() == "alpha one\nbeta\ttwo\ngamma"

    def test_click_clears_the_selection(self, qapp, filled):
        filled.select_all()
        QTest.mouseClick(filled.viewport(), Qt.MouseButton.LeftButton, pos=point_at(filled, 1, 3))
        assert not filled.has_selection

    def test_shift_click_extends_the_selection(self, qapp, filled):
        viewport = filled.viewport()
        QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=point_at(filled, 0, PREFIX_COLS))
        QTest.mouseClick(
            viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier,
            point_at(filled, 0, PREFIX_COLS + 5),
        )
        assert filled.selected_text() == "alpha"

    def test_double_click_selects_a_word(self, qapp, filled):
        QTest.mouseDClick(
            filled.viewport(), Qt.MouseButton.LeftButton, pos=point_at(filled, 2, PREFIX_COLS + 8),
        )
        assert filled.selected_text() == "three"

    def test_click_below_the_last_row_goes_to_its_end(self, qapp, filled):
        viewport = filled.viewport()
        QTest.mousePress(viewport, Qt.MouseButton.LeftButton, pos=point_at(filled, 2, PREFIX_COLS))
        QTest.mouseMove(viewport, QPoint(5, viewport.height() - 2))
        QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=QPoint(5, viewport.height() - 2))
        assert filled.selected_text() == "gamma three"

    def test_keyboard_extends_the_selection(self, qapp, filled):
        filled.setFocus()
        QTest.mouseClick(filled.viewport(), Qt.MouseButton.LeftButton, pos=point_at(filled, 0, PREFIX_COLS))
        for _ in range(5):
            QTest.keyClick(filled, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
        assert filled.selected_text() == "alpha"
        QTest.keyClick(filled, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
        # The cursor lands inside the space the tab takes up, which
        # belongs to the tab: it is not part of the selection yet.
        assert filled.selected_messages() == "alpha one\nbeta"
        QTest.keyClick(filled, Qt.Key.Key_End, Qt.KeyboardModifier.ShiftModifier)
        assert filled.selected_messages() == "alpha one\nbeta\ttwo\ngamma three"
        QTest.keyClick(filled, Qt.Key.Key_Home, Qt.KeyboardModifier.ShiftModifier)
        assert filled.selected_text() == "22:01:50.935       "

    def test_select_all_and_copy_shortcuts(self, qapp, filled):
        filled.setFocus()
        QTest.keyClick(filled, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(filled, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        assert QGuiApplication.clipboard().text().count("\n") == 2

    def test_navigation_keys_scroll(self, qapp, console):
        console.append_records(numbered(500))
        console.setFocus()
        bar = console.verticalScrollBar()
        QTest.keyClick(console, Qt.Key.Key_Home)
        assert bar.value() == 0 and not console.following
        QTest.keyClick(console, Qt.Key.Key_Down)
        assert bar.value() == 1
        QTest.keyClick(console, Qt.Key.Key_PageDown)
        assert bar.value() == 1 + bar.pageStep()
        QTest.keyClick(console, Qt.Key.Key_Up)
        QTest.keyClick(console, Qt.Key.Key_PageUp)
        assert bar.value() == 0
        QTest.keyClick(console, Qt.Key.Key_End)
        assert bar.value() == bar.maximum() and console.following


# ── Search ───────────────────────────────────────────────────────────────


class TestSearch:
    @pytest.fixture
    def filled(self, console):
        console.append_records([
            record("Alpha one", "INFO"), record("beta", offset_ms=10),
            record("alpha ALPHA", offset_ms=20), record("gamma", "ERROR", offset_ms=30),
        ])
        return console

    def test_every_occurrence_is_found_whatever_the_letter_case(self, filled):
        filled.set_search("alpha")
        assert filled.match_count == 3 and filled.current_match == 0
        assert list(filled._matches) == [
            (filled._row_base, PREFIX_COLS, 5),
            (filled._row_base + 2, PREFIX_COLS, 5),
            (filled._row_base + 2, PREFIX_COLS + 6, 5),
        ]

    def test_navigation_wraps_both_ways(self, filled):
        filled.set_search("alpha")
        filled.search_previous()
        assert filled.current_match == 2
        filled.search_next()
        assert filled.current_match == 0

    def test_search_reports_count_and_position(self, filled):
        seen: list[tuple[int, int]] = []
        filled.search_changed.connect(lambda count, index: seen.append((count, index)))
        filled.set_search("alpha")
        filled.search_next()
        assert seen[-1] == (3, 1)
        filled.set_search("")
        assert seen[-1] == (0, -1)

    def test_no_match(self, filled):
        filled.set_search("zzz")
        assert filled.match_count == 0 and filled.current_match == -1
        filled.search_next()
        filled.search_previous()
        assert filled.current_match == -1

    def test_special_characters_are_searched_literally(self, console):
        console.append_records([record("a.c (x) [1] a+c"), record("abc")])
        console.set_search("a.c")
        assert console.match_count == 1
        console.set_search("(x) [1]")
        assert console.match_count == 1

    @pytest.mark.parametrize(
        ("needle", "count"),
        [("22:01", 4), ("50.9", 4), ("inf", 1), ("ERR", 1), ("err  gam", 1), ("  ", 10), ("f  a", 1)],
    )
    def test_time_and_level_columns_are_searched(self, filled, needle, count):
        filled.set_search(needle)
        assert filled.match_count == count

    def test_fast_and_full_search_agree(self, console):
        """Skipping the time and level columns must never lose a match."""
        tags = ["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", TAG_DATA, TAG_COMMAND]
        console.append_records([
            record(f"crt err wrn inf dbg s {n} tab\there sS", tags[n % len(tags)], offset_ms=n * 37)
            for n in range(200)
        ])
        for mode in (TimestampMode.ABSOLUTE, TimestampMode.RELATIVE):
            console.set_timestamp_mode(mode)
            lines = console.display_lines()
            for needle in ("c", "rt", "t  ", "s", "s  ", "g", "f", "n", "e", "w", "d", "b", "i", "x", "here", "1"):
                console.set_search(needle)
                expected = sum(line.lower().count(needle) for line in lines)
                assert console.match_count == expected, (mode, needle)

    def test_matches_are_found_in_new_lines(self, filled):
        filled.set_search("alpha")
        filled.append_records([record("more alpha", offset_ms=40)])
        assert filled.match_count == 4 and filled.current_match == 0

    def test_matches_leave_with_their_rows(self, console):
        console.set_max_lines(4)
        console.append_records([record("hit"), record("hit"), record("miss"), record("hit")])
        console.set_search("hit")
        console.search_next()
        console.search_next()
        assert console.current_match == 2

        console.append_records([record("miss"), record("miss")])
        assert console.match_count == 1 and console.current_match == 0

        console.append_records([record("miss")] * 4)
        assert console.match_count == 0 and console.current_match == -1

    def test_search_is_run_again_when_the_rows_change(self, filled):
        filled.set_search("alpha")
        filled.set_filter("one")
        assert filled.match_count == 1
        filled.set_timestamp_mode(TimestampMode.RELATIVE)
        assert filled.match_count == 1

    def test_match_out_of_view_is_scrolled_to(self, qapp, console):
        console.append_records([*numbered(400), record("needle " + "x" * 300 + " needle")])
        console.verticalScrollBar().setValue(0)
        console.set_search("needle")
        bar = console.verticalScrollBar()
        assert bar.value() + bar.pageStep() >= 401
        console.search_next()
        assert console.horizontalScrollBar().value() > 0

    def test_matches_stop_at_the_limit(self, console, monkeypatch):
        monkeypatch.setattr(console_module, "CONSOLE_SEARCH_MATCH_LIMIT", 25)
        console.append_records([record("aaaa")] * 20)
        console.set_search("a")
        assert console.match_count == 25 and console.match_limit_reached
        console.append_records([record("aaaa")])
        assert console.match_count == 25


# ── Geometry and drawing ─────────────────────────────────────────────────────


class TestGeometry:
    def shaped_x(self, view: ConsoleView, text: str, col: int) -> float:
        layout = QTextLayout(text, view.font())
        layout.beginLayout()
        line = layout.createLine()
        line.setLineWidth(1_000_000.0)
        layout.endLayout()
        return line.cursorToX(col)[0]

    @pytest.mark.parametrize("size", [8, 9, 10, 11, 12, 13, 14, 16, 20, 32])
    def test_cell_arithmetic_agrees_with_text_shaping(self, console, size):
        """The fast drawing path places a character by counting cells.

        That is only right if it lands where Qt's own shaping puts it.
        """
        console.set_font_size(size)
        assert console._fixed_pitch
        text = "22:01:50.935  ERR  The quick brown fox, 0123456789 {}[]()<>|\\/_-+=~`!@#$%^&*;:'\","
        for col in (0, 1, 13, 19, 40, len(text)):
            assert console._x_of(0, None, text, col) == pytest.approx(
                self.shaped_x(console, text, col), abs=0.5
            ), (size, col)

    def test_proportional_font_takes_the_shaped_path(self, qapp, console):
        proportional = QFont(qapp.font().family())
        proportional.setFixedPitch(False)
        console.setFont(proportional)
        console._apply_font()
        if console._fixed_pitch:
            pytest.skip("this system has no proportional font to test with")
        console.append_records([record("iiii"), record("MMMM")])
        qapp.processEvents()
        narrow = console._x_of(0, console._entry(0), console.display_lines()[0], PREFIX_COLS + 4)
        wide = console._x_of(1, console._entry(1), console.display_lines()[1], PREFIX_COLS + 4)
        assert narrow < wide

    @pytest.mark.parametrize(
        "text",
        ["café naïve", "中文字 wide", "محمد right to left", "é combining", "\U0001f600 emoji"],
    )
    def test_text_beyond_ascii_can_be_clicked_and_selected(self, qapp, console, text):
        console.append_records([record(text)])
        qapp.processEvents()
        row = console._row_base
        entry = console._entry(row)
        line = console.display_lines()[0]
        assert console._needs_layout(line)

        # The time column is on the left whatever script the message is in.
        assert console._x_of(row, entry, line, _TIME_END) == pytest.approx(
            _TIME_END * console._cell_width, abs=0.5
        )
        assert console._layouts[row].lineAt(0).naturalTextWidth() > PREFIX_COLS * console._cell_width
        position = console._position_at(QPoint(round(console._left_margin() + 3 * console._cell_width + 1), 5))
        assert position == Position(row, 3)

        console.select_all()
        assert console.selected_messages() == text
        console.viewport().grab()

    def test_point_maps_to_the_nearest_column(self, qapp, console):
        console.append_records([record("abcdef")])
        cell = console._cell_width
        margin = console._left_margin()
        row = console._row_base
        assert console._position_at(QPoint(0, 5)) == Position(row, 0)
        assert console._position_at(QPoint(round(margin + 2.4 * cell), 5)) == Position(row, 2)
        assert console._position_at(QPoint(round(margin + 2.6 * cell), 5)) == Position(row, 3)
        assert console._position_at(QPoint(5_000, 5)) == Position(row, PREFIX_COLS + 6)

    def test_point_with_no_rows_maps_to_nothing(self, console):
        assert console._position_at(QPoint(10, 10)) is None

    def test_horizontal_range_covers_the_longest_row(self, qapp, console):
        console.append_records([record("short"), record("x" * 500)])
        qapp.processEvents()
        bar = console.horizontalScrollBar()
        needed = (PREFIX_COLS + 500) * console._cell_width + 2 * console._left_margin()
        assert bar.maximum() == pytest.approx(needed - console.viewport().width(), abs=1.5)
        console.clear_console()
        assert bar.maximum() == 0

    def test_font_size_changes_the_row_height(self, console):
        console.set_font_size(10)
        small = console._row_height
        console.set_font_size(20)
        assert console._row_height > small
        assert console.font().pixelSize() == 20


class TestDrawing:
    def background(self, view: ConsoleView, line: int) -> QColor:
        return pixel(view.viewport(), view.viewport().width() - 3, line * view._row_height + 2)

    def test_error_and_warning_rows_are_tinted(self, qapp, console):
        console.append_records([record("plain"), record("bad", "ERROR"), record("careful", "WARNING")])
        qapp.processEvents()
        palette = tokens.palette()
        assert self.background(console, 0) == QColor(palette.paper)
        assert self.background(console, 1) == QColor(palette.error_row)
        assert self.background(console, 2) == QColor(palette.warning_row)

    def test_a_hex_rows_structure_is_drawn_quieter_than_its_bytes(self, qapp, console):
        row = HexFramer().feed(b"\r\nRPM: 1697, Cur")[0]
        hex_record = LineRecord(START, row, TAG_DATA, hex_row=True)
        console.append_records([hex_record])
        qapp.processEvents()
        entry = console._entry(console._last_row())
        text = console._row_text(entry)
        prefix = len(text) - len(row)
        palette = tokens.palette()
        colour_at = {
            col: colour.name()
            for start, end, colour in console._spans(entry, text, prefix)
            for col in range(start, end)
        }
        bar = row.index("|")

        assert colour_at[prefix] == QColor(palette.text_secondary).name()
        assert colour_at[prefix + row.index("52")] == QColor(palette.text).name()
        assert colour_at[prefix + bar + 1] == QColor(palette.text_secondary).name()
        assert colour_at[prefix + bar + 3] == QColor(palette.text).name()
        assert sorted(colour_at) == list(range(len(text)))

    def test_a_text_line_that_looks_like_a_hex_row_is_drawn_as_text(self, qapp, console):
        row = HexFramer().feed(b"0123456789abcdef")[0]
        console.append_records([record(row)])
        qapp.processEvents()
        entry = console._entry(console._last_row())
        text = console._row_text(entry)
        prefix = len(text) - len(row)

        assert {colour.name() for start, _, colour in console._spans(entry, text, prefix)
                if start >= prefix} == {QColor(tokens.palette().text).name()}

    def test_colour_coding_can_be_switched_off(self, qapp, console):
        console.append_records([record("bad", "ERROR")])
        console.set_severity_colours(False)
        qapp.processEvents()
        assert self.background(console, 0) == QColor(tokens.palette().paper)

    def test_selection_and_matches_are_drawn(self, qapp, console):
        console.append_records([record("find me here")])
        palette = tokens.palette()
        x = round(console._left_margin() + (PREFIX_COLS + 5.5) * console._cell_width)

        console.set_search("me")
        qapp.processEvents()
        assert pixel(console.viewport(), x, 2) == QColor(palette.text)

        console.set_search("")
        console.select_all()
        qapp.processEvents()
        assert pixel(console.viewport(), x, 2) == QColor(palette.selection)

    def test_longest_possible_line_is_drawn_without_delay(self, qapp, console):
        console.append_records([record("x" * SERIAL_MAX_LINE_BYTES) for _ in range(40)])
        qapp.processEvents()
        started = time.perf_counter()
        console.viewport().grab()
        assert time.perf_counter() - started < MAX_REPAINT_SECONDS

    def test_theme_change_is_drawn(self, qapp, console, theme_manager):
        console.append_records([record("中 shaped row"), record("plain row")])
        qapp.processEvents()
        theme_manager.apply_theme(ThemeID.DARK)
        qapp.processEvents()
        assert self.background(console, 0) == QColor(tokens.DARK.paper)


# ── Cost ─────────────────────────────────────────────────────────────────


def test_cost_per_line_does_not_grow_with_the_lines_held(qapp, console):
    """Append and repaint must stay fast with the store full."""
    console.set_max_lines(100_000)
    lines = [
        record(f"Temperature: {n % 40}.5, Humidity: 65.{n % 10}, n={n}", offset_ms=n)
        for n in range(200_000)
    ]
    started = time.perf_counter()
    for start in range(0, len(lines), 500):
        console.append_records(lines[start:start + 500])
    elapsed = time.perf_counter() - started

    assert console.total_count == 100_000
    assert len(lines) / elapsed >= MIN_APPEND_RATE, f"{len(lines) / elapsed:,.0f} lines/s"

    qapp.processEvents()
    started = time.perf_counter()
    console.viewport().grab()
    assert time.perf_counter() - started < MAX_REPAINT_SECONDS


# ── Assistive technology ─────────────────────────────────────────────────────


def test_console_describes_itself_to_assistive_technology(qapp, console):
    install_console_accessibility()
    install_console_accessibility()
    console.append_records(numbered(500))
    qapp.processEvents()

    interface = QAccessible.queryAccessibleInterface(console)
    assert interface.role() == QAccessible.Role.Terminal
    assert interface.text(QAccessible.Text.Name) == "Console output"

    spoken = interface.text(QAccessible.Text.Value).split("\n")
    assert spoken == console.visible_lines()
    assert spoken[-1].endswith("line 499")
    assert len(spoken) <= console.verticalScrollBar().pageStep() + 1
    assert QAccessible.queryAccessibleInterface(console) is interface
