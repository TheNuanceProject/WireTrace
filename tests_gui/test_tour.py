# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests of the tour with real Qt: the sample board, every step of every
chapter on the real window, what the tour opens and puts away, the
offer at start, and the list of chapters."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QAbstractAnimation, QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QAction, QKeyEvent, QKeySequence, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMenu, QPushButton

import ui.main_window as main_window_module
import ui.tour.tour as tour_module
from app.constants import SAMPLE_LINE_INTERVAL_MS, SAMPLE_PORT_NAME, ThemeID
from app.modules import ALERTS, MODULES, PLOT, ModuleSettings
from core.reset_detector import ResetDetector
from core.sample_device import _START, open_sample_transport, sample_lines
from core.tag_detector import TagDetector
from tests_gui.support import LoopbackPort, wait_until
from ui import shortcuts
from ui.dialogs import preference_pages as pages
from ui.dialogs.preferences_dialog import PreferencesDialog
from ui.dialogs.session_report_dialog import SessionReportDialog
from ui.main_window import MainWindow
from ui.themes import tokens
from ui.themes.theme_manager import ThemeManager
from ui.tour.chapters import (
    CHAPTERS,
    SAMPLE_SESSION,
    WHATS_NEW,
    Chapter,
    Place,
    page_hidden,
    switched_off,
    words,
)
from ui.tour.menu_opener import menu_path, where_words
from ui.tour.overlay import CardAction, TourOverlay
from ui.tour.targets import FEATURE_TARGETS, Target, find, find_action
from ui.tour.tour_dialog import TourDialog, chapter_hint
from version import APP_NAME

NEWS_LINE = next(iter(WHATS_NEW))
# A release of that line.
NEWS_VERSION = f"{NEWS_LINE}.0"


class _Unchosen:
    """A list of chapters from which nothing is chosen."""

    def __init__(self) -> None:
        self.chosen = SimpleNamespace(connect=lambda slot: None)

    def exec(self) -> int:
        return 0


class _Listing:
    """A stand-in for the list of chapters: keeps the chapters it is
    given, and chooses none."""

    def __init__(self, listed: list[tuple]) -> None:
        self._listed = listed

    def __call__(self, chapters, *_args) -> _Unchosen:
        self._listed.append(tuple(chapters))
        return _Unchosen()


@pytest.fixture
def light(qapp) -> ThemeManager:
    themes = ThemeManager(qapp)
    themes.apply_theme(ThemeID.LIGHT)
    return themes


@pytest.fixture(autouse=True)
def motion_as_usual(monkeypatch):
    """The system's motion setting is not read from the computer running
    the tests."""
    monkeypatch.setattr(tour_module, "reduced_motion", lambda: False)
    monkeypatch.setattr(main_window_module, "reduced_motion", lambda: False)


@pytest.fixture
def window(qapp, config, light):
    main_window = MainWindow(config, light, transport_factory=LoopbackPort().factory)
    main_window.resize(1600, 900)
    main_window.show()
    qapp.processEvents()
    yield main_window
    if main_window._tour is not None:
        main_window._tour.stop()
    main_window.shutdown_all_tabs()


def _shown(qapp: QApplication, window: MainWindow) -> TourOverlay:
    """The overlay of the step shown, once it is."""
    wait_until(qapp, lambda: window._tour is not None and window._tour._overlay is not None)
    return window._tour._overlay


def _card_texts(overlay: TourOverlay) -> tuple[str, str]:
    return overlay.card._title.text(), overlay.card._text.text()


def _button(overlay: TourOverlay, text: str) -> QPushButton:
    return next(b for b in overlay.card.findChildren(QPushButton) if b.text() == text and b.isVisible())


def _chapter(chapter_id: str) -> Chapter:
    return next(chapter for chapter in CHAPTERS if chapter.id == chapter_id)


def _every_feature_on(config) -> None:
    features = ModuleSettings(config)
    for spec in MODULES:
        features.set_enabled(spec, True)


