# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests of the live plot with real pyqtgraph: the value list, panels by
scale, the measuring cursors, and the plot in a window of its own."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import numpy as np
import pytest
from PySide6.QtCore import QEvent, QObject, QPointF, Qt
from PySide6.QtWidgets import QApplication, QFileDialog, QFrame, QPushButton, QWidget

import core.plot_engine as plot_engine_module
from app.config import ConfigManager
from app.constants import (
    NAME_SEPARATOR,
    PLOT_DRAWN_MARGIN_VIEWS,
    PLOT_MAX_PANELS,
    PLOT_REGEX_MAX_CONSECUTIVE_TIMEOUTS,
    ThemeID,
)
from app.port_memory import PortMemory
from core.plot_engine import PlotEngine
from core.plot_thinning import SAMPLES_PER_COLUMN
from tests_gui.support import LoopbackPort, let_events_run, pixel, wait_until
from ui import shortcuts
from ui.device_tab import DeviceTab
from ui.formatting import format_value
from ui.themes import tokens
from ui.themes.theme_manager import ThemeManager
from ui.widgets.banner import BannerArea
from ui.widgets.connect_page import ConnectPage
from ui.widgets.plot_view import PlotExport, PlotLayout, PlotView
from ui.widgets.plot_window import PlotWindow
from ui.widgets.toast import Toast
from updater.usage import Feature

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
    def test_panels_by_scale_is_the_layout_at_first(self, running):
        assert running.layout_mode is PlotLayout.PANELS
        assert _button(running, "Panels by scale").isChecked()
        assert running.panels() == [["RPM"], ["Current", "Voltage"], ["MotorTemp"]]

    def test_one_chart_holds_every_value(self, running):
        _button(running, "One chart").click()

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
        assert running.panels() == [["RPM"], ["Current", "Voltage"], ["MotorTemp"]]


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
        assert [len(panel.cursors) for panel in running._panels] == [2, 2, 2]

    def test_the_time_shown_stays_put_while_held_however_much_arrives(
        self, qapp, running, engine,
    ):
        _button(running, "Measure").click()
        held = running._time_in_view()

        for second in range(_SECONDS + 1, _SECONDS + 30):
            engine.process(
                "RPM: 1460, Current: 2.4, Voltage: 12.5, MotorTemp: 46",
                _START + timedelta(seconds=second),
            )
        running._redraw(force=True)

        assert running._time_in_view() == pytest.approx(held)

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
        assert running.panels() == [["RPM"], ["Current", "Voltage"], ["MotorTemp"]]

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


class TestBreaks:
    """No line joins the samples on either side of a time the board was away."""

    def test_the_line_is_broken_after_the_last_sample_before_the_loss(self, qapp, running):
        lost = _START + timedelta(seconds=_SECONDS - 5.05)

        running.set_breaks([lost])
        running._redraw()

        x, y = running._traces["RPM"].getData()
        (gap,) = np.flatnonzero(np.isnan(y))
        assert x[gap] == x[gap - 1] == pytest.approx(_SECONDS - 5.1)
        assert x[gap + 1] == pytest.approx(_SECONDS - 5.0)
        assert running._traces["RPM"].opts["connect"] == "finite"
        data_x, data_y = running._data["RPM"]
        assert not np.isnan(data_y).any()
        assert data_x.size == x.size - 1

    def test_a_loss_outside_the_samples_breaks_nothing(self, qapp, running):
        running.set_breaks([_START - timedelta(seconds=1), _START + timedelta(seconds=_SECONDS + 1)])
        running._redraw()

        _, y = running._traces["RPM"].getData()
        assert not np.isnan(y).any()

    def test_a_new_session_has_no_breaks(self, qapp, running, engine):
        running.set_breaks([_START + timedelta(seconds=_SECONDS - 5.05)])
        running.reset_session()
        running.set_breaks([])

        _feed(engine)
        running._redraw()

        _, y = running._traces["RPM"].getData()
        assert not np.isnan(y).any()


def _line_at(engine: PlotEngine, seconds: float) -> None:
    engine.process("RPM: 1460, Current: 2.4, Voltage: 12.5, MotorTemp: 46", _START + timedelta(seconds=seconds))


def _seconds_per_pixel(view: PlotView) -> float:
    """The time one pixel of the screen spans, as pyqtgraph measures it."""
    return view._panels[0].item.getViewBox().viewPixelSize()[0] / view._graphics.devicePixelRatioF()


