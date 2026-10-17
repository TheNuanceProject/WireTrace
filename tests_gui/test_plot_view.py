# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests of the live plot with real pyqtgraph: the value list, panels by
scale, the measuring cursors, and the plot in a window of its own."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtWidgets import QApplication, QFrame, QPushButton

from app.config import ConfigManager
from app.constants import (
    NAME_SEPARATOR,
    PLOT_MAX_PANELS,
    PLOT_REGEX_MAX_CONSECUTIVE_TIMEOUTS,
    ThemeID,
)
from core.plot_engine import PlotEngine
from tests_gui.support import LoopbackPort, pixel, wait_until
from ui import shortcuts
from ui.device_tab import DeviceTab
from ui.themes import tokens
from ui.themes.theme_manager import ThemeManager
from ui.widgets.connect_page import ConnectPage
from ui.widgets.plot_view import PlotLayout, PlotView
from ui.widgets.plot_window import PlotWindow

_START = datetime(2026, 4, 18, 19, 25, 0)
_SECONDS = 60
_PER_SECOND = 10


def _feed(engine: PlotEngine, seconds: int = _SECONDS) -> None:
    """A motor run: RPM near 1460, current near 2.4 with a step at 30 s,
    voltage near 12.5, temperature falling from 52."""
    for index in range(seconds * _PER_SECOND + 1):
        t = index / _PER_SECOND
        stepped = 30 <= t < 36
        line = (
            f"RPM: {1460 - (40 if stepped else 0)}, "
            f"Current: {4.0 if stepped else 2.4}, "
            f"Voltage: {12.25 if stepped else 12.5}, "
            f"MotorTemp: {52.0 - t * 0.1:.2f}"
        )
        engine.process(line, _START + timedelta(seconds=t))


@pytest.fixture
def engine(qapp: QApplication) -> PlotEngine:
    plot_engine = PlotEngine()
    plot_engine.set_active(True)
    return plot_engine


@pytest.fixture
def make_view(qapp: QApplication, theme_manager: ThemeManager, engine: PlotEngine):
    """Build plot views that are shut down when the test ends, pass or fail.

    A plot must be shut down before it is destroyed; see PlotView.shutdown.
    """
    theme_manager.apply_theme(ThemeID.LIGHT)
    views: list[PlotView] = []

    def build() -> PlotView:
        plot_view = PlotView(engine, theme_manager)
        views.append(plot_view)
        plot_view.resize(1000, 520)
        plot_view.show()
        qapp.processEvents()
        return plot_view

    yield build
    for plot_view in views:
        plot_view.shutdown()


@pytest.fixture
def view(make_view) -> PlotView:
    return make_view()


@pytest.fixture
def running(qapp: QApplication, view: PlotView, engine: PlotEngine) -> PlotView:
    """A view that has found the four values and drawn a minute of them."""
    _feed(engine)
    view._redraw()
    view._update_figures()
    qapp.processEvents()
    return view


def _button(view: PlotView, text: str) -> QPushButton:
    return next(b for b in view.findChildren(QPushButton) if b.text() == text)


class TestWaiting:
    def test_view_waits_until_values_are_found(self, view):
        assert view._stack.currentIndex() == 0
        assert view._placeholder.text() == "Waiting for lines that hold values"
        assert not view._describe.isVisible()
        assert view.panels() == []

    def test_lines_without_values_offer_describing_the_format(self, qapp, view, engine):
        asked: list[bool] = []
        view.configure_requested.connect(lambda: asked.append(True))
        for index in range(400):
            engine.process("boot stage complete", _START + timedelta(seconds=index))
        qapp.processEvents()

        assert view._stack.currentIndex() == 0
        assert view._placeholder.text().startswith("No values were found")
        assert view._describe.isVisible()

        view._describe.click()
        assert asked == [True]

    def test_a_pattern_that_keeps_running_out_of_time_is_reported(self, qapp, view, engine):
        _feed(engine)
        engine.set_manual_config(r"(?P<x>(a|a)+)+$")
        for index in range(PLOT_REGEX_MAX_CONSECUTIVE_TIMEOUTS):
            engine.process("a" * 40 + "b", _START + timedelta(seconds=index))
        qapp.processEvents()

        assert view._stack.currentIndex() == 0
        assert view._placeholder.text().startswith("The pattern takes too long")
        assert view._describe.isVisible()

    def test_view_built_after_a_pattern_stalled_says_so(self, make_view, engine):
        engine.set_manual_config(r"(?P<x>(a|a)+)+$")
        for index in range(PLOT_REGEX_MAX_CONSECUTIVE_TIMEOUTS):
            engine.process("a" * 40 + "b", _START + timedelta(seconds=index))

        late = make_view()

        assert late._stack.currentIndex() == 0
        assert late._placeholder.text().startswith("The pattern takes too long")

    def test_view_built_after_detection_starts_from_what_was_found(self, make_view, engine):
        _feed(engine)

        late = make_view()

        assert late._stack.currentIndex() == 1
        assert [name for name, _, _ in late.value_texts()] == [
            "RPM", "Current", "Voltage", "MotorTemp",
        ]