def _next(qapp: QApplication, window: MainWindow) -> None:
    window._tour._overlay.card._default.click()
    qapp.processEvents()


def _walk_to(qapp: QApplication, window: MainWindow, title: str) -> TourOverlay:
    """Go on through the tour under way to the card called ``title``."""
    while True:
        overlay = _shown(qapp, window)
        if overlay.card._title.text() == title:
            return overlay
        _next(qapp, window)


def _key(widget, key: Qt.Key) -> None:
    QApplication.sendEvent(widget, QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier))


def _press_and_release(widget, point: QPoint) -> None:
    """A click at ``point`` on the screen, as ``widget`` (an open menu) gets it."""
    local = QPointF(widget.mapFromGlobal(point))
    for kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
        QApplication.sendEvent(widget, QMouseEvent(
            kind, local, QPointF(point), Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton if kind == QEvent.Type.MouseButtonPress else Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        ))


def _all_steps() -> list:
    return [step for chapter in (*CHAPTERS, *WHATS_NEW.values()) for step in chapter.steps]


def _walk(qapp: QApplication, window: MainWindow) -> list[tuple[str, bool]]:
    """Take the tour under way to its end: each card's title, and whether
    it pointed at something."""
    seen: list[tuple[str, bool]] = []
    while window._tour is not None:
        overlay = _shown(qapp, window)
        qapp.processEvents()
        if overlay._seek.isActive():
            # The target is not shown yet: the plot is still looking for values.
            wait_until(qapp, lambda shown=overlay: not shown._seek.isActive())
        seen.append((overlay.card._title.text(), overlay.ring() is not None))
        overlay.card._default.click()
        qapp.processEvents()
    return seen


# ── The sample board ─────────────────────────────────────────────────────────


class TestTheSampleBoard:
    def test_its_lines_are_the_same_every_time(self):
        assert [sample_lines(step) for step in range(80)] == [sample_lines(step) for step in range(80)]

    def test_it_sends_a_reading_of_three_values_every_step(self):
        for step in range(80):
            assert re.fullmatch(r"temp=\d+\.\d humidity=\d+\.\d current=\d\.\d\d", sample_lines(step)[0])

    def test_it_sends_lines_at_every_level_now_and_then(self):
        tags = {TagDetector.detect(line) for step in range(1, 121) for line in sample_lines(step)[1:]}
        assert {"INFO", "WARNING", "DEBUG", "ERROR"} <= tags

    def test_its_first_line_is_a_restart_that_is_recognised(self):
        assert ResetDetector().check(_START[0]) == "POWERON_RESET"

    def test_its_times_count_from_its_start_as_esp_idf_does(self):
        line = next(line for line in sample_lines(15) if line.startswith("I ("))
        assert line.startswith(f"I ({1000 + 15 * SAMPLE_LINE_INTERVAL_MS})")

    def test_only_the_sample_port_is_opened(self):
        transport, reason = open_sample_transport("COM7", 115200)

        assert transport is None
        assert reason == "COM7 is not the sample board"

    def test_a_sample_tab_reads_it_through_the_real_reading_path(self, qapp, window, config):
        tab = window.new_tab(sample=True)
        tab.connect_sample()
        wait_until(qapp, lambda: tab.is_connected)
        wait_until(qapp, lambda: tab.status_bar.texts()[3] not in ("0 lines", ""))

        assert tab.title == f"{SAMPLE_PORT_NAME} · No device"
        assert tab.is_sample
        assert SAMPLE_PORT_NAME not in config.get("Ports", "remembered_json", fallback="")

    def test_a_sample_tab_is_no_connection_to_ask_about_on_exit(self, qapp, window):
        tab = window.new_tab(sample=True)
        tab.connect_sample()
        wait_until(qapp, lambda: tab.is_connected)

        assert window._may_exit() is True


# ── Every step ───────────────────────────────────────────────────────────────