class _HandOverCount:
    """Counts the samples handed to every trace: each hand-over paints the chart again."""

    def __init__(self, view: PlotView, monkeypatch: pytest.MonkeyPatch) -> None:
        self.count = 0
        for trace in view._traces.values():
            monkeypatch.setattr(trace, "setData", self._counting(trace.setData))

    def _counting(self, original):
        def counted(*args, **kwargs):
            self.count += 1
            return original(*args, **kwargs)
        return counted


class _PaintCount(QObject):
    """Counts the paints of a widget."""

    def __init__(self, widget: QWidget) -> None:
        super().__init__(widget)
        self.count = 0
        widget.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Paint:
            self.count += 1
        return False


class TestDrawingCost:
    """A frame is drawn only when the picture would change."""

    @pytest.fixture
    def settled(self, qapp, running) -> PlotView:
        """A live plot whose last tick has drawn everything there is."""
        running._timer.stop()
        running._timer.timeout.emit()
        qapp.processEvents()
        return running

    def test_the_grid_has_major_lines_and_one_level_between(self, running):
        for panel in running._panels:
            for side in ("left", "bottom"):
                assert panel.item.getAxis(side).style["maxTickLevel"] == 1

    def test_a_device_with_nothing_new_draws_nothing(self, qapp, settled, monkeypatch):
        handed = _HandOverCount(settled, monkeypatch)

        for _ in range(3):
            settled._timer.timeout.emit()
            qapp.processEvents()

        assert handed.count == 0

    def test_samples_within_a_pixel_wait_and_a_whole_pixel_is_drawn(
        self, qapp, settled, engine, monkeypatch,
    ):
        handed = _HandOverCount(settled, monkeypatch)
        pixel = _seconds_per_pixel(settled)

        _line_at(engine, _SECONDS + pixel / 4)
        settled._timer.timeout.emit()
        assert handed.count == 0

        _line_at(engine, _SECONDS + pixel * 1.5)
        settled._timer.timeout.emit()
        assert handed.count == len(settled._traces)
        assert settled._data["RPM"][0][-1] == pytest.approx(_SECONDS + pixel * 1.5)

    def test_each_frame_is_painted_once(self, qapp, settled, engine):
        paints = _PaintCount(settled._graphics.viewport())
        frames = 5
        seconds = float(_SECONDS)
        for frame in range(frames):
            seconds += 2 * _seconds_per_pixel(settled)
            engine.process(
                f"RPM: {1460 + 50 * frame}, Current: 2.4, Voltage: 12.5, MotorTemp: 46",
                _START + timedelta(seconds=seconds),
            )
            settled._timer.timeout.emit()
            wait_until(qapp, lambda frame=frame: paints.count > frame)
            let_events_run(qapp)

        assert paints.count == frames

    def test_a_new_time_span_is_drawn_at_the_next_tick_with_nothing_new(self, qapp, settled):
        settled._window_combo.setCurrentIndex(settled._window_combo.findData(10.0))
        settled._timer.timeout.emit()
        qapp.processEvents()
        low, high = settled._time_in_view()

        assert 49 <= low <= 51
        assert high >= _SECONDS

    def test_going_live_puts_the_newest_data_back_with_nothing_new(self, qapp, settled):
        _button(settled, "Pause").click()
        settled._panels[0].item.setXRange(10.0, 20.0, padding=0)
        _button(settled, "Resume").click()
        settled._timer.timeout.emit()
        qapp.processEvents()

        assert settled._time_in_view()[1] >= _SECONDS

    def test_a_loss_while_live_is_drawn_at_the_next_tick(self, qapp, settled):
        settled.set_breaks([_START + timedelta(seconds=_SECONDS - 5.05)])
        settled._timer.timeout.emit()

        _, y = settled._traces["RPM"].getData()
        assert np.isnan(y).any()

    def test_clear_while_live_empties_the_chart_at_once(self, qapp, settled):
        _button(settled, "Clear").click()

        x, _ = settled._traces["RPM"].getData()
        assert x is None or x.size == 0


_DENSE_PER_SECOND = 500
_DENSE_SECONDS = 60
_DENSE_SAMPLES = _DENSE_PER_SECOND * _DENSE_SECONDS


