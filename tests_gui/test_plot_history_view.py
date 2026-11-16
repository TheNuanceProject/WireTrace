# SPDX-License-Identifier: GPL-3.0-or-later
"""The plot drawing older time than it holds in memory, read from the session recording."""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

import core.plot_engine as plot_engine_module
from app.constants import ThemeID
from core.plot_engine import PlotEngine
from core.records import LineRecord
from core.session_spool import SessionSpool
from tests_gui.support import wait_until
from tests_gui.test_plot_view import _scene_point
from ui.features.plot_history import PlotHistory
from ui.formatting import format_value
from ui.themes.theme_manager import ThemeManager
from ui.widgets.plot_view import (
    _EXTREMES_LIVE_NOTE,
    _FIGURES_LIVE,
    _NOT_EXACT,
    _READING_OLDER_NOTE,
    PlotView,
)

_START = datetime(2026, 5, 6, 7, 8, 9)
_IN_MEMORY = 100
_LINES = 1_000
_SPIKE_AT = 123
_SPIKE = 500.0


def _value(index: int) -> float:
    return _SPIKE if index == _SPIKE_AT else float(index % 17)


@pytest.fixture
def spool(tmp_path):
    instance = SessionSpool(str(tmp_path))
    instance.create()
    yield instance
    instance.delete()


@pytest.fixture
def plot(qapp: QApplication, theme_manager: ThemeManager, spool, monkeypatch):
    """A plot that keeps 100 samples in memory, after 1,000 lines with a spike."""
    monkeypatch.setattr(plot_engine_module, "PLOT_RING_BUFFER_SIZE", _IN_MEMORY)
    theme_manager.apply_theme(ThemeID.LIGHT)
    engine = PlotEngine()
    history = PlotHistory(engine, spool.snapshot_from)
    records = [
        LineRecord(_START + timedelta(seconds=index * 0.1), f"temp={_value(index)}", "DATA")
        for index in range(_LINES)
    ]
    spool.append(records)
    for record in records:
        engine.process(record.text, record.timestamp)
    view = PlotView(engine, theme_manager, history=history)
    view.resize(1000, 520)
    view.show()
    history.set_reading(True)
    view._window_combo.setCurrentIndex(view._window_combo.findData(None))
    qapp.processEvents()
    yield view, engine, history
    view.shutdown()
    history.shutdown()


def _read_everything(qapp: QApplication, view: PlotView, history: PlotHistory) -> None:
    def all_read() -> bool:
        view._redraw()
        overview = history.overview("temp")
        return overview is not None and overview.figures(0, np.inf) is not None \
            and overview.figures(0, np.inf).count == _LINES
    wait_until(qapp, all_read)
    view._redraw()
    view._update_figures()


def test_older_time_is_drawn_from_the_recording_and_keeps_every_spike(qapp, plot):
    view, engine, history = plot
    assert engine.snapshot("temp")[0].size == _IN_MEMORY

    _read_everything(qapp, view, history)

    x, y = view._data["temp"]
    assert x[0] == 0.0
    assert _SPIKE in y.tolist()
    assert view.note_text() == _EXTREMES_LIVE_NOTE
    assert view.value_texts()[0][2] == _FIGURES_LIVE


def test_older_time_shows_no_value_between_its_extremes(qapp, plot):
    view, _, history = plot
    _read_everything(qapp, view, history)

    assert view._held_text("temp", 5.0) == _NOT_EXACT
    assert view._held_text("temp", 95.0) != _NOT_EXACT


def test_held_still_the_older_time_is_read_in_full_with_exact_figures(qapp, plot):
    view, _, history = plot
    _read_everything(qapp, view, history)

    view._pause_btn.setChecked(True)
    wait_until(qapp, lambda: view._detail_for_view() is not None)
    view._update_figures()

    values = np.array([_value(index) for index in range(_LINES)])
    _, last, figures = view.value_texts()[0]
    assert last == format_value(_value(_LINES - 1))
    assert figures == (
        f"min {format_value(values.min())}  max {format_value(values.max())}  "
        f"avg {format_value(values.mean())}"
    )
    assert view.note_text() == ""
    assert view._held_text("temp", 5.0) == "16"
    assert view._data["temp"][0].size == _LINES


def test_zooming_while_held_reads_the_new_time(qapp, plot):
    view, _, history = plot
    _read_everything(qapp, view, history)
    view._pause_btn.setChecked(True)
    wait_until(qapp, lambda: view._detail_for_view() is not None)

    view._panels[0].item.setXRange(10.0, 20.0, padding=0)
    wait_until(qapp, lambda: (detail := view._detail_for_view()) is not None
               and detail.request.low == pytest.approx(10.0))
    view._update_figures()

    expected = [_value(index) for index in range(100, 201)]
    assert view.value_texts()[0][2] == (
        f"min {format_value(min(expected))}  max {format_value(max(expected))}  "
        f"avg {format_value(float(np.mean(expected)))}"
    )


def test_a_new_session_forgets_older_values(qapp, plot):
    view, _, history = plot
    _read_everything(qapp, view, history)

    view.reset_session()
    history.reset()

    assert history.overview("temp") is None
    assert view.note_text() == ""


def test_the_card_under_the_pointer_asks_to_zoom_in_over_older_time(qapp, plot):
    view, _, history = plot
    _read_everything(qapp, view, history)

    view._on_pointer_moved(_scene_point(view, 30.0))

    assert view.hover_texts()[2] == [("temp", _NOT_EXACT)]


def test_until_older_values_are_read_the_view_says_so(qapp, plot):
    view, _, history = plot
    history.set_reading(False)
    history.reset()
    history.sync()

    view._redraw()
    view._update_figures()

    assert view.note_text() == _READING_OLDER_NOTE


def test_a_plot_holding_the_whole_session_needs_no_note(qapp, theme_manager, spool):
    theme_manager.apply_theme(ThemeID.LIGHT)
    engine = PlotEngine()
    history = PlotHistory(engine, spool.snapshot_from)
    records = [
        LineRecord(_START + timedelta(seconds=index), f"temp={index}", "DATA") for index in range(50)
    ]
    spool.append(records)
    for record in records:
        engine.process(record.text, record.timestamp)
    view = PlotView(engine, theme_manager, history=history)
    view.resize(1000, 520)
    view.show()
    history.set_reading(True)
    try:
        view._redraw()
        view._update_figures()

        assert view.note_text() == ""
        assert view.value_texts()[0][2] == "min 0  max 49  avg 24.5"
    finally:
        view.shutdown()
        history.shutdown()