class TestEveryStep:
    def test_every_steps_words_are_filled_in(self):
        off = [switched_off(spec) for spec in MODULES]
        for step in (*_all_steps(), SAMPLE_SESSION, *off):
            for text in (step.title, words(step.text)):
                assert "{" not in text and "}" not in text, text
                assert chr(0x2014) not in text and chr(0x2013) not in text, text
        for spec in MODULES:
            assert chr(0x2014) not in page_hidden(spec)

    def test_every_feature_has_its_switch_marked(self):
        assert set(FEATURE_TARGETS) == {spec.id for spec in MODULES}

    def test_every_entry_a_step_opens_is_in_the_menu_bar(self, window):
        for step in _all_steps():
            if step.place is Place.MENU:
                action = find_action(window, step.target)
                assert action is not None, step.title
                assert menu_path(window.menuBar(), action), step.title

    def test_every_page_a_step_opens_is_a_page_of_preferences(self):
        for step in _all_steps():
            assert (step.place is Place.PREFERENCES) == (step.page in pages.PAGES), step.title

    def test_the_whole_tour_points_at_every_control_it_names(self, qapp, window, config):
        _every_feature_on(config)
        window.start_tour(CHAPTERS)
        seen = _walk(qapp, window)

        steps = [step for chapter in CHAPTERS for step in chapter.steps]
        assert [title for title, _ in seen] == (
            [s.title for s in CHAPTERS[0].steps] + [SAMPLE_SESSION.title]
            + [s.title for chapter in CHAPTERS[1:] for s in chapter.steps]
        )
        pointed = dict(seen)
        for step in steps:
            assert pointed[step.title] is (step.target is not None), step.title
        assert config.tour_chapters_taken == tuple(chapter.id for chapter in CHAPTERS)

    def test_whats_new_points_at_what_it_names(self, qapp, window):
        news = WHATS_NEW[NEWS_LINE]
        window.start_tour((news,))
        seen = _walk(qapp, window)

        assert seen[0] == (SAMPLE_SESSION.title, False)
        assert all(pointed for _, pointed in seen[1:])

    def test_a_toolbar_button_in_the_more_menu_is_said_to_be_there(self, qapp, config, light):
        narrow = MainWindow(config, light, transport_factory=LoopbackPort().factory)
        narrow.resize(tokens.METRICS.window_min_width, 700)
        narrow.show()
        qapp.processEvents()
        commands = next(chapter for chapter in CHAPTERS if chapter.id == "commands")
        narrow.start_tour((commands,))
        texts: dict[str, str] = {}
        while narrow._tour is not None:
            overlay = _shown(qapp, narrow)
            title, text = _card_texts(overlay)
            texts[title] = text
            overlay.card._default.click()
            qapp.processEvents()
        narrow.shutdown_all_tabs()

        assert texts["Control lines"].endswith(tour_module.IN_MORE_MENU)
        assert not texts["Line ending"].endswith(tour_module.IN_MORE_MENU)


# ── What the tour opens and puts away ────────────────────────────────────────