class TestThinnedLines:
    """A line is handed to the chart as at most four samples a pixel column;
    everything read from the plot still comes from every sample."""

    @pytest.fixture
    def dense(self, qapp, theme_manager, monkeypatch) -> PlotView:
        """A held plot of 30,000 samples a value in memory: more than the
        chart has columns for at every display scale."""
        monkeypatch.setattr(plot_engine_module, "PLOT_RING_BUFFER_SIZE", _DENSE_SAMPLES)
        theme_manager.apply_theme(ThemeID.LIGHT)
        engine = PlotEngine()
        engine.set_active(True)
        view = PlotView(engine, theme_manager)
        view.resize(1000, 520)
        view.show()
        qapp.processEvents()
        for index in range(_DENSE_SAMPLES):
            spike = 900 if index % 997 == 0 else 0
            engine.process(
                f"RPM: {1460 + 20 * np.sin(index / 9) + spike:.1f}, Current: 2.4, "
                f"Voltage: 12.5, MotorTemp: 46",
                _START + timedelta(seconds=index / _DENSE_PER_SECOND),
            )
        view._redraw()
        view._timer.stop()
        qapp.processEvents()
        _button(view, "Pause").click()
        view._redraw(force=True)
        qapp.processEvents()
        yield view
        view.shutdown()

    def test_the_chart_is_handed_at_most_four_samples_a_column(self, dense):
        x, _ = dense._traces["RPM"].getData()
        columns = math.ceil(dense._pixels_wide() * (1 + 2 * PLOT_DRAWN_MARGIN_VIEWS))

        assert dense._data["RPM"][0].size == _DENSE_SAMPLES
        assert x.size <= SAMPLES_PER_COLUMN * columns + 4
        assert x.size < _DENSE_SAMPLES

    def test_every_spike_is_drawn_and_read_from_every_sample(self, dense):
        _, drawn = dense._traces["RPM"].getData()
        _, every = dense._data["RPM"]

        assert np.count_nonzero(drawn > 2000) == np.count_nonzero(every > 2000) > 0
        assert dense.value_texts()[0][2].startswith("min ")

    def test_zooming_in_on_a_held_chart_draws_every_sample_there(self, qapp, dense):
        x_all, _ = dense._data["RPM"]
        low, high = float(x_all[-400]), float(x_all[-300])
        dense._panels[0].item.setXRange(low, high, padding=0)
        dense._timer.timeout.emit()
        qapp.processEvents()

        x, _ = dense._traces["RPM"].getData()
        shown = (x_all >= low) & (x_all <= high)
        assert np.isin(x_all[shown], x).all()

    def test_a_small_drag_of_a_held_chart_is_not_drawn_again(self, dense, monkeypatch):
        handed = _HandOverCount(dense, monkeypatch)
        low, high = dense._time_in_view()
        step = (high - low) / 4
        dense._panels[0].item.setXRange(low - step, high - step, padding=0)
        dense._timer.timeout.emit()

        assert handed.count == 0

    def test_a_wider_chart_is_drawn_again_for_its_pixels(self, qapp, dense, monkeypatch):
        handed = _HandOverCount(dense, monkeypatch)
        dense.resize(dense.width() * 2, dense.height())
        wait_until(qapp, lambda: not dense._drawing_fits_view())
        dense._timer.timeout.emit()

        assert handed.count > 0
        assert dense._drawing_fits_view()

    def test_a_wider_live_chart_with_nothing_new_is_drawn_again(self, qapp, dense, monkeypatch):
        _button(dense, "Resume").click()
        dense._timer.timeout.emit()
        qapp.processEvents()
        handed = _HandOverCount(dense, monkeypatch)
        dense.resize(dense.width() * 2, dense.height())
        wait_until(qapp, lambda: not dense._drawing_fits_view())
        dense._timer.timeout.emit()

        assert handed.count > 0
        assert dense._drawing_fits_view()