class TestValues:
    def test_each_value_shows_its_figures_for_the_time_in_view(self, running):
        texts = {name: (last, figures) for name, last, figures in running.value_texts()}

        assert texts["RPM"] == ("1,460", "min 1,420  max 1,460  avg 1,456")
        assert texts["Current"][0] == "2.4"
        assert texts["Current"][1].startswith("min 2.4  max 4  avg ")
        assert texts["MotorTemp"] == ("46", "min 46  max 52  avg 49")

    def test_figures_follow_the_time_shown(self, qapp, running):
        running._window_combo.setCurrentIndex(running._window_combo.findData(10.0))
        running._redraw()
        qapp.processEvents()
        running._update_figures()

        texts = {name: figures for name, _, figures in running.value_texts()}

        assert texts["RPM"] == "min 1,460  max 1,460  avg 1,460"
        assert running.summary() == "4 values, last 10 s shown"

    def test_showing_everything_is_said_in_the_summary(self, running):
        running._window_combo.setCurrentIndex(running._window_combo.findData(None))

        assert running.summary() == "4 values, everything held is shown"

    def test_unticking_a_value_hides_its_line_only(self, running):
        running.set_value_shown("Current", False)

        assert running._traces["Current"].isVisible() is False
        assert running._traces["RPM"].isVisible() is True
        assert [name for name, _, _ in running.value_texts()] == [
            "RPM", "Current", "Voltage", "MotorTemp",
        ]

    def test_hidden_value_stays_hidden_when_the_layout_changes(self, running):
        running.set_value_shown("Voltage", False)

        running.set_layout_mode(PlotLayout.PANELS)

        assert running._traces["Voltage"].isVisible() is False

    def test_long_name_gives_way_to_the_value(self, make_view, engine):
        name = "A_value_with_a_very_long_name_indeed_for_a_small_list"
        for index in range(60):
            engine.process(f"{name}: {index}", _START + timedelta(seconds=index))
        view = make_view()
        row = view._rows[name]

        assert row.width() <= tokens.METRICS.plot_values_width
        assert row._check.text() != name
        assert name in row._check.toolTip()

    def test_values_are_drawn_in_the_colours_of_the_theme(self, qapp, running, theme_manager):
        assert running._traces["RPM"].opts["pen"].color().name().upper() == (
            tokens.LIGHT.plot_traces[0]
        )

        theme_manager.apply_theme(ThemeID.DARK)

        assert running._traces["RPM"].opts["pen"].color().name().upper() == (
            tokens.DARK.plot_traces[0]
        )
        assert running._traces["MotorTemp"].opts["pen"].color().name().upper() == (
            tokens.DARK.plot_traces[3]
        )
        assert pixel(running._graphics, 4, 4).name().upper() == tokens.DARK.paper