class TestWhatItPutsAway:
    def test_it_leaves_the_users_tabs_as_they_were(self, qapp, window):
        users = window.current_tab()
        window.start_tour(CHAPTERS)
        _walk(qapp, window)

        assert window.tabs() == [users]
        assert window.current_tab() is users

    def test_a_tab_on_its_first_screen_is_opened_for_the_first_chapter_and_closed(self, qapp, window):
        busy = window.new_tab(sample=True)
        busy.connect_sample()
        wait_until(qapp, lambda: busy.is_connected)
        window.start_tour((CHAPTERS[0],))
        overlay = _shown(qapp, window)

        shown = window.current_tab()
        assert shown is not busy and not shown.has_session
        assert find(shown, Target.PORTS)
        overlay.card._default.click()
        _walk(qapp, window)
        assert window.current_tab() is busy
        assert len(window.tabs()) == 2

    def test_closing_the_tour_midway_closes_the_sample_and_its_plot(self, qapp, window):
        plot = next(chapter for chapter in CHAPTERS if chapter.id == "plot")
        window.start_tour((plot,))
        while True:
            overlay = _shown(qapp, window)
            if overlay.card._title.text() == "Values":
                break
            overlay.card._default.click()
            qapp.processEvents()
        sample = window._tour._sample_tab
        assert sample.is_plot_visible()

        _button(overlay, "Close tour").click()
        qapp.processEvents()

        assert window._tour is None
        assert sample not in window.tabs()

    def test_escape_closes_the_tour(self, qapp, window):
        window.start_tour((CHAPTERS[0],))
        overlay = _shown(qapp, window)

        QApplication.sendEvent(overlay, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
        qapp.processEvents()

        assert window._tour is None

    def test_the_arrow_keys_go_back_and_on(self, qapp, window):
        window.start_tour((CHAPTERS[0],))
        overlay = _shown(qapp, window)
        steps = CHAPTERS[0].steps

        for key, title in ((Qt.Key.Key_Right, steps[1].title), (Qt.Key.Key_Left, steps[0].title)):
            QApplication.sendEvent(overlay, QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier))
            qapp.processEvents()
            assert overlay.card._title.text() == title

    def test_nothing_on_the_sample_board_is_counted(self, qapp, window, config, monkeypatch):
        _every_feature_on(config)
        noted: list[object] = []
        for name in ("note_feature", "note_connection", "note_line_rate", "note_problem"):
            monkeypatch.setattr(window._usage, name, lambda *args, n=name: noted.append(n))
        window.start_tour((*CHAPTERS[1:3], _chapter("watching")))
        _walk(qapp, window)

        assert noted == []

    def test_a_feature_switched_off_is_shown_at_its_switch_once(self, qapp, window, config):
        ModuleSettings(config).set_enabled(PLOT, False)
        window.start_tour((_chapter("plot"),))
        _walk_to(qapp, window, switched_off(PLOT).title)
        tour = window._tour

        assert isinstance(tour._preferences, PreferencesDialog)
        assert tour._preferences.current_page == pages.FEATURES
        assert tour._overlay.ring() is not None
        assert tour._overlay.card._count.text().endswith("1 of 1")
        _next(qapp, window)
        assert window._tour is None

    def test_back_returns_to_a_feature_switched_off(self, qapp, window, config):
        ModuleSettings(config).set_enabled(PLOT, False)
        window.start_tour((_chapter("plot"), _chapter("watching")))
        overlay = _walk_to(qapp, window, _chapter("watching").steps[0].title)

        _button(overlay, "Back").click()
        qapp.processEvents()

        assert _shown(qapp, window).card._title.text() == switched_off(PLOT).title


# ── Where a step is shown: menus, Preferences, the report ───────────────────


def _menu_steps() -> list:
    return [step for step in _all_steps() if step.place is Place.MENU]