class TestOwnWindow:
    @pytest.fixture
    def port(self) -> LoopbackPort:
        return LoopbackPort()

    @pytest.fixture
    def tab(self, qapp, config: ConfigManager, theme_manager, port) -> DeviceTab:
        """A connected tab whose plot was opened under the console."""
        theme_manager.apply_theme(ThemeID.LIGHT)
        config.plot_own_window = False
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
        assert tab.plot.engine._active is False

    def test_close_button_closes_the_plot_in_its_own_window(self, qapp, tab):
        tab.set_plot_floating(True)
        window = tab.findChild(PlotWindow)

        tab.findChild(PlotView)._close_btn.click()
        qapp.processEvents()

        assert tab.is_plot_visible() is False
        assert not window.isVisible()

    def test_closing_the_window_closes_the_plot(self, qapp, tab):
        tab.set_plot_floating(True)
        window = tab.findChild(PlotWindow)

        window.close()
        qapp.processEvents()

        assert tab.is_plot_visible() is False
        assert not window.isVisible()
        assert tab.plot.engine._active is False

    def test_a_plot_opens_where_preferences_say(self, qapp, tab, config):
        tab.set_plot_floating(True)
        tab.toggle_plot()

        tab.toggle_plot()
        assert tab.is_plot_floating() is False

        tab.toggle_plot()
        config.plot_own_window = True
        tab.toggle_plot()
        assert tab.is_plot_floating() is True
        assert tab.findChild(PlotWindow).isVisible()

    def test_plot_button_hides_and_shows_the_window(self, qapp, tab, config):
        config.plot_own_window = True
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
        wait_until(qapp, lambda: len(tab.plot.engine.columns) == 2)
        tab.set_plot_floating(True)
        window = tab.findChild(PlotWindow)

        wait_until(qapp, lambda: "2 values" in window.summary_text())
        assert window.summary_text() == "PORT0, connected  ·  2 values, last 60 s shown"

        tab.plot.view._pause_btn.setChecked(True)
        assert window.summary_text().startswith("PORT0, connected  ·  paused  ·  ")
        tab.plot.view._pause_btn.setChecked(False)
        assert window.summary_text().startswith("PORT0, connected  ·  2 values")

        tab.disconnect_device()
        wait_until(qapp, lambda: not tab.is_connected)
        assert window.summary_text().startswith("PORT0, not connected")

    def test_window_says_while_the_tab_waits_for_its_board(self, qapp, tab, port):
        tab.set_plot_floating(True)
        window = tab.findChild(PlotWindow)

        port.transport.drop()
        wait_until(qapp, lambda: tab.is_waiting)
        assert window.summary_text().startswith("PORT0, waiting for the board")

        tab.stop_waiting()
        wait_until(qapp, lambda: not tab.is_connected)
        assert window.summary_text().startswith("PORT0, not connected")

    def test_a_window_opened_while_the_tab_waits_says_so(self, qapp, tab, port):
        port.transport.drop()
        wait_until(qapp, lambda: tab.is_waiting)

        tab.set_plot_floating(True)

        assert tab.findChild(PlotWindow).summary_text().startswith("PORT0, waiting for the board")

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


class TestOwnWindowByDefault:
    @pytest.fixture
    def port(self) -> LoopbackPort:
        return LoopbackPort()

    @pytest.fixture
    def make_tab(self, qapp, config: ConfigManager, theme_manager, port):
        tabs: list[DeviceTab] = []
        theme_manager.apply_theme(ThemeID.LIGHT)

        def factory() -> DeviceTab:
            device_tab = DeviceTab(theme_manager, config, transport_factory=port.factory)
            device_tab.show()
            device_tab.findChild(ConnectPage).connect_requested.emit("PORT0", 115200)
            wait_until(qapp, lambda: device_tab.is_connected)
            tabs.append(device_tab)
            return device_tab

        yield factory
        for device_tab in tabs:
            device_tab.ordered_shutdown()
            device_tab.close()
            device_tab.deleteLater()
        qapp.processEvents()

    def test_the_plot_opens_in_its_own_window(self, qapp, make_tab):
        tab = make_tab()

        tab.toggle_plot()
        window = tab.findChild(PlotWindow)

        assert tab.is_plot_visible() and tab.is_plot_floating()
        assert window.isVisible()
        assert tab._console_plot_splitter.count() == 1

    def test_the_window_opens_where_it_last_was_for_its_port(self, qapp, make_tab, config):
        tab = make_tab()
        tab.toggle_plot()
        window = tab.findChild(PlotWindow)
        window.setGeometry(200, 150, 700, 480)
        qapp.processEvents()
        placed = window.frameGeometry()

        window.close()
        qapp.processEvents()
        assert PortMemory(config).get("PORT0").plot_window

        other = make_tab()
        other.toggle_plot()
        qapp.processEvents()
        reopened = other.findChild(PlotWindow).frameGeometry()

        assert (reopened.width(), reopened.height()) == (placed.width(), placed.height())
        assert (reopened.x(), reopened.y()) == (placed.x(), placed.y())

    def test_closing_the_tab_remembers_where_the_window_was(self, qapp, make_tab, config):
        tab = make_tab()
        tab.toggle_plot()
        qapp.processEvents()

        tab.ordered_shutdown()

        assert PortMemory(config).get("PORT0").plot_window