class TestPanels:
    def test_one_chart_holds_every_value(self, running):
        assert running.layout_mode is PlotLayout.ONE_CHART
        assert running.panels() == [["RPM", "Current", "Voltage", "MotorTemp"]]

    def test_panels_group_values_of_similar_size(self, running):
        _button(running, "Panels by scale").click()

        assert running.layout_mode is PlotLayout.PANELS
        assert running.panels() == [["RPM"], ["Current", "Voltage"], ["MotorTemp"]]

    def test_panels_share_one_time_axis_and_only_the_last_shows_it(self, running):
        running.set_layout_mode(PlotLayout.PANELS)
        first, *rest = running._panels

        for panel in rest:
            assert panel.item.getViewBox().linkedView(0) is first.item.getViewBox()
        assert [panel.item.getAxis("bottom").style["showValues"] for panel in running._panels] == [
            False, False, True,
        ]
        widths = {panel.item.getAxis("left").fixedWidth for panel in running._panels}
        assert widths == {tokens.METRICS.plot_axis_width}

    def test_returning_to_one_chart_restores_it(self, running):
        running.set_layout_mode(PlotLayout.PANELS)

        _button(running, "One chart").click()

        assert running.panels() == [["RPM", "Current", "Voltage", "MotorTemp"]]

    def test_panels_chosen_before_any_value_arrives_are_sorted_on_the_first_data(
        self, qapp, view, engine,
    ):
        view.set_layout_mode(PlotLayout.PANELS)
        assert view.panels() == []

        _feed(engine)
        view._redraw()

        assert view.panels() == [["RPM"], ["Current", "Voltage"], ["MotorTemp"]]

    def test_fit_sorts_the_values_into_panels_again(self, qapp, running, engine):
        running.set_layout_mode(PlotLayout.PANELS)
        for index in range(50):
            engine.process(
                "RPM: 1460, Current: 900, Voltage: 12.5, MotorTemp: 46",
                _START + timedelta(seconds=_SECONDS + 1 + index),
            )
        running._redraw()
        assert running.panels() == [["RPM"], ["Current", "Voltage"], ["MotorTemp"]]

        _button(running, "Fit").click()
        running._redraw()

        assert running.panels() == [["RPM", "Current"], ["Voltage", "MotorTemp"]]

    def test_many_values_never_make_more_panels_than_the_limit(self, make_view, engine):
        for index in range(60):
            line = ", ".join(f"v{power}: {10.0 ** (power * 2)}" for power in range(8))
            engine.process(line, _START + timedelta(seconds=index))
        view = make_view()
        view._redraw()

        view.set_layout_mode(PlotLayout.PANELS)

        assert len(view.panels()) == PLOT_MAX_PANELS
        assert sorted(name for panel in view.panels() for name in panel) == sorted(
            f"v{power}" for power in range(8)
        )

    def test_switching_layout_many_times_leaves_one_set_of_charts(self, qapp, running):
        for _ in range(25):
            running.set_layout_mode(PlotLayout.PANELS)
            running.set_layout_mode(PlotLayout.ONE_CHART)
        qapp.processEvents()

        assert len(running._panels) == 1
        assert len(running._graphics.ci.items) == 1


class TestTimeShown:
    def test_newest_data_is_kept_in_view(self, running):
        low, high = running._time_in_view()

        assert high >= _SECONDS
        assert low <= 1.5

    def test_shorter_time_shows_only_the_newest_part(self, qapp, running):
        running._window_combo.setCurrentIndex(running._window_combo.findData(10.0))
        running._redraw()
        qapp.processEvents()
        low, high = running._time_in_view()

        assert 49 <= low <= 51
        assert high >= _SECONDS

    def test_pause_holds_the_picture_and_resume_catches_up(self, qapp, running, engine):
        states: list[bool] = []
        running.pause_toggled.connect(states.append)
        _button(running, "Pause").click()
        held = running._data["RPM"][0].size
        for index in range(30):
            engine.process(
                "RPM: 1, Current: 1, Voltage: 1, MotorTemp: 1",
                _START + timedelta(seconds=_SECONDS + 1 + index),
            )

        running._redraw()
        assert running._data["RPM"][0].size == held
        assert _button(running, "Resume").isChecked()

        _button(running, "Resume").click()
        running._redraw()

        assert running._data["RPM"][0].size == held + 30
        assert states == [True, False]

    def test_clear_forgets_the_samples_and_keeps_the_values(self, qapp, running, engine):
        _button(running, "Clear").click()

        assert engine.has_any_data is False
        assert [last for _, last, _ in running.value_texts()] == ["no data"] * 4
        assert running.panels() == [["RPM", "Current", "Voltage", "MotorTemp"]]


_HEADING = ["", "Time", "RPM", "Current", "Voltage", "MotorTemp"]