class TestMenus:
    def test_each_entry_is_shown_in_its_menu_opened_and_chosen(self, qapp, window, config):
        _every_feature_on(config)
        window.start_tour((*CHAPTERS, *WHATS_NEW.values()))
        bar = window.menuBar()
        shown: list[str] = []
        left_open: list[QMenu] = []
        while window._tour is not None:
            overlay = _shown(qapp, window)
            if overlay._seek.isActive():
                wait_until(qapp, lambda shown_now=overlay: not shown_now._seek.isActive())
            step = next((s for s in _menu_steps() if s.title == overlay.card._title.text()), None)
            if step is not None:
                action = find_action(window, step.target)
                path = menu_path(bar, action)
                assert all(menu.isVisible() for menu in path), step.title
                assert QApplication.activePopupWidget() is path[-1], step.title
                if action.menu() is None:
                    assert path[-1].activeAction() is action, step.title
                opener = window._tour._menu
                card = overlay.card.geometry()
                assert not card.intersects(opener.area(overlay.host)), step.title
                assert overlay.ring() is not None, step.title
                shown.append(step.title)
                left_open = list(path)
            _next(qapp, window)
            # A menu stays open only for a next step in it.
            now_open = window._tour._menu.menus if window._tour and window._tour._menu else ()
            assert not any(menu.isVisible() for menu in left_open if menu not in now_open)
            left_open = []

        assert shown == [step.title for step in _menu_steps()]

    def test_keys_on_the_open_menu_work_the_tour_and_choose_nothing(self, qapp, window):
        window.start_tour((_chapter("settings"),))
        _walk_to(qapp, window, _chapter("settings").steps[0].title)
        action = find_action(window, Target.MENU_PREFERENCES)
        menu = menu_path(window.menuBar(), action)[-1]
        steps = _chapter("settings").steps

        for key in (Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_X, Qt.Key.Key_Tab):
            _key(menu, key)
            qapp.processEvents()
            assert menu.isVisible() and menu.activeAction() is action
        assert window._tour._preferences is None
        _key(menu, Qt.Key.Key_Return)
        qapp.processEvents()
        assert _shown(qapp, window).card._title.text() == steps[1].title
        assert not menu.isVisible()

        _key(_shown(qapp, window), Qt.Key.Key_Left)
        qapp.processEvents()
        assert menu.isVisible() and menu.activeAction() is action
        _key(menu, Qt.Key.Key_Right)
        qapp.processEvents()
        assert _shown(qapp, window).card._title.text() == steps[1].title
        _key(_shown(qapp, window), Qt.Key.Key_Left)
        qapp.processEvents()
        _key(menu, Qt.Key.Key_Escape)
        qapp.processEvents()

        assert window._tour is None
        assert not menu.isVisible()

    def test_a_click_beside_the_menu_leaves_it_open_and_one_on_the_card_is_passed_on(self, qapp, window):
        window.start_tour((_chapter("settings"),))
        overlay = _walk_to(qapp, window, _chapter("settings").steps[0].title)
        menu = menu_path(window.menuBar(), find_action(window, Target.MENU_PREFERENCES))[-1]
        beside = window.mapToGlobal(window.rect().bottomRight() - QPoint(4, 4))

        _press_and_release(menu, beside)
        qapp.processEvents()
        assert menu.isVisible()
        assert overlay.card._title.text() == _chapter("settings").steps[0].title

        next_button = _button(overlay, "Next")
        _press_and_release(menu, next_button.mapToGlobal(next_button.rect().center()))
        qapp.processEvents()
        assert _shown(qapp, window).card._title.text() == _chapter("settings").steps[1].title

    def test_menus_the_system_closed_open_again_when_the_window_is_back(self, qapp, window):
        window.start_tour((_chapter("settings"),))
        _walk_to(qapp, window, _chapter("settings").steps[0].title)
        menu = menu_path(window.menuBar(), find_action(window, Target.MENU_PREFERENCES))[-1]

        menu.hide()
        qapp.processEvents()
        assert not menu.isVisible()
        QApplication.sendEvent(window, QEvent(QEvent.Type.WindowActivate))
        qapp.processEvents()

        assert menu.isVisible()

    def test_a_menu_bar_the_system_draws_is_told_in_words(self, qapp, window, monkeypatch):
        monkeypatch.setattr(tour_module, "opens_in_window", lambda bar: False)
        window.start_tour((_chapter("settings"),))
        overlay = _walk_to(qapp, window, _chapter("settings").steps[0].title)
        bar = window.menuBar()

        assert QApplication.activePopupWidget() is None
        assert overlay.card._text.text().endswith(where_words(bar, find_action(window, Target.MENU_PREFERENCES)))
        assert overlay.ring() is None

    def test_where_an_entry_is_said_in_words(self, window):
        bar = window.menuBar()

        assert where_words(bar, find_action(window, Target.MENU_THEME)) == (
            "It is at View > Theme, in the menu bar at the top of the screen."
        )
        assert where_words(bar, find_action(window, Target.MENU_HELP)) == "It is the Help menu, at the top of the screen."
        assert where_words(bar, find_action(window, Target.MENU_PREFERENCES)) == (
            f"It is in the {APP_NAME} menu, at the top of the screen."
        )