def test_measuring_takes_room_from_the_console_never_from_the_plot(qapp, config, theme_manager):
    from tests_gui.screens import main_window, start_session

    with main_window(qapp, config, theme_manager) as (window, port):
        start_session(qapp, window, port)
        window.resize(window.width(), 1)
        tab = window.current_tab()
        view = tab.plot.view
        _feed(tab.plot.engine)
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
        view = tab.plot.view
        _feed(tab.plot.engine)
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


class _WheelOver:
    """A scroll of the wheel, one notch towards the user, at ``position`` in a chart."""

    _NOTCH = 120

    def __init__(self, position) -> None:
        self._position = position

    def delta(self) -> int:
        return self._NOTCH

    def pos(self):
        return self._position

    def accept(self) -> None:
        pass

    def ignore(self) -> None:
        pass


class _RightClick:
    """A click of the right mouse button in a chart."""

    def button(self):
        return Qt.MouseButton.RightButton

    def accept(self) -> None:
        pass

    def ignore(self) -> None:
        pass


class TestLiveZoom:
    """While live, the wheel changes how much recent time is shown and the
    plot stays on the newest data; dragging back holds it; Fit goes back."""

    @staticmethod
    def _wheel(view: PlotView, notches: int) -> None:
        box = view._panels[-1].item.getViewBox()
        wheel = _WheelOver(box.boundingRect().center())
        wheel.delta = lambda: _WheelOver._NOTCH * notches
        box.wheelEvent(wheel)

    @staticmethod
    def _time_shown(qapp, view: PlotView) -> tuple[float, float]:
        view._redraw()
        qapp.processEvents()
        return view._time_in_view()

    def test_the_wheel_shows_less_recent_time_and_stays_on_the_newest(
        self, qapp, running, engine,
    ):
        self._wheel(running, 2)

        span = running._window_seconds
        assert span < 60
        assert running._window_combo.currentText() == "Custom"
        assert not running.paused
        low, high = self._time_shown(qapp, running)
        assert high >= engine.latest_x and high - low < 60

        _feed(engine, 70)
        low, high = self._time_shown(qapp, running)
        assert high >= engine.latest_x
        assert running.summary() == f"4 values, last {format_value(span)} s shown"

    def test_zooming_out_past_everything_held_shows_all(self, qapp, running):
        self._wheel(running, -20)

        assert running._window_seconds is None
        assert running._window_combo.currentText() == "All"
        assert running._window_combo.findText("Custom") < 0

    def test_zooming_from_all_keeps_following(self, qapp, running, engine):
        running._window_combo.setCurrentIndex(running._window_combo.findText("All"))
        self._wheel(running, 3)

        assert running._window_seconds < engine.latest_x
        _feed(engine, 70)
        assert self._time_shown(qapp, running)[1] >= engine.latest_x

    def test_dragging_back_holds_the_plot_and_fit_goes_live(self, qapp, running):
        import pyqtgraph as pg

        self._wheel(running, 1)
        box = running._panels[0].item.getViewBox()

        class _Drag:
            def button(self):
                return Qt.MouseButton.LeftButton

            def isStart(self):
                return True

            def isFinish(self):
                return False

            # Towards the right, back in time, by a tenth of the chart.
            def pos(self):
                return pg.Point(box.boundingRect().center()) + pg.Point(box.width() / 10, 0)

            def lastPos(self):
                return pg.Point(box.boundingRect().center())

            def buttonDownPos(self, _button=None):
                return pg.Point(box.boundingRect().center())

            def accept(self):
                pass

            def ignore(self):
                pass

        before = running._time_in_view()
        box.mouseDragEvent(_Drag())
        assert running.paused
        qapp.processEvents()
        assert running._time_in_view()[1] < before[1]

        running.fit()

        assert not running.paused
        assert running._window_seconds == 60
        assert running._window_combo.currentText() == "60 s"
        assert running._window_combo.findText("Custom") < 0

    def test_a_choice_from_the_list_ends_the_custom_span(self, qapp, running):
        self._wheel(running, 2)

        running._window_combo.setCurrentIndex(running._window_combo.findText("10 s"))

        assert running._window_seconds == 10
        assert running._window_combo.findText("Custom") < 0
        running.fit()
        assert running._window_seconds == 10

    def test_with_less_held_than_the_span_chosen_the_wheel_zooms_in_at_once(
        self, qapp, view, engine,
    ):
        _feed(engine, 10)
        view._redraw()
        qapp.processEvents()
        assert view._window_seconds == 60

        self._wheel(view, 1)

        assert view._window_combo.currentText() == "Custom"
        assert view._window_seconds < engine.latest_x

    def test_fit_after_the_wheel_reached_all_gives_back_the_choice(self, qapp, running):
        self._wheel(running, 2)
        self._wheel(running, -40)
        assert running._window_combo.currentText() == "All"

        running.fit()

        assert running._window_seconds == 60
        assert running._window_combo.currentText() == "60 s"

    def test_all_chosen_from_the_list_is_what_fit_gives_back(self, qapp, running):
        running._window_combo.setCurrentIndex(running._window_combo.findText("All"))
        self._wheel(running, 2)

        running.fit()

        assert running._window_seconds is None
        assert running._window_combo.currentText() == "All"

    def test_the_time_list_shows_custom_whole_and_keeps_its_width(self, qapp, running):
        from PySide6.QtWidgets import QStyle, QStyleOptionComboBox

        combo = running._window_combo
        width = combo.width()

        self._wheel(running, 2)
        qapp.processEvents()

        option = QStyleOptionComboBox()
        combo.initStyleOption(option)
        field = combo.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox, option,
            QStyle.SubControl.SC_ComboBoxEditField, combo,
        )
        assert combo.currentText() == "Custom"
        assert field.width() >= combo.fontMetrics().horizontalAdvance("Custom")
        assert combo.width() == width

    def test_a_held_plot_zooms_where_the_pointer_is(self, qapp, running):
        running._pause_btn.setChecked(True)

        self._wheel(running, 2)

        assert running._window_seconds == 60
        assert running._window_combo.findText("Custom") < 0