def _scene_point(view: PlotView, time: float, panel: int = 0) -> QPointF:
    """Where ``time`` lies in a panel, half way up, in scene coordinates.

    Qt lays the panels out in a pass of its own after they are made, so
    this waits until the point lies inside the panel it is meant for: a
    point read from a layout not yet done can lie outside every panel.
    """
    def point() -> QPointF:
        box = view._panels[panel].item.getViewBox()
        low, high = box.viewRange()[1]
        return box.mapViewToScene(QPointF(time, (low + high) / 2))

    def in_its_panel() -> bool:
        area = view._panels[panel].item.getViewBox().sceneBoundingRect()
        return not area.isEmpty() and area.contains(point())

    wait_until(QApplication.instance(), in_its_panel)
    return point()


class _Click:
    """A left click at a scene position, as pyqtgraph reports one."""

    def __init__(self, scene_pos: QPointF) -> None:
        self._pos = scene_pos

    def button(self) -> Qt.MouseButton:
        return Qt.MouseButton.LeftButton

    def scenePos(self) -> QPointF:
        return self._pos


class TestMeasuring:
    def test_measuring_holds_the_plot_with_cursors_on_samples_in_view(self, running):
        _button(running, "Measure").click()
        low, high = running._time_in_view()
        a, b = running.cursor_times

        assert running.measuring is True
        assert running.paused is True
        assert _button(running, "Resume").isVisible()
        assert low < a < b < high
        assert {round(a * 10) / 10, round(b * 10) / 10} == {a, b}
        assert running._measure_table.isVisible()
        assert [len(panel.cursors) for panel in running._panels] == [2]

    def test_table_gives_each_moment_each_value_and_b_minus_a(self, running):
        running.set_measuring(True)

        running.set_cursor(0, 29.0)
        running.set_cursor(1, 33.0)

        assert running.measure_texts() == [
            _HEADING,
            ["A", "19:25:29.000", "1,460", "2.4", "12.5", "49.1"],
            ["B", "19:25:33.000", "1,420", "4", "12.25", "48.7"],
            ["B minus A", "4 s", "-40", "+1.6", "-0.25", "-0.4"],
        ]

    def test_the_table_is_as_tall_as_its_rows(self, qapp, running):
        running.set_measuring(True)
        table = running._measure_table

        assert table._content.sizeHint().height() > 0
        wait_until(qapp, lambda: table._scroll.height() >= table._content.sizeHint().height())

    def test_a_cursor_goes_to_the_nearest_sample_received(self, running):
        running.set_measuring(True)

        running.set_cursor(0, 29.04)
        running.set_cursor(1, 500.0)

        assert running.cursor_times == (29.0, 60.0)

    def test_b_before_a_gives_a_negative_time(self, running):
        running.set_measuring(True)

        running.set_cursor(0, 33.0)
        running.set_cursor(1, 29.0)

        assert running.measure_texts()[3][:3] == ["B minus A", "-4 s", "+40"]

    def test_hidden_value_is_left_out_of_the_table(self, running):
        running.set_measuring(True)
        running.set_cursor(0, 29.0)
        running.set_cursor(1, 33.0)

        running.set_value_shown("Current", False)

        assert "Current" not in running.measure_texts()[0]

    def test_a_click_moves_the_nearer_cursor(self, running):
        running.set_measuring(True)
        running.set_cursor(0, 20.0)
        running.set_cursor(1, 40.0)

        running._on_chart_clicked(_Click(_scene_point(running, 36.02)))

        assert running.cursor_times == (20.0, 36.0)

    def test_dragging_a_cursor_in_one_panel_moves_it_in_all(self, running):
        running.set_layout_mode(PlotLayout.PANELS)
        running.set_measuring(True)

        running._panels[1].cursors[0].setValue(12.52)

        assert running.cursor_times[0] == 12.5
        assert {panel.cursors[0].value() for panel in running._panels} == {12.5}

    def test_cursors_survive_a_change_of_layout(self, running):
        running.set_measuring(True)
        running.set_cursor(0, 20.0)
        running.set_cursor(1, 40.0)

        running.set_layout_mode(PlotLayout.PANELS)

        assert running.cursor_times == (20.0, 40.0)
        assert all(len(panel.cursors) == 2 for panel in running._panels)

    def test_stopping_measuring_goes_back_to_live(self, running):
        running.set_measuring(True)

        _button(running, "Measure").click()

        assert running.measuring is False
        assert running.paused is False
        assert not running._measure_table.isVisible()
        assert all(panel.cursors == [] for panel in running._panels)

    def test_a_plot_paused_before_measuring_stays_paused(self, running):
        _button(running, "Pause").click()
        running.set_measuring(True)

        running.set_measuring(False)

        assert running.paused is True

    def test_resume_ends_measuring(self, running):
        running.set_measuring(True)

        _button(running, "Resume").click()

        assert running.paused is False
        assert running.measuring is False