class TestPreferences:
    def test_a_step_there_opens_its_page_points_at_the_setting_and_keeps_nothing(self, qapp, window, config):
        before = {section: dict(config._config[section]) for section in config._config.sections()}
        window.start_tour((_chapter("logging"),))
        overlay = _walk_to(qapp, window, "Log settings")
        dialog = window._tour._preferences

        assert dialog.isVisible() and dialog.current_page == pages.LOGGING
        assert overlay.host is dialog
        marked = find(dialog, Target.LOG_SETTINGS)
        assert len(marked) == 6
        ring = overlay.ring()
        for widget in marked:
            assert ring.contains(widget.mapTo(dialog, widget.rect().center()))
        card = overlay.card.geometry()
        assert overlay.rect().contains(card)
        if overlay.width() >= ring.right() + overlay.card.width() + 2 * tokens.METRICS.space_4:
            # Where there is room beside the settings, the card is there.
            assert not card.intersects(ring)
        finished: list[int] = []
        dialog.finished.connect(finished.append)

        _next(qapp, window)

        assert finished == [QDialog.DialogCode.Rejected]
        after = {section: dict(config._config[section]) for section in config._config.sections()}
        assert {k: v for k, v in after.items() if k != "Tour"} == {k: v for k, v in before.items() if k != "Tour"}

    def test_steps_there_in_a_row_share_one_window(self, qapp, window):
        window.start_tour((_chapter("settings"),))
        _walk_to(qapp, window, "One page per subject")
        dialog = window._tour._preferences
        assert dialog.current_page == pages.APPEARANCE

        _next(qapp, window)

        assert window._tour._preferences is dialog
        assert dialog.current_page == pages.FEATURES
        assert window._tour._overlay.ring() is not None

    def test_closing_it_from_its_title_bar_ends_the_tour(self, qapp, window):
        window.start_tour((_chapter("settings"),))
        _walk_to(qapp, window, "One page per subject")

        window._tour._preferences.reject()
        qapp.processEvents()

        assert window._tour is None

    def test_a_page_not_in_the_list_is_shown_and_how_to_add_it_said(self, qapp, window, config):
        assert not ModuleSettings(config).is_enabled(ALERTS)
        window.start_tour((_chapter("alerts"),))
        overlay = _walk_to(qapp, window, "What alerts tell you")

        assert window._tour._preferences.current_page == pages.ALERTS
        assert overlay.card._text.text().endswith(page_hidden(ALERTS))
        assert overlay.ring() is not None


class TestReport:
    def test_the_report_step_opens_the_sample_sessions_report_and_closes_it(self, qapp, window, config):
        _every_feature_on(config)
        window.start_tour((_chapter("watching"),))
        overlay = _walk_to(qapp, window, "The report")
        report = window._tour._report

        assert isinstance(report, SessionReportDialog) and report.isVisible()
        assert overlay.host is report
        assert overlay.ring() is not None
        assert "Sample" in report.text()

        _next(qapp, window)
        assert not report.isVisible()