class TestZoom:
    """The wheel and dragging act on time only; every scale fits the values shown."""

    @staticmethod
    def _held_plot(qapp, window, port):
        import math

        from tests_gui.screens import start_session

        start_session(qapp, window, port)
        tab = window.current_tab()
        view = tab.plot.view
        port.feed(b"".join(
            b"rpm: %d, temp: %.2f\n" % (1000 + 300 * math.sin(n / 50), 50 + 5 * math.sin(n / 70))
            for n in range(3000)
        ))
        wait_until(qapp, lambda: len(tab.plot.engine.columns) >= 2)
        view._redraw(force=True)
        wait_until(qapp, lambda: view._graphics.isVisible() and len(view._panels) >= 2)
        view._pause_btn.setChecked(True)
        qapp.processEvents()
        return view

    @staticmethod
    def _shown_values(view, panel, low: float, high: float) -> tuple[float, float]:
        values = []
        for name in panel.names:
            x, y = view._data[name]
            inside = (x >= low) & (x <= high)
            values.extend(y[inside].tolist())
        return min(values), max(values)

    def test_every_panel_takes_only_time_from_the_mouse(self, qapp, config, theme_manager):
        from tests_gui.screens import main_window

        with main_window(qapp, config, theme_manager) as (window, port):
            view = self._held_plot(qapp, window, port)

            for panel in view._panels:
                assert panel.item.getViewBox().mouseEnabled() == [True, False]

    def test_no_panel_offers_the_libraries_own_menu_or_buttons(
        self, qapp, config, theme_manager, monkeypatch,
    ):
        from tests_gui.screens import main_window

        with main_window(qapp, config, theme_manager) as (window, port):
            view = self._held_plot(qapp, window, port)
            raised: list[object] = []

            for panel in view._panels:
                box = panel.item.getViewBox()
                monkeypatch.setattr(box, "raiseContextMenu", raised.append)
                assert panel.item.menuEnabled() is False
                assert box.menuEnabled() is False
                assert panel.item.buttonsHidden is True
                # A right-click on the chart, and on its value axis.
                box.mouseClickEvent(_RightClick())
                panel.item.getAxis("left").mouseClickEvent(_RightClick())

            assert raised == []

    def test_zooming_over_one_panel_keeps_every_scale_on_the_values_shown(
        self, qapp, config, theme_manager,
    ):
        from tests_gui.screens import main_window

        with main_window(qapp, config, theme_manager) as (window, port):
            view = self._held_plot(qapp, window, port)
            boxes = [panel.item.getViewBox() for panel in view._panels]
            low, high = boxes[0].viewRange()[0]

            # The library's own wheel handling, over a sample in the middle
            # of the session: lines arrive in bursts, so the chart zooms
            # where there are samples to fit.
            x, _ = view._data[view._panels[-1].names[0]]
            middle = float(np.median(x))
            wheel = _WheelOver(boxes[-1].mapFromView(QPointF(middle, boxes[-1].viewRange()[1][0])))
            for _ in range(10):
                boxes[-1].wheelEvent(wheel)

            def fitted() -> bool:
                shown_low, shown_high = boxes[0].viewRange()[0]
                for panel, box in zip(view._panels, boxes, strict=True):
                    least, greatest = self._shown_values(view, panel, shown_low, shown_high)
                    scale_low, scale_high = box.viewRange()[1]
                    # Fitted to what is shown, with the library's small margin.
                    if not (scale_low <= least and greatest <= scale_high
                            and scale_high - scale_low <= (greatest - least) * 1.25 + 1e-9):
                        return False
                return True

            # The library fits each scale on a later turn of the event loop.
            wait_until(qapp, fitted)
            shown_low, shown_high = boxes[0].viewRange()[0]
            assert shown_high - shown_low < (high - low) / 10