class TestHover:
    def test_the_card_gives_the_nearest_sample_and_every_value_then(self, running):
        running._on_pointer_moved(_scene_point(running, 20.04))

        assert running.hover_texts() == (
            "19:25:20.000", "20 s",
            [("RPM", "1,460"), ("Current", "2.4"), ("Voltage", "12.5"), ("MotorTemp", "50")],
        )

    def test_a_line_and_a_dot_mark_the_sample_in_every_panel(self, running):
        running.set_layout_mode(PlotLayout.PANELS)
        running._on_pointer_moved(_scene_point(running, 20.04))

        for panel in running._panels:
            assert panel.hover.isVisible() and panel.hover.value() == 20.0
            for name, dot in panel.dots.items():
                x, y = dot.getData()
                assert dot.isVisible() and list(x) == [20.0]
                assert list(y) == [running._held(name, 20.0)]

    def test_the_card_stays_inside_the_chart(self, running):
        for time in (0.5, 30.0, 59.5):
            running._on_pointer_moved(_scene_point(running, time))
            assert running._chart_area.rect().contains(running._readout.geometry())

    def test_leaving_the_chart_hides_the_card_and_the_line(self, qapp, running):
        running._on_pointer_moved(_scene_point(running, 20.0))

        qapp.sendEvent(running._graphics.viewport(), QEvent(QEvent.Type.Leave))

        assert running.hover_texts() is None
        assert not any(panel.hover.isVisible() for panel in running._panels)

    def test_a_hidden_value_is_left_out_of_the_card(self, running):
        running.set_value_shown("Voltage", False)
        running._on_pointer_moved(_scene_point(running, 20.0))

        assert [name for name, _ in running.hover_texts()[2]] == ["RPM", "Current", "MotorTemp"]

    def test_the_card_follows_live_data_under_a_still_pointer(self, running, engine):
        pointer = _scene_point(running, 30.0)
        running._on_pointer_moved(pointer)
        before = running._hover_time

        for index in range(1, 101):
            t = _SECONDS + index / _PER_SECOND
            engine.process(
                f"RPM: 1460, Current: 2.4, Voltage: 12.5, MotorTemp: {52.0 - t * 0.1:.2f}",
                _START + timedelta(seconds=t),
            )
        running._redraw()

        assert running._hover_time != before
        assert running._hover_time == running._sample_under(pointer)


class TestSessions:
    def test_new_connection_starts_from_the_waiting_text(self, running):
        running.set_layout_mode(PlotLayout.PANELS)
        running.set_measuring(True)

        running.reset_session()

        assert running._stack.currentIndex() == 0
        assert running.panels() == []
        assert running.value_texts() == []
        assert running.paused is False
        assert running.measuring is False
        assert running.layout_mode is PlotLayout.PANELS

    def test_values_found_after_a_reset_are_drawn_again(self, qapp, running, engine):
        running.reset_session()

        _feed(engine, seconds=10)
        running._redraw()

        assert running._stack.currentIndex() == 1
        assert running.panels() == [["RPM", "Current", "Voltage", "MotorTemp"]]

    def test_no_drawing_happens_while_the_view_is_hidden(self, qapp, running):
        assert running._timer.isActive() and running._stats_timer.isActive()

        running.hide()
        assert not running._timer.isActive() and not running._stats_timer.isActive()

        running.show()
        assert running._timer.isActive()

    def test_shutdown_stops_the_view_for_good(self, qapp, running, theme_manager):
        running.shutdown()
        running.shutdown()

        assert running.panels() == []
        assert not running._timer.isActive()
        theme_manager.apply_theme(ThemeID.DARK)
        running.hide()
        running.show()
        assert not running._timer.isActive()

    @pytest.mark.parametrize("attempt", range(6))
    def test_view_with_panels_and_cursors_is_destroyed_without_harm(
        self, qapp, make_view, engine, attempt,
    ):
        _feed(engine, seconds=20)
        view = make_view()
        view._redraw()
        view.set_layout_mode(PlotLayout.PANELS)
        view.set_measuring(True)
        qapp.processEvents()

        view.shutdown()
        view.close()
        view.deleteLater()
        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qapp.processEvents()