class TestDimming:
    @staticmethod
    def _covers(widget) -> list:
        from ui.tour.overlay import Backdrop

        return [
            child for child in widget.children()
            if isinstance(child, (TourOverlay, Backdrop)) and child.isVisible()
        ]

    def test_the_main_window_is_dimmed_at_every_step_and_never_twice(self, qapp, window, config):
        _every_feature_on(config)
        window.start_tour((*CHAPTERS, *WHATS_NEW.values()))
        while window._tour is not None:
            overlay = _shown(qapp, window)
            tour = window._tour
            assert len(self._covers(window)) == 1, overlay.card._title.text()
            if overlay.host is not window:
                assert len(self._covers(overlay.host)) == 1
                assert tour._backdrop is not None and tour._backdrop.isVisible()
                assert window.focusWidget() is tour._backdrop
            else:
                assert tour._backdrop is None
            _next(qapp, window)

        assert self._covers(window) == []

    def test_the_window_stays_dimmed_while_the_sample_board_opens(self, qapp, window):
        window.start_tour((_chapter("reading"),))

        assert not window._tour._sample_tab.is_connected
        assert len(self._covers(window)) == 1
        _shown(qapp, window)
        assert len(self._covers(window)) == 1

    def test_covers_stay_above_a_widget_added_to_their_window(self, qapp, window):
        from PySide6.QtWidgets import QLabel

        window.start_tour((_chapter("settings"),))
        _walk_to(qapp, window, "One page per subject")
        backdrop = window._tour._backdrop
        overlay = window._tour._overlay

        notice = QLabel("Notice", window)
        notice.show()
        extra = QLabel("Notice", overlay.host)
        extra.show()
        qapp.processEvents()

        assert window.children()[-1] is backdrop
        assert overlay.host.children()[-1] is overlay

    def test_the_backdrop_takes_keys_clicks_and_the_windows_menu(self, qapp, window):
        from PySide6.QtGui import QContextMenuEvent

        window.start_tour((_chapter("settings"),))
        _walk_to(qapp, window, "One page per subject")
        backdrop = window._tour._backdrop
        tabs = len(window.tabs())

        QTest.keySequence(backdrop, QKeySequence(shortcuts.keys("new_tab")))
        menu = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(10, 10), backdrop.mapToGlobal(QPoint(10, 10)))
        QApplication.sendEvent(backdrop, menu)
        qapp.processEvents()
        assert len(window.tabs()) == tabs
        assert menu.isAccepted()

        _key(backdrop, Qt.Key.Key_Escape)
        qapp.processEvents()
        assert window._tour is None


class TestKeys:
    def test_no_shortcut_of_the_window_works_behind_the_tour(self, qapp, window):
        window.start_tour((CHAPTERS[0],))
        overlay = _shown(qapp, window)
        tabs = len(window.tabs())
        focused = QApplication.focusWidget() or overlay

        QTest.keySequence(focused, QKeySequence(shortcuts.keys("new_tab")))
        qapp.processEvents()

        assert len(window.tabs()) == tabs
        assert window._tour is not None


# ── Motion ───────────────────────────────────────────────────────────────────


class TestMotion:
    @pytest.mark.parametrize("reduced", [False, True])
    def test_the_card_moves_only_when_motion_is_not_reduced(self, qapp, window, reduced):
        overlay = TourOverlay(window, reduced)
        overlay.show_step("A", "B", "", [CardAction("Next", lambda: None, primary=True)], lambda: None)

        arrival = overlay._arrival
        assert arrival.state() is QAbstractAnimation.State.Running
        durations = {arrival.animationAt(i).duration() for i in range(arrival.animationCount())}
        assert arrival.animationCount() == (1 if reduced else 2)
        assert durations == {tokens.MOTION.fast if reduced else tokens.MOTION.base}
        overlay.close_overlay()


# ── The offer at start, and the list ─────────────────────────────────────────