def test_a_popped_out_plot_keeps_its_window_size_while_measuring(qapp, config, theme_manager):
    from tests_gui.screens import main_window, start_session

    with main_window(qapp, config, theme_manager) as (window, port):
        start_session(qapp, window, port)
        tab = window.current_tab()
        view = tab.plot.view
        tab.set_plot_floating(True)
        _feed(tab.plot.engine)
        view._redraw(force=True)
        wait_until(qapp, lambda: view._graphics.isVisible())
        own = tab.findChild(PlotWindow)
        size = own.size()
        chart = view._graphics.height()
        table = view._measure_table

        view.set_measuring(True)
        wait_until(qapp, lambda: table.isVisible() and view._graphics.height() < chart)
        assert own.size() == size

        view.set_measuring(False)
        wait_until(qapp, lambda: not table.isVisible() and view._graphics.height() == chart)
        assert own.size() == size
        tab.set_plot_floating(False)


def test_measuring_never_lets_windows_resize_the_plot_window_to_its_content(
    qapp, config, theme_manager,
):
    """On Windows, Qt corrects a window whose height depends on its width
    to the preferred height of its content at every move and resize
    (``QWidgetWindowPrivate::closestAcceptableGeometry`` asks the window's
    ``hasHeightForWidth``, then ``QLayout.closestAcceptableSize``). The
    measuring table makes the layout depend on its width; the window
    must not say so."""
    from PySide6.QtCore import QSize
    from PySide6.QtWidgets import QLayout

    from tests_gui.screens import main_window, start_session

    with main_window(qapp, config, theme_manager) as (window, port):
        start_session(qapp, window, port)
        tab = window.current_tab()
        view = tab.plot.view
        tab.set_plot_floating(True)
        _feed(tab.plot.engine)
        view._redraw(force=True)
        wait_until(qapp, lambda: view._graphics.isVisible())
        own = tab.findChild(PlotWindow)
        own.resize(own.minimumWidth() + 200, own.minimumHeight())
        wait_until(qapp, lambda: own.height() == own.minimumHeight())

        view.set_measuring(True)
        wait_until(qapp, lambda: view._measure_table.isVisible())
        asked = own.size()
        demanded = QLayout.closestAcceptableSize(own, QSize(asked.width(), asked.height()))

        # The content would ask for more than the window has...
        assert own.layout().hasHeightForWidth()
        assert demanded.height() > asked.height()
        # ...and the window never lets Qt act on it.
        assert own.hasHeightForWidth() is False
        assert own.size() == asked
        tab.set_plot_floating(False)


def test_qt_itself_sees_that_the_plot_window_has_no_height_for_width(qapp):
    from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

    plot_window = PlotWindow("COM3")
    plot_window.setWindowFlag(Qt.WindowType.Window, False)
    wrapped = QLabel("a long line of text that wraps " * 10)
    wrapped.setWordWrap(True)
    plot_window.layout().insertWidget(0, wrapped)
    host = QWidget()
    host_layout = QVBoxLayout(host)
    host_layout.addWidget(plot_window)

    assert plot_window.layout().hasHeightForWidth()
    # Asked through Qt's own layout code, which calls the window's method.
    assert host_layout.itemAt(0).hasHeightForWidth() is False
    host.deleteLater()