class TestOwnWindow:
    @pytest.fixture
    def port(self) -> LoopbackPort:
        return LoopbackPort()

    @pytest.fixture
    def tab(self, qapp, config: ConfigManager, theme_manager, port) -> DeviceTab:
        theme_manager.apply_theme(ThemeID.LIGHT)
        device_tab = DeviceTab(theme_manager, config, transport_factory=port.factory)
        device_tab.resize(1100, 700)
        device_tab.show()
        device_tab.findChild(ConnectPage).connect_requested.emit("PORT0", 115200)
        wait_until(qapp, lambda: device_tab.is_connected)
        device_tab.toggle_plot()
        qapp.processEvents()
        yield device_tab
        device_tab.ordered_shutdown()

    def test_button_names_what_it_will_do(self, tab):
        view = tab.findChild(PlotView)

        assert _button(view, "Pop out").isVisible()
        view.set_floating(True)
        assert _button(view, "Dock").width() >= (
            _button(view, "Dock").sizeHint().width()
        )

    def test_the_toolbar_fits_the_main_window_at_its_least_width(self, tab):
        view = tab.findChild(PlotView)
        toolbar = view.findChild(QFrame, "plotToolbar")

        assert toolbar.minimumSizeHint().width() <= tokens.METRICS.window_min_width

    def test_plot_moves_to_its_own_window_and_back(self, qapp, tab):
        view = tab.findChild(PlotView)

        _button(view, "Pop out").click()
        qapp.processEvents()
        window = tab.findChild(PlotWindow)

        assert tab.is_plot_floating() is True
        assert window.isVisible()
        assert view.window() is window
        assert window.windowTitle() == "PORT0 plot - WireTrace"
        assert tab._console_plot_splitter.count() == 1
        assert tab.is_plot_visible() is True

        _button(view, "Dock").click()
        qapp.processEvents()

        assert tab.is_plot_floating() is False
        assert view.window() is tab.window()
        assert view.isVisible()
        assert tab._console_plot_splitter.count() == 2

    def test_close_button_closes_the_plot_and_stops_its_work(self, qapp, tab):
        view = tab.findChild(PlotView)
        close = view._close_btn

        assert close.isVisible()
        assert close.toolTip() == shortcuts.with_keys("Close the plot", "plot")
        close.click()
        qapp.processEvents()

        assert tab.is_plot_visible() is False
        assert not view.isVisible()
        assert not view._timer.isActive() and not view._stats_timer.isActive()
        assert tab._plot_engine._active is False

    def test_close_button_closes_the_plot_in_its_own_window(self, qapp, tab):
        tab.set_plot_floating(True)
        window = tab.findChild(PlotWindow)

        tab.findChild(PlotView)._close_btn.click()
        qapp.processEvents()

        assert tab.is_plot_visible() is False
        assert not window.isVisible()

    def test_closing_the_window_puts_the_plot_back(self, qapp, tab):
        view = tab.findChild(PlotView)
        tab.set_plot_floating(True)
        window = tab.findChild(PlotWindow)

        window.close()
        qapp.processEvents()

        assert tab.is_plot_floating() is False
        assert tab.is_plot_visible() is True
        assert view.isVisible()
        assert _button(view, "Pop out").isVisible()

    def test_plot_button_hides_and_shows_the_window(self, qapp, tab):
        tab.set_plot_floating(True)
        window = tab.findChild(PlotWindow)

        tab.toggle_plot()
        assert not window.isVisible()
        assert tab.is_plot_visible() is False

        tab.toggle_plot()
        assert window.isVisible()
        assert tab.is_plot_floating() is True

    def test_window_says_what_it_shows(self, qapp, tab, port):
        port.feed(b"".join(b"temp: %d, rpm: %d\n" % (n, n * 2) for n in range(60)))
        wait_until(qapp, lambda: tab.console.total_count == 60)
        tab.set_plot_floating(True)
        window = tab.findChild(PlotWindow)

        assert window.summary_text() == "PORT0, live  ·  2 values, last 60 s shown"

        tab.disconnect_device()
        wait_until(qapp, lambda: not tab.is_connected)
        assert window.summary_text().startswith("PORT0, not connected")

    def test_window_takes_the_name_given_to_the_tab(self, qapp, tab):
        tab.set_plot_floating(True)

        tab.rename("Pump")

        assert tab.findChild(PlotWindow).windowTitle() == f"PORT0{NAME_SEPARATOR}Pump plot - WireTrace"

    def test_split_under_the_console_is_restored_after_docking(self, qapp, tab):
        tab._console_plot_splitter.setSizes([400, 200])
        qapp.processEvents()
        before = tab._console_plot_splitter.sizes()

        tab.set_plot_floating(True)
        tab.set_plot_floating(False)
        qapp.processEvents()
        after = tab._console_plot_splitter.sizes()

        assert abs(after[1] - before[1]) <= 2

    @pytest.mark.parametrize("attempt", range(4))
    def test_tab_closed_while_the_plot_is_in_its_window(self, qapp, tab, port, attempt):
        port.feed(b"".join(b"temp: %d, rpm: %d\n" % (n, n * 2) for n in range(60)))
        wait_until(qapp, lambda: tab.console.total_count == 60)
        tab.set_plot_floating(True)
        qapp.processEvents()

        tab.ordered_shutdown()
        tab.close()
        tab.deleteLater()
        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qapp.processEvents()

        assert not any(isinstance(w, PlotWindow) and w.isVisible() for w in qapp.topLevelWidgets())