class TestTheOffer:
    def test_it_is_made_once_and_the_question_after_it_waits(self, qapp, window, config, monkeypatch):
        monkeypatch.setattr(main_window_module, "APP_VERSION", NEWS_VERSION)
        after: list[str] = []

        window.offer_tour(lambda: after.append("question"))
        overlay = window._offer
        assert overlay.card._title.text() == f"Welcome to {APP_NAME}"
        assert after == []
        _button(overlay, "Not now").click()

        assert after == ["question"]
        assert config.tour_offered_version == NEWS_VERSION
        window.offer_tour(lambda: after.append("again"))
        assert after == ["question", "again"]
        assert window._offer is None

    def test_someone_updating_is_offered_the_whole_tour_too(self, qapp, window, config, monkeypatch):
        monkeypatch.setattr(main_window_module, "APP_VERSION", NEWS_VERSION)
        monkeypatch.setattr(type(config), "first_run", property(lambda self: False))

        window.offer_tour(lambda: None)

        assert window._offer.card._title.text() == f"{APP_NAME} now has a tour"
        assert _button(window._offer, "Start the tour")
        window._end_offer()

    def test_a_later_version_offers_what_is_new(self, qapp, window, config, monkeypatch):
        monkeypatch.setattr(main_window_module, "APP_VERSION", NEWS_VERSION)
        config.tour_offered_version = "0.9.0"

        window.offer_tour(lambda: None)

        assert window._offer.card._title.text() == WHATS_NEW[NEWS_LINE].title
        _button(window._offer, "Show me").click()
        overlay = _shown(qapp, window)
        assert overlay.card._title.text() == SAMPLE_SESSION.title

    def test_a_release_that_only_fixes_offers_nothing_again(self, qapp, window, config, monkeypatch):
        config.tour_offered_version = NEWS_VERSION
        monkeypatch.setattr(main_window_module, "APP_VERSION", f"{NEWS_LINE}.1")
        after: list[str] = []

        window.offer_tour(lambda: after.append("question"))

        assert after == ["question"]
        assert window._offer is None

    def test_an_earlier_version_returned_to_offers_nothing(self, qapp, window, config, monkeypatch):
        config.tour_offered_version = "9.0.0"
        monkeypatch.setattr(main_window_module, "APP_VERSION", NEWS_VERSION)
        after: list[str] = []

        window.offer_tour(lambda: after.append("question"))

        assert after == ["question"]
        assert window._offer is None
        assert config.tour_offered_version == "9.0.0"

    def test_a_release_that_only_fixes_keeps_what_is_new_in_the_list(self, qapp, window, monkeypatch):
        monkeypatch.setattr(main_window_module, "APP_VERSION", f"{NEWS_LINE}.1")
        listed: list[tuple] = []
        monkeypatch.setattr(main_window_module, "TourDialog", _Listing(listed))

        window.open_tour_chapters()

        assert WHATS_NEW[NEWS_LINE] in listed[0]

    def test_the_question_waits_for_the_tour_taken_from_the_offer(self, qapp, window, monkeypatch):
        monkeypatch.setattr(main_window_module, "APP_VERSION", NEWS_VERSION)
        after: list[str] = []
        window.offer_tour(lambda: after.append("question"))

        _button(window._offer, "Start the tour").click()
        overlay = _shown(qapp, window)
        assert after == []
        _button(overlay, "Close tour").click()

        assert after == ["question"]


class TestTheList:
    def test_it_lists_every_chapter_and_says_which_were_taken(self, qapp, light):
        dialog = TourDialog(CHAPTERS, CHAPTERS, ("connecting",))

        starts = [b for b in dialog.findChildren(QPushButton) if b.text() == "Start"]
        assert [b.accessibleName() for b in starts] == [f"Start {c.title}" for c in CHAPTERS]
        assert chapter_hint(CHAPTERS[0], True).endswith(", taken.")
        assert chapter_hint(CHAPTERS[1], False) == f"{CHAPTERS[1].summary} {len(CHAPTERS[1].steps)} steps."

    def test_the_whole_tour_or_one_chapter_is_chosen(self, qapp, light):
        dialog = TourDialog((*CHAPTERS, WHATS_NEW[NEWS_LINE]), CHAPTERS, ())
        chosen: list[object] = []
        dialog.chosen.connect(chosen.append)

        next(b for b in dialog.findChildren(QPushButton) if b.text() == "Take the whole tour").click()

        assert chosen == [CHAPTERS]

    def test_help_offers_the_tour(self, window):
        texts = {action.text() for action in window.findChildren(QAction)}
        assert "Take the tour..." in texts