def test_a_popped_out_plot_opens_at_the_size_it_was_left(qapp, config, theme_manager):
    from tests_gui.screens import main_window, start_session

    with main_window(qapp, config, theme_manager) as (window, port):
        start_session(qapp, window, port)
        tab = window.current_tab()
        view = tab.plot.view
        tab.set_plot_floating(True)
        own = tab.findChild(PlotWindow)
        wait_until(qapp, lambda: own.isVisible())
        own.resize(own.minimumWidth() + 40, own.minimumHeight() + 30)
        wait_until(qapp, lambda: own.height() == own.minimumHeight() + 30)
        left = own.size()

        tab.toggle_plot()
        wait_until(qapp, lambda: not own.isVisible())
        tab.toggle_plot()
        wait_until(qapp, lambda: own.isVisible())
        # Values arrive, the panels are built, and the note appears as
        # the window is shown: it takes its room from the chart.
        _feed(tab.plot.engine)
        view._redraw(force=True)
        wait_until(qapp, lambda: view._graphics.isVisible())
        view.height_wanted.emit(tokens.METRICS.status_height)
        for _ in range(10):
            qapp.processEvents()

        assert own.size() == left
        tab.set_plot_floating(False)


class TestSaving:
    @pytest.fixture
    def port(self) -> LoopbackPort:
        return LoopbackPort()

    @pytest.fixture
    def tab(self, qapp, config: ConfigManager, theme_manager, port) -> DeviceTab:
        """A connected tab whose plot, under the console, has found two values."""
        theme_manager.apply_theme(ThemeID.LIGHT)
        config.plot_own_window = False
        device_tab = DeviceTab(theme_manager, config, transport_factory=port.factory)
        device_tab.resize(1100, 700)
        device_tab.show()
        device_tab.findChild(ConnectPage).connect_requested.emit("PORT0", 115200)
        wait_until(qapp, lambda: device_tab.is_connected)
        device_tab.toggle_plot()
        port.feed(b"".join(b"temp: %d, rpm: %d\n" % (n, n * 2) for n in range(60)))
        wait_until(qapp, lambda: len(device_tab.plot.engine.columns) == 2)
        view = device_tab.plot.view
        wait_until(qapp, lambda: bool(view._panels) and view._graphics.isVisible())
        yield device_tab
        device_tab.ordered_shutdown()

    @staticmethod
    def _choose(monkeypatch, path) -> list[str]:
        suggested: list[str] = []

        def ask(parent, title, start, filters):
            suggested.append(start)
            return str(path), ""

        monkeypatch.setattr(QFileDialog, "getSaveFileName", ask)
        return suggested

    @pytest.mark.parametrize(("kind", "start"), [
        (PlotExport.PNG, b"\x89PNG"), (PlotExport.SVG, b"<?xml"),
    ])
    def test_the_chart_is_saved_as_a_picture(self, qapp, tab, monkeypatch, tmp_path, kind, start):
        path = tmp_path / f"chart.{kind.name.lower()}"
        suggested = self._choose(monkeypatch, path)
        noted: list[object] = []
        tab.usage_noted.connect(noted.append)

        tab.plot.view.export_requested.emit(kind)

        assert Feature.PLOT_EXPORT in noted
        assert path.read_bytes().startswith(start)
        assert suggested[0].endswith(f".{kind.name.lower()}")
        assert Toast.of(tab.plot.view).message == f"Saved the plot: {path.name}"

    def test_the_values_of_the_whole_session_are_saved(self, qapp, tab, monkeypatch, tmp_path):
        path = tmp_path / "values.csv"
        self._choose(monkeypatch, path)

        tab.plot.view.export_requested.emit(PlotExport.VALUES_ALL)
        wait_until(qapp, lambda: Toast.of(tab.plot.view).message.startswith("Saved"))

        rows = [line for line in path.read_text().splitlines() if not line.startswith("#")]
        assert rows[0] == "Timestamp,Seconds,temp,rpm"
        assert [row.split(",")[2:] for row in rows[1:]] == [
            [f"{float(n)}", f"{float(n * 2)}"] for n in range(60)
        ]
        assert Toast.of(tab.plot.view).message == "Saved 60 rows of plot values: values.csv"

    def test_a_picture_that_cannot_be_written_is_reported(self, qapp, tab, monkeypatch, tmp_path):
        folder = tmp_path / "taken.png"
        folder.mkdir()
        self._choose(monkeypatch, folder)

        tab.plot.view.export_requested.emit(PlotExport.PNG)

        banner = tab.findChild(BannerArea).banner("export")
        assert banner.title_label.text() == "The plot could not be saved"