def test_measuring_takes_room_from_the_console_never_from_the_plot(qapp, config, theme_manager):
    from tests_gui.screens import main_window, start_session

    with main_window(qapp, config, theme_manager) as (window, port):
        start_session(qapp, window, port)
        window.resize(window.width(), 1)
        tab = window.current_tab()
        view = tab._plot_view
        _feed(tab._plot_engine)
        view._redraw(force=True)
        wait_until(qapp, lambda: view._graphics.isVisible())

        view.set_measuring(True)
        table = view._measure_table
        wait_until(qapp, lambda: table.isVisible() and table.height() >= table.minimumSizeHint().height())

        metrics = tokens.METRICS
        assert view._graphics.height() >= metrics.pane_min_height - metrics.header_height
        assert tab.console.height() >= metrics.pane_min_height


def test_the_chart_keeps_its_height_while_measuring(qapp, config, theme_manager):
    from tests_gui.screens import main_window, start_session

    with main_window(qapp, config, theme_manager) as (window, port):
        start_session(qapp, window, port)
        tab = window.current_tab()
        view = tab._plot_view
        _feed(tab._plot_engine)
        view._redraw(force=True)
        wait_until(qapp, lambda: view._graphics.isVisible())
        chart, console = view._graphics.height(), tab.console.height()
        table = view._measure_table

        view.set_measuring(True)
        wait_until(qapp, lambda: table.isVisible() and table.height() == view._table_height > 0)

        assert view._graphics.height() == chart
        assert tab.console.height() == console - table.height()

        view.set_measuring(False)
        wait_until(qapp, lambda: not table.isVisible() and tab.console.height() == console)

        assert view._graphics.height() == chart


def test_a_popped_out_plot_grows_its_window_while_measuring(qapp, config, theme_manager):
    from tests_gui.screens import main_window, start_session

    with main_window(qapp, config, theme_manager) as (window, port):
        start_session(qapp, window, port)
        tab = window.current_tab()
        view = tab._plot_view
        tab.set_plot_floating(True)
        _feed(tab._plot_engine)
        view._redraw(force=True)
        wait_until(qapp, lambda: view._graphics.isVisible())
        own = tab.findChild(PlotWindow)
        height = own.height()
        table = view._measure_table

        view.set_measuring(True)
        wait_until(qapp, lambda: table.isVisible() and own.height() == height + view._table_height)
        view.set_measuring(False)
        wait_until(qapp, lambda: not table.isVisible() and own.height() == height)
        tab.set_plot_floating(False)
