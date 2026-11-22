# SPDX-License-Identifier: GPL-3.0-or-later
"""The live plot: the values of structured lines drawn against time.

The view owns no data. On a timer it asks a ``PlotEngine`` for the
samples of each value and draws them. Beside the chart it lists every
value with its last, least, greatest and mean figure over the time
shown. It can draw all values in one chart or in panels by scale, where
values of similar size share a panel so that a small value is not
flattened by a large one.

Under the pointer, a line marks the nearest sample received and a card
beside it gives its time and every value then. Measure holds the plot
still and puts two cursors, A and B, on received samples; a table gives
both moments, every value at each, and B minus A. Every figure is a
value as received, never one worked out between two samples.

The view is built on first use, which may be after the engine has
found the values and collected samples; it then starts from that state.

Older time than the samples in memory comes from the plot's history
(``ui/features/plot_history.py``), read from the session recording.
While live, it is drawn by the least and greatest value of each slice
of time, so no spike is lost. Once the plot is held still, the time on
screen is read again in full: every sample when there is room for each,
or the least and greatest per pixel, with exact figures. Where only the
least and greatest are drawn, the view says so and reads no value from
between them: the card and the cursors ask to zoom in instead.

All parsing, detection and buffering is in ``core/plot_engine.py``; the
figures are worked out in ``core/plot_stats.py``.
"""

from __future__ import annotations

import contextlib
import enum
import logging
import math
from collections.abc import Callable
from typing import TYPE_CHECKING

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import (
    QBuffer,
    QEvent,
    QIODevice,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QHideEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QResizeEvent,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)

from app._atomic import atomic_write_bytes
from app.constants import (
    PLOT_DEFAULT_WINDOW_SECONDS,
    PLOT_DRAWN_MARGIN_VIEWS,
    PLOT_HISTORY_DETAIL_DELAY_MS,
    PLOT_LIVE_SPAN_MIN_SECONDS,
    PLOT_REDRAW_INTERVAL_MS,
    PLOT_STATS_INTERVAL_MS,
    PLOT_WINDOW_CHOICES,
)
from app.diagnostic_log import log_data_failure
from core.plot_history import DetailRequest
from core.plot_stats import (
    SeriesStats,
    group_by_scale,
    magnitude,
    nearest_sample,
    stats_after,
    value_held,
    visible_stats,
)
from core.plot_thinning import points_to_draw
from ui import shortcuts
from ui.formatting import (
    format_change,
    format_count,
    format_received,
    format_time_of_day,
    format_value,
)
from ui.themes import tokens
from ui.themes.fonts import monospace_font
from ui.themes.icons import bind_icon
from ui.tour.targets import Target, mark
from ui.widgets.button import Button, join_buttons
from ui.widgets.styling import SECONDARY, SMALL, plain_label, plain_tooltip, set_compact
from ui.widgets.vertical_scroll import VerticalScrollArea

if TYPE_CHECKING:
    from datetime import datetime

    from core.plot_engine import PlotEngine
    from core.plot_history import DetailResult
    from ui.features.plot_history import PlotHistory
    from ui.themes.theme_manager import ThemeManager

logger = logging.getLogger(__name__)

_WHEEL_STEP = 1.02
_CUSTOM_WINDOW = "Custom"
#: How strongly the grid is drawn, from 0 to 1: part of the drawing of the
#: chart, named as such by the design language, like the trace colours.
_GRID_ALPHA = 0.3
#: The finest level of grid lines drawn: 0 is the major lines alone, 1 adds
#: one level between them. pyqtgraph's own 2 adds a third level, about 50
#: lines an axis, which crowds the chart and costs most of each frame,
#: since the grid is drawn again whenever the time shown moves.
_GRID_LEVEL = 1
#: Space left beside the newest sample, as a share of the time shown.
_X_PADDING = 0.02
#: A cursor's letter sits at the top end of its line (1 on a scale from
#: the bottom, 0) and hangs below that point from the middle of its own
#: top edge, so that it is whole however short the panel is.
_CURSOR_LABEL_POSITION = 1.0
_CURSOR_LABEL_ANCHOR = (0.5, 0.0)
#: Where the two cursors are put when measuring starts, as shares of the time shown.
_CURSOR_START = (1 / 3, 2 / 3)
#: The rows of the measuring table, after its heading.
_MEASURE_ROWS = ("A", "B", "B minus A")
_TIME_HEADING = "Time"
_MEASURE_HINT = (
    "Drag A or B, or click the chart to move the nearer one. "
    "Measuring holds the plot; Resume goes back to live."
)

_WAITING_TEXT = "Waiting for lines that hold values"
_PATTERN_STALLED_TEXT = (
    "The pattern takes too long on the lines being received, so plotting has stopped. "
    "Change the pattern in Configure plot."
)
_NOT_FOUND_TEXT = (
    "No values were found in the lines received so far. "
    "You can describe the format of your lines yourself."
)
_NO_FIGURE = "no data"
#: In place of a value where only the least and greatest are drawn.
_NOT_EXACT = "zoom in"

_EXTREMES_LIVE_NOTE = (
    "Older time is drawn by its least and greatest values. Pause to read every value."
)
_EXTREMES_HELD_NOTE = (
    "This time is drawn by its least and greatest values. Zoom in to read every value."
)
_READING_NOTE = "Reading every value of this time from the session recording."
_READING_OLDER_NOTE = "Reading older values from the session recording."
_INCOMPLETE_NOTE = (
    "Not every line of this time is still held, or there is too much to read at once. "
    "Zoom in to read every value."
)
_START_LOST_NOTE = (
    "The start of this session is no longer available; "
    "the plot begins with the oldest data still held."
)
_STALLED_HISTORY_NOTE = (
    "Older values cannot be read: the pattern takes too long on the recorded lines."
)
#: The figures beside a value, until every value of the time shown is known.
_FIGURES_LIVE = "Pause for min, max and avg"
_FIGURES_READING = "Reading min, max and avg"
_FIGURES_ZOOM = "Zoom in for min, max and avg"


class PlotLayout(enum.Enum):
    """How the values are arranged."""

    ONE_CHART = "one"
    PANELS = "panels"


class PlotExport(enum.Enum):
    """What the Export menu saves, with the words of its entry."""

    PNG = "Picture (PNG)..."
    SVG = "Picture (SVG)..."
    VALUES_SHOWN = "Values of the time shown (CSV)..."
    VALUES_ALL = "Values of the whole session (CSV)..."


class _Swatch(QWidget):
    """A short line in the colour of a value's trace."""

    def __init__(self, colour: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._colour = colour
        metrics = tokens.METRICS
        self.setFixedSize(metrics.plot_swatch_width, metrics.indicator)

    def set_colour(self, colour: str) -> None:
        self._colour = colour
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(self._colour))
        pen.setWidthF(tokens.METRICS.mark)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        middle = self.height() / 2
        inset = pen.widthF()
        painter.drawLine(int(inset), int(middle), int(self.width() - inset), int(middle))
        painter.end()


class ValueRow(QFrame):
    """One value in the list beside the chart.

    Signals:
        shown_changed(str, bool):  The value was ticked or unticked.
    """

    shown_changed = Signal(str, bool)

    def __init__(self, name: str, colour: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("plotValueRow")
        self.name = name
        metrics = tokens.METRICS
        mono = monospace_font(tokens.TYPOGRAPHY.console_family)

        self._swatch = _Swatch(colour)
        self._check = QCheckBox(name)
        self._check.setChecked(True)
        self._check.setToolTip(plain_tooltip(f"{name}\nShow or hide this value in the chart"))
        self._check.toggled.connect(lambda shown: self.shown_changed.emit(self.name, shown))
        self._last = plain_label(_NO_FIGURE)
        self._last.setObjectName("plotValueLast")
        self._last.setFont(mono)
        self._last.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._figures = plain_label()
        self._figures.setProperty(SECONDARY, True)
        self._figures.setProperty(SMALL, True)
        # The name gives way to the value: a long name is shortened and
        # can be read in full in the tooltip.
        self._check.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

        top = QHBoxLayout()
        top.setSpacing(metrics.space_2)
        top.addWidget(self._swatch)
        top.addWidget(self._check, 1)
        top.addWidget(self._last)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(metrics.space_3, metrics.space_2, metrics.space_3, metrics.space_2)
        layout.setSpacing(metrics.space_half)
        layout.addLayout(top)
        layout.addWidget(self._figures)
        self.set_stats(None)

    @property
    def shown(self) -> bool:
        return self._check.isChecked()

    def set_shown(self, shown: bool) -> None:
        self._check.setChecked(shown)

    def set_colour(self, colour: str) -> None:
        self._swatch.set_colour(colour)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        metrics = tokens.METRICS
        room = self._check.width() - metrics.indicator - metrics.space_3
        self._check.setText(
            self._check.fontMetrics().elidedText(self.name, Qt.TextElideMode.ElideRight, room)
        )

    def set_stats(self, stats: SeriesStats | None) -> None:
        """Show the figures of the time in view, or that there are none."""
        if stats is None:
            self._last.setText(_NO_FIGURE)
            self._figures.setText("")
            return
        self._last.setText(format_value(stats.last))
        self._figures.setText(
            f"min {format_value(stats.minimum)}  max {format_value(stats.maximum)}  "
            f"avg {format_value(stats.mean)}"
        )

    def set_last_only(self, last: float | None, why: str) -> None:
        """Show the last value only, and why the other figures are not shown."""
        self._last.setText(_NO_FIGURE if last is None else format_value(last))
        self._figures.setText(why)

    def texts(self) -> tuple[str, str, str]:
        """Name, last value and the min/max/avg line as shown."""
        return self.name, self._last.text(), self._figures.text()


def _figure_label(text: str = "") -> QLabel:
    """A figure: in the console's fixed-width font, right-aligned, so
    figures in a column line up digit under digit."""
    label = plain_label(text)
    label.setFont(monospace_font(tokens.TYPOGRAPHY.console_family))
    label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return label


class _Readout(QFrame):
    """The card beside the pointer: one received moment, and every value then.

    It floats over the chart and lets the pointer through, so the chart
    still takes every move, drag and scroll.
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("plotReadout")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        metrics = tokens.METRICS
        self._time = _figure_label()
        self._time.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._since = _figure_label()
        self._since.setProperty(SECONDARY, True)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(
            metrics.space_3, metrics.space_2, metrics.space_3, metrics.space_2,
        )
        self._grid.setHorizontalSpacing(metrics.space_3)
        self._grid.setVerticalSpacing(metrics.space_1)
        self._grid.addWidget(self._time, 0, 0, 1, 2)
        self._grid.addWidget(self._since, 0, 2)
        self._rows: list[tuple[_Swatch, QLabel, QLabel]] = []
        self.hide()

    def set_moment(self, time: str, since: str, values: list[tuple[str, str, str]]) -> None:
        """Show ``time`` and, for each ``(colour, name, value)``, the value then."""
        self._time.setText(time)
        self._since.setText(since)
        while len(self._rows) > len(values):
            for widget in self._rows.pop():
                self._grid.removeWidget(widget)
                widget.deleteLater()
        while len(self._rows) < len(values):
            row = len(self._rows) + 1
            swatch, name, value = _Swatch(""), plain_label(), _figure_label()
            name.setProperty(SECONDARY, True)
            self._grid.addWidget(swatch, row, 0)
            self._grid.addWidget(name, row, 1)
            self._grid.addWidget(value, row, 2)
            self._rows.append((swatch, name, value))
        for (swatch, name, value), (colour, text, figure) in zip(self._rows, values, strict=True):
            swatch.set_colour(colour)
            name.setText(text)
            value.setText(figure)
        self.adjustSize()

    def texts(self) -> tuple[str, str, list[tuple[str, str]]]:
        """The time, the time on the chart's scale, and each name with its value."""
        return (
            self._time.text(), self._since.text(),
            [(name.text(), value.text()) for _, name, value in self._rows],
        )


class _Note(QFrame):
    """A line under the toolbar saying what the chart cannot show in full."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("plotNote")
        metrics = tokens.METRICS
        self._label = plain_label()
        self._label.setProperty(SECONDARY, True)
        self._label.setProperty(SMALL, True)
        self._label.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(metrics.space_3, metrics.space_2, metrics.space_3, metrics.space_2)
        layout.setSpacing(0)
        layout.addWidget(self._label)
        self.hide()

    def text(self) -> str:
        return self._label.text() if not self.isHidden() else ""

    def show_text(self, text: str) -> None:
        """Show ``text``, or hide when it is empty."""
        if text != self._label.text() or self.isHidden() == bool(text):
            self._label.setText(text)
            self.setHidden(not text)


class _SidewaysScroll(QScrollArea):
    """Scrolls its content sideways. As tall as the content, and as its
    scroll bar too when the content is wider than the room.

    The height follows the content whenever its layout changes, the way
    ``VerticalScrollArea`` follows its page's width.
    """

    def __init__(self, content: QWidget) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setWidget(content)

    def _height(self) -> int:
        content = self.widget().sizeHint()
        bar = self.horizontalScrollBar().sizeHint().height()
        return content.height() + (bar if content.width() > self.viewport().width() else 0)

    def sizeHint(self) -> QSize:
        return QSize(super().sizeHint().width(), self._height())

    def minimumSizeHint(self) -> QSize:
        return QSize(super().minimumSizeHint().width(), self._height())

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        handled = super().eventFilter(watched, event)
        if watched is self.widget() and event.type() == QEvent.Type.LayoutRequest:
            self.updateGeometry()
        return handled

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        # Narrower or wider, the scroll bar may be needed or not.
        self.updateGeometry()


class _MeasureTable(QFrame):
    """The measuring cursors' figures.

    One column for the time and one for each value shown; one row for A,
    one for B and one for B minus A. Many values scroll sideways.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("plotMeasure")
        metrics = tokens.METRICS
        self._columns: tuple[tuple[str | None, str], ...] = ()
        self._figures: list[list[QLabel]] = []
        self._content = QWidget()
        self._content.setObjectName("plotMeasureGrid")
        self._grid = QGridLayout(self._content)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(metrics.space_6)
        self._grid.setVerticalSpacing(metrics.space_1)
        self._scroll = _SidewaysScroll(self._content)
        self._scroll.setObjectName("plotMeasureScroll")
        self._hint = plain_label(_MEASURE_HINT)
        self._hint.setProperty(SECONDARY, True)
        self._hint.setProperty(SMALL, True)
        self._hint.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            metrics.space_3, metrics.space_2, metrics.space_3, metrics.space_2,
        )
        layout.setSpacing(metrics.space_2)
        layout.addWidget(self._scroll)
        layout.addWidget(self._hint)

    def set_figures(self, columns: list[tuple[str | None, str]], rows: list[list[str]]) -> None:
        """Show ``rows`` (A, B, B minus A) under ``columns`` ((colour, name) each)."""
        if tuple(columns) != self._columns:
            self._build(tuple(columns))
        for labels, texts in zip(self._figures, rows, strict=True):
            for label, text in zip(labels, texts, strict=True):
                label.setText(text)

    def texts(self) -> list[list[str]]:
        """The heading and each row, as shown."""
        heading = ["", *(name for _, name in self._columns)]
        return [heading, *([label.text() for label in labels] for labels in self._figures)]

    def _build(self, columns: tuple[tuple[str | None, str], ...]) -> None:
        while self._grid.count():
            widget = self._grid.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        self._columns = columns
        metrics = tokens.METRICS
        for column, (colour, name) in enumerate(columns, start=1):
            heading = QWidget()
            row = QHBoxLayout(heading)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(metrics.space_2)
            row.addStretch(1)
            if colour is not None:
                row.addWidget(_Swatch(colour))
            label = plain_label(name)
            label.setObjectName("plotMeasureHeading")
            row.addWidget(label)
            self._grid.addWidget(heading, 0, column)
        self._figures = []
        for index, title in enumerate(_MEASURE_ROWS, start=1):
            label = plain_label(title)
            label.setObjectName("plotMeasureHeading")
            self._grid.addWidget(label, index, 0)
            figures = [label]
            for column in range(1, len(columns) + 1):
                figure = _figure_label()
                self._grid.addWidget(figure, index, column)
                figures.append(figure)
            self._figures.append(figures)
        self._grid.setColumnStretch(len(columns) + 1, 1)
        # Shown now, not on Qt's next turn, so the first height measured
        # is the table's whole height.
        for index in range(self._grid.count()):
            widget = self._grid.itemAt(index).widget()
            if widget is not None:
                widget.show()


class _TimeViewBox(pg.ViewBox):
    """The view of one chart. While the plot is live, the wheel asks for
    more or less recent time and dragging asks to hold the plot; while it
    is held, both move and zoom time as usual.

    Signals:
        zoom_asked(float):  The wheel turned while live: the factor by which
                            to scale the recent time shown.
        hold_asked():       A drag began while live.
    """

    zoom_asked = Signal(float)
    hold_asked = Signal()

    def __init__(self, live: Callable[[], bool]) -> None:
        super().__init__()
        self._live = live

    def wheelEvent(self, ev: object, axis: int | None = None) -> None:
        if not self._live():
            super().wheelEvent(ev, axis)
            return
        # The same step per notch as pyqtgraph's own zoom.
        self.zoom_asked.emit(_WHEEL_STEP ** (ev.delta() * self.state["wheelScaleFactor"]))
        ev.accept()

    def mouseDragEvent(self, ev: object, axis: int | None = None) -> None:
        if self._live() and ev.button() == Qt.MouseButton.LeftButton and ev.isStart():
            self.hold_asked.emit()
        super().mouseDragEvent(ev, axis)


class _Panel:
    """One chart: a pyqtgraph plot item, its traces, the line under the
    pointer with a dot on each trace, and its two cursor lines."""

    def __init__(
        self, item: pg.PlotItem, names: list[str],
        hover: pg.InfiniteLine, dots: dict[str, pg.ScatterPlotItem],
    ) -> None:
        self.item = item
        self.names = names
        self.hover = hover
        self.dots = dots
        self.cursors: list[pg.InfiniteLine] = []


class PlotView(QWidget):
    """Live plot with a list of values, panels by scale and measuring cursors.

    Signals:
        pause_toggled(bool):     The plot was paused (True) or resumed.
        configure_requested():   The user asked for the Configure plot dialog.
        float_requested(bool):   The user asked for the plot to open in its
                                 own window (True) or to return (False).
        close_requested():       The user asked for the plot to close.
        export_requested(object): The user chose a ``PlotExport`` entry.
        height_wanted(int):      The measuring table or the note appeared and
                                 takes this many pixels (positive), or went
                                 and gave them back (negative). Under the
                                 console the host adds them to the plot, so
                                 the chart keeps its height.
    """

    pause_toggled = Signal(bool)
    configure_requested = Signal()
    float_requested = Signal(bool)
    close_requested = Signal()
    export_requested = Signal(object)
    height_wanted = Signal(int)

    def __init__(
        self,
        plot_engine: PlotEngine,
        theme_manager: ThemeManager,
        parent: QWidget | None = None,
        history: PlotHistory | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("plotView")
        self._engine = plot_engine
        self._theme_manager = theme_manager
        self._history = history
        # For each value, the time from which every sample is drawn; older
        # time shows only the least and greatest of each slice.
        self._exact_from: dict[str, float] = {}
        # The samples in memory as they were when the plot was held still.
        self._held_samples: dict[str, tuple[np.ndarray, np.ndarray]] | None = None
        # Older time held still on screen, being read in full, and the
        # figures that reading gave.
        self._detail_in_view: DetailRequest | None = None
        self._lines_skipped = 0
        # The height the note took when it appeared, given back when it goes.
        self._note_height = 0

        self._columns: list[str] = []
        self._rows: dict[str, ValueRow] = {}
        self._traces: dict[str, pg.PlotDataItem] = {}
        self._panels: list[_Panel] = []
        self._data: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        # When the board was lost, each time it was: no line joins the
        # samples on either side. Their times on the chart are worked out
        # again once the time of x = 0 is known or changes.
        self._breaks: list[datetime] = []
        self._break_x: np.ndarray | None = None
        self._break_origin: datetime | None = None
        self._layout_mode = PlotLayout.PANELS
        self._window_seconds: float | None = PLOT_DEFAULT_WINDOW_SECONDS
        self._paused = False
        self._floating = False
        self._measuring = False
        # Measuring held the plot, and stopping measuring lets it go live.
        self._held_for_measuring = False
        # Dragging back in time held the plot, and Fit lets it go live.
        self._held_for_looking_back = False
        # The latest choice made from the time list, and whether the wheel
        # has changed the time shown since; Fit gives that choice back.
        self._chosen_window: float | None = PLOT_DEFAULT_WINDOW_SECONDS
        self._zoomed = False
        # The height the measuring table took when it appeared, given back
        # exactly when it goes.
        self._table_height = 0
        self._cursor_times: list[float] = [0.0, 0.0]
        # Where the pointer is over the chart, in scene coordinates; None
        # when it is elsewhere. The moment under it, if any.
        self._pointer: QPointF | None = None
        self._hover_time: float | None = None
        # The next redraw fits the view to the data.
        self._fit_pending = False
        # The next redraw with data sorts the values into panels.
        self._regroup_pending = False
        # What each trace was last handed. Handing a trace samples redraws
        # the chart, so samples it already has are not handed again.
        self._handed: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        # The engine's generation and newest time when the last frame was
        # drawn, and whether something besides new samples has changed
        # what the next frame shows.
        self._drawn_generation: int | None = None
        self._drawn_latest = 0.0
        self._redraw_wanted = False
        # The time a thinned line was worked out for and the time one
        # pixel spanned then; None while every line is drawn whole.
        self._drawn_span: tuple[float, float, float] | None = None
        self._is_shut_down = False

        # Both timers run only while the view can be seen.
        self._timer = QTimer(self)
        self._timer.setInterval(PLOT_REDRAW_INTERVAL_MS)
        self._timer.timeout.connect(self._on_tick)
        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(PLOT_STATS_INTERVAL_MS)
        self._stats_timer.timeout.connect(self._update_figures)
        # Older time held still is read in full once the view has rested.
        self._detail_timer = QTimer(self)
        self._detail_timer.setSingleShot(True)
        self._detail_timer.setInterval(PLOT_HISTORY_DETAIL_DELAY_MS)
        self._detail_timer.timeout.connect(self._ask_for_detail)

        self._build()
        self._apply_theme()

        self._engine.columns_detected.connect(self._on_columns_detected)
        self._engine.detection_failed.connect(self._on_detection_failed)
        self._engine.pattern_stalled.connect(self._on_pattern_stalled)
        if self._engine.stalled:
            self._on_pattern_stalled()
        elif self._engine.detection_complete:
            self._on_columns_detected(self._engine.columns)
        elif self._engine.detection_gave_up:
            self._on_detection_failed()
        theme_manager.theme_changed.connect(self._on_theme_changed)
        if history is not None:
            history.changed.connect(self._on_history_changed)

    # ── Building ─────────────────────────────────────────────────────────

    def _build(self) -> None:
        metrics = tokens.METRICS

        # Shown until values are found, and again if none can be found.
        self._placeholder = plain_label(_WAITING_TEXT)
        self._placeholder.setObjectName("plotPlaceholderLabel")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setWordWrap(True)
        self._describe = self._button("Describe the format...", self.configure_requested.emit)
        self._describe.setVisible(False)
        waiting = QWidget()
        waiting_layout = QVBoxLayout(waiting)
        waiting_layout.setContentsMargins(
            metrics.space_6, metrics.space_6, metrics.space_6, metrics.space_6,
        )
        waiting_layout.setSpacing(metrics.space_3)
        waiting_layout.addStretch(1)
        waiting_layout.addWidget(self._placeholder)
        waiting_layout.addWidget(self._describe, 0, Qt.AlignmentFlag.AlignHCenter)
        waiting_layout.addStretch(1)

        self._values_layout = QVBoxLayout()
        self._values_layout.setContentsMargins(0, 0, 0, 0)
        self._values_layout.setSpacing(0)
        self._values_layout.addStretch(1)
        values = QWidget()
        values.setLayout(self._values_layout)
        self._values_scroll = VerticalScrollArea(values)
        self._values_scroll.setObjectName("plotValues")
        mark(self._values_scroll, Target.PLOT_VALUES)
        # At least its design width, wider if a value needs it; never wider
        # than it asks, so the chart has the rest.
        self._values_scroll.setMinimumWidth(metrics.plot_values_width)
        self._values_scroll.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)

        self._measure_table = _MeasureTable()
        self._measure_table.setVisible(False)
        self._chart_layout = QVBoxLayout()
        self._chart_layout.setContentsMargins(0, 0, 0, 0)
        self._chart_layout.setSpacing(0)
        self._chart_layout.addWidget(self._measure_table)
        self._graphics: pg.GraphicsLayoutWidget | None = None
        self._new_graphics()
        chart = self._chart_layout
        body = QWidget()
        body.setObjectName("plotContent")
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        body_layout.addWidget(self._values_scroll)
        body_layout.addLayout(chart, 1)
        self._chart_area = mark(body, Target.PLOT_CHART)
        self._readout = _Readout(body)
        self._note = _Note()

        content = QWidget()
        self._stack = QStackedLayout(content)
        self._stack.setContentsMargins(0, 0, 0, 0)
        self._stack.addWidget(waiting)
        self._stack.addWidget(body)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_toolbar())
        outer.addWidget(self._note)
        outer.addWidget(content, 1)

    def _new_graphics(self) -> None:
        """Put a fresh, empty drawing surface above the measuring table."""
        graphics = pg.GraphicsLayoutWidget()
        graphics.setObjectName("plotWidget")
        graphics.ci.setSpacing(0)
        graphics.ci.setContentsMargins(0, 0, 0, 0)
        # With the toolbar above it, the chart is at least a pane high, as
        # the console is: the measuring table under it takes room from the
        # console, never from the chart. The card under the pointer reads
        # the chart, so it has no tooltip to cover the card; the user
        # guide says how to move and zoom.
        metrics = tokens.METRICS
        graphics.setMinimumHeight(metrics.pane_min_height - metrics.header_height)
        graphics.setBackground(tokens.palette().paper)
        scene = graphics.scene()
        scene.sigMouseMoved.connect(self._on_pointer_moved)
        scene.sigMouseClicked.connect(self._on_chart_clicked)
        graphics.viewport().installEventFilter(self)
        self._chart_layout.insertWidget(0, graphics, 1)
        self._graphics = graphics

    def _dispose_graphics(self) -> None:
        """Take the drawing surface apart and let it go.

        pyqtgraph's charts must be closed by pyqtgraph and then deleted
        with their scene at once. A chart taken out of the scene and
        left for Python to free later can be reached by a layout event
        still on its way, or be deleted by Qt in an order pyqtgraph's
        objects do not survive; either crashes the process.
        """
        graphics, self._graphics = self._graphics, None
        if graphics is None:
            return
        graphics.viewport().removeEventFilter(self)
        self._show_hover(None)
        for panel in self._panels:
            panel.cursors.clear()
            panel.item.close()
        self._panels = []
        self._traces = {}
        self._handed = {}
        self._chart_layout.removeWidget(graphics)
        graphics.close()
        graphics.setParent(None)
        graphics.deleteLater()

    @staticmethod
    def _button(
        text: str, run: Callable[..., object], *, checkable: bool = False,
    ) -> Button:
        """A toolbar button. A checkable one calls ``run`` with its new state."""
        button = Button(text, compact=True)
        button.setCheckable(checkable)
        if checkable:
            button.toggled.connect(run)
        else:
            button.clicked.connect(lambda _checked=False: run())
        return button

    def _build_toolbar(self) -> QWidget:
        metrics = tokens.METRICS
        bar = QFrame()
        bar.setObjectName("plotToolbar")
        bar.setFixedHeight(metrics.header_height)

        self._layout_buttons: dict[PlotLayout, Button] = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        for mode, text, tip in (
            (PlotLayout.ONE_CHART, "One chart", "Draw every value in one chart"),
            (
                PlotLayout.PANELS, "Panels by scale",
                "Values of similar size share a panel, so a small value is not "
                "flattened by a large one",
            ),
        ):
            button = Button(text, compact=True)
            button.setCheckable(True)
            button.setToolTip(tip)
            button.clicked.connect(lambda _checked, m=mode: self.set_layout_mode(m))
            group.addButton(button)
            self._layout_buttons[mode] = button
        self._layout_buttons[self._layout_mode].setChecked(True)
        modes = mark(join_buttons(*self._layout_buttons.values()), Target.PLOT_LAYOUT)

        self._measure_btn = self._button("Measure", self.set_measuring, checkable=True)
        self._measure_btn.setToolTip(
            "Two cursors, A and B: the time between them and the change in each value"
        )
        mark(self._measure_btn, Target.PLOT_MEASURE)

        self._window_combo = QComboBox()
        set_compact(self._window_combo)
        self._window_combo.setAccessibleName("Time shown")
        self._window_combo.setToolTip(
            "How much recent time is shown. Nothing is discarded by changing it."
        )
        for text, seconds in PLOT_WINDOW_CHOICES:
            self._window_combo.addItem(text, seconds)
        # Wide enough from the start for every entry, Custom included,
        # which appears only while the wheel has zoomed.
        self._window_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._window_combo.setMinimumContentsLength(
            max(len(text) for text in (*(text for text, _ in PLOT_WINDOW_CHOICES), _CUSTOM_WINDOW))
        )
        self._window_combo.setCurrentIndex(self._window_index(self._window_seconds))
        self._window_combo.currentIndexChanged.connect(self._on_window_changed)
        mark(self._window_combo, Target.PLOT_TIME)

        self._pause_btn = self._button("Pause", self._on_pause_toggled, checkable=True)
        self._pause_btn.setToolTip("Hold the picture still. Values keep being collected.")
        mark(self._pause_btn, Target.PLOT_HOLD)
        self._fit_btn = self._button("Fit", self.fit)
        self._fit_btn.setToolTip(
            "Undo any zoom and show the newest data. In panels, sort the values into panels again."
        )
        mark(self._fit_btn, Target.PLOT_HOLD)
        self._clear_btn = self._button("Clear", self._on_clear)
        self._clear_btn.setToolTip("Forget the samples drawn so far. The values stay.")
        self._configure_btn = self._button("Configure...", self.configure_requested.emit)
        self._configure_btn.setToolTip("Choose how values are found in the lines")
        mark(self._configure_btn, Target.PLOT_CONFIGURE)
        self._export_btn = Button(compact=True)
        self._export_btn.setObjectName("flatIconButton")
        self._export_btn.setAccessibleName("Export")
        self._export_btn.setToolTip("Save the chart as a picture, or the values as a CSV file")
        bind_icon(self._export_btn, "export", "text_secondary")
        self._export_menu = QMenu(self)
        self._export_actions: dict[PlotExport, QAction] = {}
        for kind in PlotExport:
            if kind is PlotExport.VALUES_SHOWN:
                self._export_menu.addSeparator()
            action = self._export_menu.addAction(kind.value)
            action.triggered.connect(lambda _checked=False, k=kind: self.export_requested.emit(k))
            self._export_actions[kind] = action
        self._export_menu.aboutToShow.connect(self._enable_exports)
        self._export_btn.setMenu(self._export_menu)
        mark(self._export_btn, Target.PLOT_SAVE)
        self._float_btn = mark(
            self._button("", lambda: self.float_requested.emit(not self._floating)), Target.PLOT_SAVE,
        )
        self.set_floating(False)
        self._close_btn = Button(compact=True)
        self._close_btn.setObjectName("flatIconButton")
        self._close_btn.setAccessibleName("Close the plot")
        self._close_btn.setToolTip(shortcuts.with_keys("Close the plot", "plot"))
        bind_icon(self._close_btn, "close_tab", "text_secondary")
        self._close_btn.clicked.connect(self.close_requested)

        layout = QHBoxLayout(bar)
        layout.setContentsMargins(metrics.space_3, 0, metrics.space_3, 0)
        layout.setSpacing(metrics.space_2)
        layout.addWidget(modes)
        layout.addWidget(self._measure_btn)
        layout.addWidget(self._window_combo)
        layout.addWidget(self._pause_btn)
        layout.addWidget(self._fit_btn)
        layout.addWidget(self._clear_btn)
        layout.addWidget(self._configure_btn)
        layout.addStretch(1)
        layout.addWidget(self._float_btn)
        layout.addWidget(self._export_btn)
        layout.addWidget(self._close_btn)
        return bar

    # ── Public API ───────────────────────────────────────────────────────────

    @property
    def layout_mode(self) -> PlotLayout:
        return self._layout_mode

    @property
    def measuring(self) -> bool:
        return self._measuring

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def cursor_times(self) -> tuple[float, float]:
        """Where cursors A and B stand, in seconds."""
        return self._cursor_times[0], self._cursor_times[1]

    def panels(self) -> list[list[str]]:
        """The values of each panel, top to bottom."""
        return [list(panel.names) for panel in self._panels]

    def value_texts(self) -> list[tuple[str, str, str]]:
        """Each value as listed: name, last value, min/max/avg line."""
        return [self._rows[name].texts() for name in self._columns]

    def measure_texts(self) -> list[list[str]]:
        """The measuring table as shown: its heading, then A, B and B minus A."""
        return self._measure_table.texts()

    def hover_texts(self) -> tuple[str, str, list[tuple[str, str]]] | None:
        """The card under the pointer as shown, or None when there is none."""
        return self._readout.texts() if self._readout.isVisible() else None

    def time_in_view(self) -> tuple[float, float]:
        """The time on screen, in seconds on the plot, never before 0."""
        if not self._panels:
            return 0.0, 0.0
        low, high = self._time_in_view()
        return max(low, 0.0), max(high, 0.0)

    def save_picture(self, path: str) -> None:
        """Save the chart as shown: SVG when ``path`` ends in .svg, PNG otherwise.

        The SVG is drawn by Qt's own SVG writer, the PNG by pyqtgraph's
        image exporter, each from the chart's scene as it is on screen.

        Raises:
            OSError: The picture could not be made or written.
        """
        if self._graphics is None:
            raise OSError("there is no chart to save")
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if path.lower().endswith(".svg"):
            self._draw_svg(buffer)
        else:
            import pyqtgraph.exporters as exporters

            image = exporters.ImageExporter(self._graphics.ci).export(toBytes=True)
            if not image.save(buffer, "PNG"):
                raise OSError("the picture could not be made")
        atomic_write_bytes(path, bytes(buffer.data()))

    def _draw_svg(self, device: QBuffer) -> None:
        from PySide6.QtSvg import QSvgGenerator

        area = self._graphics.ci.sceneBoundingRect()
        size = area.size().toSize()
        generator = QSvgGenerator()
        generator.setOutputDevice(device)
        generator.setSize(size)
        generator.setViewBox(QRect(QPoint(0, 0), size))
        generator.setResolution(self._graphics.logicalDpiX())
        painter = QPainter()
        if not painter.begin(generator):
            raise OSError("the picture could not be made")
        try:
            painter.fillRect(QRect(QPoint(0, 0), size), QColor(tokens.palette().paper))
            self._graphics.scene().render(painter, QRectF(QPoint(0, 0), area.size()), area)
        finally:
            painter.end()

    def note_text(self) -> str:
        """The note over the chart about what it shows, or "" when there is none."""
        return self._note.text()

    def set_lines_skipped(self, count: int) -> None:
        """Lines of this session the plot fell too far behind to draw."""
        self._lines_skipped = count

    def summary(self) -> str:
        """One line saying how much is shown: "4 values, last 60 s shown"."""
        values = format_count(len(self._columns), "value")
        if self._window_seconds is None:
            shown = f"{values}, everything held is shown"
        elif self._custom_index() is not None:
            shown = f"{values}, last {format_value(self._window_seconds)} s shown"
        else:
            shown = f"{values}, last {self._window_combo.currentText()} shown"
        if self._lines_skipped:
            shown += f", {format_count(self._lines_skipped, 'line')} not drawn: the plot fell behind"
        return shown

    def set_layout_mode(self, mode: PlotLayout) -> None:
        """Draw every value in one chart, or in panels by scale."""
        self._layout_buttons[mode].setChecked(True)
        if mode is self._layout_mode:
            return
        self._layout_mode = mode
        self._rebuild_panels()

    def set_measuring(self, on: bool) -> None:
        """Measure between two cursors, A and B, or stop.

        Measuring holds the plot still, as a stopped oscilloscope does, so
        the cursors stay on the samples they were put on. Stopping goes
        back to live if measuring was what held the plot.
        """
        if self._measure_btn.isChecked() != on:
            self._measure_btn.setChecked(on)
            return
        self._measuring = on
        if on:
            if not self._paused:
                self._held_for_measuring = True
                self._pause_btn.setChecked(True)
            if self._panels:
                low, high = self._time_in_view()
                self._cursor_times = [
                    self._snapped(low + (high - low) * share) for share in _CURSOR_START
                ]
        elif self._held_for_measuring:
            self._held_for_measuring = False
            self._pause_btn.setChecked(False)
        self._rebuild_cursors()
        self._measure_table.setVisible(on)
        self._update_measure()
        if on and not self._table_height:
            self._table_height = self._table_height_at(self._graphics.width())
            self.height_wanted.emit(self._table_height)
        elif self._table_height:
            given, self._table_height = self._table_height, 0
            self.height_wanted.emit(-given)

    def _table_height_at(self, width: int) -> int:
        """The height the measuring table takes when ``width`` wide, the
        width of the chart above it; its hint wraps, so it depends on it."""
        table = self._measure_table
        if table.hasHeightForWidth():
            return max(table.heightForWidth(width), table.minimumSizeHint().height())
        return table.sizeHint().height()

    def set_cursor(self, index: int, time: float) -> None:
        """Move cursor A (0) or B (1) to the sample nearest ``time`` seconds."""
        time = self._snapped(time)
        self._cursor_times[index] = time
        for panel in self._panels:
            if panel.cursors:
                line = panel.cursors[index]
                line.blockSignals(True)
                line.setValue(time)
                line.blockSignals(False)
        self._update_measure()

    def set_value_shown(self, name: str, shown: bool) -> None:
        """Show or hide one value in the chart."""
        row = self._rows.get(name)
        if row is not None:
            row.set_shown(shown)

    def set_floating(self, floating: bool) -> None:
        """Say whether the plot is in its own window; the host moves it."""
        self._floating = floating
        self._float_btn.setText("Dock" if floating else "Pop out")
        self._float_btn.setToolTip(
            "Put the plot back under the console" if floating
            else "Move the plot to a window of its own, for a second monitor"
        )

    def fit(self) -> None:
        """Undo zoom and panning, and sort the values into panels again.

        A plot held still takes the newest samples in memory; one held by
        dragging back in time goes live again.
        """
        if self._zoomed:
            self._zoomed = False
            self._select_window(self._window_index(self._chosen_window))
            self._remove_custom_window()
            self._window_seconds = self._chosen_window
        if self._held_for_looking_back:
            self._pause_btn.setChecked(False)
        self._fit_pending = True
        self._held_samples = None
        if self._layout_mode is PlotLayout.PANELS:
            self._regroup_pending = True
        if self._paused:
            self._redraw(force=True)

    def set_breaks(self, lost: list[datetime]) -> None:
        """When the board was lost: each value's line is broken there."""
        self._breaks = list(lost)
        self._break_x = None
        self._redraw_soon()

    def _broken(self, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """``x`` and ``y`` with a gap after the last sample before each break.

        The gap is a sample with no value at the time of the one before,
        so the times stay in order for clipping and thinning.
        """
        breaks = self._break_positions()
        if not breaks.size or x.size < 2:
            return x, y
        at = np.unique(np.searchsorted(x, breaks, side="right"))
        at = at[(at > 0) & (at < x.size)]
        if not at.size:
            return x, y
        return np.insert(x, at, x[at - 1]), np.insert(y.astype(float, copy=False), at, np.nan)

    def _break_positions(self) -> np.ndarray:
        origin = self._engine.origin
        if self._break_x is None or origin != self._break_origin:
            self._break_origin = origin
            self._break_x = np.array(
                [(lost - origin).total_seconds() for lost in self._breaks]
                if origin is not None else [],
                dtype=float,
            )
        return self._break_x

    def reset_session(self) -> None:
        """Start afresh for a new connection, keeping the view itself.

        Forgets the values, their samples and the cursors, and returns
        to the waiting text. The layout chosen and the time shown stay.
        """
        self._engine.reset()
        self._held_samples = None
        self._lines_skipped = 0
        self._set_columns([])
        self._placeholder.setText(_WAITING_TEXT)
        self._describe.setVisible(False)
        self._stack.setCurrentIndex(0)
        if self._pause_btn.isChecked():
            self._pause_btn.setChecked(False)
        if self._measure_btn.isChecked():
            self._measure_btn.setChecked(False)
        self._fit_pending = False
        self._show_note()

    def shutdown(self) -> None:
        """Stop for good. Called once, when the tab closes.

        A disconnect is not a shutdown: that uses ``reset_session()``.
        """
        if self._is_shut_down:
            return
        # A show event that arrives while the widget is being destroyed
        # must not start the timers again.
        self._is_shut_down = True

        # The theme manager outlives this widget and must not call it.
        with contextlib.suppress(RuntimeError, TypeError):
            self._theme_manager.theme_changed.disconnect(self._on_theme_changed)
        self._timer.stop()
        self._stats_timer.stop()
        self._detail_timer.stop()
        if self._history is not None:
            with contextlib.suppress(RuntimeError, TypeError):
                self._history.changed.disconnect(self._on_history_changed)

        # The charts are taken apart here, while every part still exists,
        # and not left to the destruction of the window.
        self._dispose_graphics()

    # ── Values and panels ────────────────────────────────────────────────────

    def _on_columns_detected(self, columns: list[str]) -> None:
        if list(columns) != self._columns:
            self._set_columns(list(columns))
        self._stack.setCurrentIndex(1)
        self._fit_pending = True

    def _on_detection_failed(self) -> None:
        self._placeholder.setText(_NOT_FOUND_TEXT)
        self._describe.setVisible(True)
        self._stack.setCurrentIndex(0)

    def _on_pattern_stalled(self) -> None:
        self._placeholder.setText(_PATTERN_STALLED_TEXT)
        self._describe.setVisible(True)
        self._stack.setCurrentIndex(0)

    def _colour_of(self, name: str) -> str:
        colours = tokens.palette().plot_traces
        return colours[self._columns.index(name) % len(colours)]

    def _set_columns(self, columns: list[str]) -> None:
        for row in self._rows.values():
            self._values_layout.removeWidget(row)
            row.deleteLater()
        self._columns = columns
        self._rows = {}
        self._data = {}
        self._exact_from = {}
        for index, name in enumerate(columns):
            row = ValueRow(name, self._colour_of(name))
            row.shown_changed.connect(self._on_value_shown)
            self._values_layout.insertWidget(index, row)
            self._rows[name] = row
        self._rebuild_panels()

    def _groups(self) -> list[list[str]]:
        """Which values each panel holds, for the layout chosen."""
        if not self._columns:
            return []
        if self._layout_mode is PlotLayout.ONE_CHART:
            return [list(self._columns)]
        sizes = {
            name: magnitude(self._data[name][1]) if name in self._data else 0.0
            for name in self._columns
        }
        return group_by_scale(sizes)

    def _rebuild_panels(self) -> None:
        if self._is_shut_down:
            return
        self._dispose_graphics()
        self._new_graphics()
        groups = self._groups()
        metrics = tokens.METRICS
        for row, names in enumerate(groups):
            box = _TimeViewBox(self._is_live)
            box.zoom_asked.connect(self._zoom_live)
            box.hold_asked.connect(self._hold_to_look_back)
            item = self._graphics.ci.addPlot(row=row, col=0, viewBox=box)
            item.showGrid(x=True, y=True, alpha=_GRID_ALPHA)
            for side in ("left", "bottom"):
                item.getAxis(side).setStyle(maxTickLevel=_GRID_LEVEL)
            # The wheel and dragging move and zoom time only, every panel
            # together; each panel's scale always fits the values in the
            # time shown. While live, the wheel changes how much recent
            # time is shown, and dragging back holds the plot.
            item.getViewBox().setMouseEnabled(x=True, y=False)
            item.setAutoVisible(y=True)
            # pyqtgraph's own right-click menu and auto-range button are
            # not offered: its menu exports SVG through an exporter that
            # fails with PySide6, redraws a chart (spectrum, log scale) in
            # ways the card and Measure do not follow, and zooms the value
            # scale by rectangle. Everything the plot does is in its toolbar.
            item.setMenuEnabled(False)
            item.hideButtons()
            item.getAxis("left").setWidth(metrics.plot_axis_width)
            if row:
                item.setXLink(self._panels[0].item)
            else:
                item.getViewBox().sigXRangeChanged.connect(self._on_view_moved)
            is_last = row == len(groups) - 1
            item.getAxis("bottom").setStyle(showValues=is_last)
            if is_last:
                item.setLabel("bottom", "Time", units="s")
            for name in names:
                trace = item.plot(
                    pen=pg.mkPen(color=self._colour_of(name), width=tokens.METRICS.mark),
                    connect="finite",
                )
                trace.setVisible(self._rows[name].shown)
                self._traces[name] = trace
            hover = pg.InfiniteLine(angle=90, movable=False)
            hover.setVisible(False)
            item.addItem(hover, ignoreBounds=True)
            dots = {}
            for name in names:
                dot = pg.ScatterPlotItem(size=metrics.space_2)
                dot.setVisible(False)
                item.addItem(dot, ignoreBounds=True)
                dots[name] = dot
            self._panels.append(_Panel(item, names, hover, dots))
        self._apply_theme()
        self._rebuild_cursors()
        self._fit_pending = True
        self._regroup_pending = self._layout_mode is PlotLayout.PANELS and not self._data
        # Drawn at once, not at the next tick: the new panels are never
        # shown empty or out of place.
        if self._panels:
            self._redraw(force=True)

    def _on_value_shown(self, name: str, shown: bool) -> None:
        trace = self._traces.get(name)
        if trace is not None:
            trace.setVisible(shown)
        self._update_measure()
        self._refresh_hover()

    # ── Toolbar ──────────────────────────────────────────────────────────

    def _enable_exports(self) -> None:
        """Nothing can be saved before values have been found."""
        found = bool(self._panels)
        for action in self._export_actions.values():
            action.setEnabled(found)

    def _on_pause_toggled(self, paused: bool) -> None:
        self._paused = paused
        self._pause_btn.setText("Resume" if paused else "Pause")
        if not paused:
            self._held_for_looking_back = False
        if paused:
            self._held_samples = {name: self._engine.snapshot(name) for name in self._columns}
            self._detail_timer.start()
        else:
            self._held_samples = None
            self._detail_timer.stop()
            # Going live puts the newest data back in view at the next
            # tick, even if nothing has arrived while held.
            self._redraw_wanted = True
        if not paused and self._measuring:
            # Live and measuring do not go together: going live ends it.
            self._held_for_measuring = False
            self.set_measuring(False)
        self.pause_toggled.emit(paused)

    def _on_clear(self) -> None:
        self._engine.clear_buffers()
        self._held_samples = None
        self._data = {}
        for name in self._traces:
            self._hand_over(name, np.empty(0), np.empty(0))
        self._fit_pending = True
        self._update_figures()

    def _on_window_changed(self, index: int) -> None:
        """A choice made from the time list."""
        if index == self._custom_index():
            return
        self._zoomed = False
        self._remove_custom_window()
        self._chosen_window = self._window_combo.currentData()
        self._show_window(self._chosen_window)

    def _show_window(self, seconds: float | None) -> None:
        """Show the last ``seconds``; None shows everything held."""
        self._window_seconds = seconds
        self._fit_pending = True
        self._redraw_soon()

    def _is_live(self) -> bool:
        return not self._paused

    def _zoom_live(self, factor: float) -> None:
        """Show ``factor`` times the recent time on screen, staying on the newest data.

        Past everything held, the plot shows All; the time list's own
        choice is kept for Fit.
        """
        latest = self._engine.latest_x
        if latest <= 0:
            return
        on_screen = latest if self._window_seconds is None else min(self._window_seconds, latest)
        span = on_screen * factor
        self._zoomed = True
        if span >= latest:
            self._remove_custom_window()
            self._select_window(self._window_index(None))
            self._show_window(None)
            return
        span = max(PLOT_LIVE_SPAN_MIN_SECONDS, span)
        index = self._custom_index()
        if index is None:
            self._window_combo.blockSignals(True)
            self._window_combo.addItem(_CUSTOM_WINDOW, span)
            self._window_combo.blockSignals(False)
            index = self._window_combo.count() - 1
        self._window_combo.setItemData(index, span)
        self._select_window(index)
        self._show_window(span)

    def _select_window(self, index: int) -> None:
        """Show entry ``index`` in the time list, as the plot's own change."""
        self._window_combo.blockSignals(True)
        self._window_combo.setCurrentIndex(index)
        self._window_combo.blockSignals(False)

    def _remove_custom_window(self) -> None:
        index = self._custom_index()
        if index is not None:
            self._window_combo.blockSignals(True)
            self._window_combo.removeItem(index)
            self._window_combo.blockSignals(False)

    def _window_index(self, seconds: float | None) -> int:
        """The index of the time list's choice of ``seconds``; None is All."""
        return next(
            index for index in range(self._window_combo.count())
            if self._window_combo.itemText(index) != _CUSTOM_WINDOW
            and self._window_combo.itemData(index) == seconds
        )

    def _custom_index(self) -> int | None:
        index = self._window_combo.findText(_CUSTOM_WINDOW)
        return index if index >= 0 else None

    def _hold_to_look_back(self) -> None:
        """A drag back in time while live holds the plot where it is taken."""
        self._held_for_looking_back = True
        self._pause_btn.setChecked(True)

    # ── Drawing ──────────────────────────────────────────────────────────

    def showEvent(self, event: QShowEvent) -> None:
        """Draw the current data at once, then keep redrawing."""
        super().showEvent(event)
        if not self._is_shut_down:
            self._redraw()
            self._update_figures()
            self._timer.start()
            self._stats_timer.start()

    def hideEvent(self, event: QHideEvent) -> None:
        """Do no drawing while nothing can be seen."""
        super().hideEvent(event)
        self._timer.stop()
        self._stats_timer.stop()

    def _on_tick(self) -> None:
        """Draw the next frame if it can show anything new.

        A held chart is drawn again only when it was moved, zoomed or
        widened past what its lines were worked out for.
        """
        if self._paused:
            if self._traces and not self._drawing_fits_view():
                self._redraw(force=True)
        elif self._frame_due():
            self._redraw()

    def _frame_due(self) -> bool:
        """True when the next frame would differ from the one on screen.

        New samples change the picture once the newest time has moved
        a whole pixel of the screen: before that they fall in the column
        already drawn, and the chart would be painted again unchanged.
        Anything else that changes what is shown is drawn at once.
        """
        if self._redraw_wanted or self._fit_pending or self._regroup_pending:
            return True
        if self._engine.generation != self._drawn_generation:
            return True
        if not self._drawing_fits_view():
            return True
        return abs(self._engine.latest_x - self._drawn_latest) >= self._seconds_per_pixel()

    def _pixels_wide(self) -> float:
        """The chart's width in the screen's own pixels, or 0 before it has a size."""
        if not self._panels or self._graphics is None:
            return 0.0
        width = self._panels[0].item.getViewBox().width() * self._graphics.devicePixelRatioF()
        return max(width, 0.0)

    def _seconds_per_pixel(self) -> float:
        """The time one pixel of the screen spans on the chart, or 0 before
        the chart has a size."""
        pixels = self._pixels_wide()
        if not pixels:
            return 0.0
        low, high = self._time_in_view()
        return (high - low) / pixels

    def _drawing_span(self, low: float, high: float) -> tuple[float, float, int, float]:
        """The time to work the lines out for when ``low`` to ``high`` is
        shown, the columns to cut it into, and the time one pixel spans.

        No columns before the chart has a size: the lines are drawn whole.
        """
        pixels = self._pixels_wide()
        span = high - low
        if not pixels or not span > 0:
            return low, high, 0, 0.0
        margin = PLOT_DRAWN_MARGIN_VIEWS * span
        # Columns never wider than a pixel of the time shown.
        columns = math.ceil(pixels * (1 + 2 * PLOT_DRAWN_MARGIN_VIEWS))
        return low - margin, high + margin, columns, span / pixels

    def _drawing_fits_view(self) -> bool:
        """True when the lines drawn hold what the time shown needs: they
        reach both its ends, and no pixel is narrower than a column."""
        drawn = self._drawn_span
        if drawn is None:
            return True
        start, end, seconds_per_pixel = drawn
        low, high = self._time_in_view()
        return start <= low and high <= end and self._seconds_per_pixel() >= seconds_per_pixel

    def _redraw_soon(self) -> None:
        """Show a change other than new samples: at once while held still,
        at the next tick while live."""
        if self._paused:
            self._redraw(force=True)
        else:
            self._redraw_wanted = True

    def _hand_over(self, name: str, x: np.ndarray, y: np.ndarray) -> None:
        """Give the trace of ``name`` these samples, unless it has them already."""
        handed = self._handed.get(name)
        if (
            handed is not None
            and np.array_equal(handed[0], x, equal_nan=True)
            and np.array_equal(handed[1], y, equal_nan=True)
        ):
            return
        self._traces[name].setData(x, y)
        self._handed[name] = (x, y)

    def _redraw(self, *, force: bool = False) -> None:
        if (self._paused and not force) or not self._traces:
            return
        self._redraw_wanted = False
        self._drawn_generation = self._engine.generation
        self._drawn_latest = self._engine.latest_x
        try:
            if self._history is not None:
                self._history.sync()
            low, high = self._time_to_draw()
            start, end, columns, seconds_per_pixel = self._drawing_span(low, high)
            thinned = False
            for name in self._traces:
                x, y, exact_from = self._series(name, low, high)
                self._data[name] = (x, y)
                self._exact_from[name] = exact_from
                broken_x, broken_y = self._broken(x, y)
                drawn_x, drawn_y = points_to_draw(broken_x, broken_y, start, end, columns)
                thinned = thinned or drawn_x is not broken_x
                self._hand_over(name, drawn_x, drawn_y)
            self._drawn_span = (start, end, seconds_per_pixel) if thinned else None
            if not any(x.size for x, _ in self._data.values()):
                return
            if self._regroup_pending:
                self._regroup_pending = False
                if self._groups() != self.panels():
                    self._rebuild_panels()
                    return
            self._place_view()
            self._refresh_hover()
            # pyqtgraph fits each panel's value scale just before painting,
            # and a scale changed during a paint asks for a second paint.
            # Fitting it now makes each redraw one paint.
            self._graphics.scene().prepareForPaint()
        except Exception:
            # A fault while drawing must not take the tab with it: the
            # plot stops updating and says why in the log.
            log_data_failure(logger, "Plot redraw failed; the plot is paused")
            self._pause_btn.setChecked(True)

    def _place_view(self) -> None:
        """Keep the newest data in view, or fit everything once.

        While held still (Pause, Measure) the time shown stays where it
        is, however much arrives meanwhile; only Fit moves it.
        """
        if self._paused and not self._fit_pending:
            return
        first = self._panels[0].item
        if self._window_seconds is None:
            if self._fit_pending:
                for panel in self._panels:
                    panel.item.enableAutoRange(axis="xy", enable=True)
        else:
            if self._fit_pending:
                for panel in self._panels:
                    panel.item.enableAutoRange(axis="y", enable=True)
            latest = self._engine.latest_x
            first.setXRange(max(0.0, latest - self._window_seconds), latest, padding=_X_PADDING)
        self._fit_pending = False

    def _time_in_view(self) -> tuple[float, float]:
        low, high = self._panels[0].item.getViewBox().viewRange()[0]
        return float(low), float(high)

    def _time_to_draw(self) -> tuple[float, float]:
        """The time the chart will show once placed: the time shown while
        live, everything when everything is shown, or where it is held."""
        latest = self._engine.latest_x
        if self._window_seconds is None and (self._fit_pending or not self._paused):
            return 0.0, latest
        if self._fit_pending or not self._paused:
            return max(0.0, latest - self._window_seconds), latest
        return self._time_in_view()

    def _samples_in_memory(self, name: str) -> tuple[np.ndarray, np.ndarray]:
        held = self._held_samples
        if held is not None and name in held:
            return held[name]
        return self._engine.snapshot(name)

    def _series(self, name: str, low: float, high: float) -> tuple[np.ndarray, np.ndarray, float]:
        """What to draw of ``name`` between ``low`` and ``high``, and the time
        from which every sample is drawn.

        The samples in memory, after older time from the history: every
        sample read in full for this view if there is one, or else the
        least and greatest of each slice of the overview.
        """
        x, y = self._samples_in_memory(name)
        history = self._history
        oldest = float(x[0]) if x.size else np.inf
        # Times start at the session's first line: there is nothing before 0.
        low = max(low, 0.0)
        if history is None or low >= oldest:
            return x, y, -np.inf
        end = min(high, oldest)
        detail = self._detail_for_view()
        if detail is not None and name in detail.samples:
            older_x, older_y = detail.samples[name]
            exact_from = detail.request.low if detail.every_sample and detail.complete else oldest
        else:
            overview = history.overview(name)
            if overview is None:
                return x, y, oldest
            older_x, older_y = overview.samples(low, end)
            caught_up = history.reached is not None and history.reached >= oldest
            # Once the recording has been read that far, an older time with
            # no samples has none to draw.
            exact_from = -np.inf if caught_up and not np.any(older_x < oldest) else oldest
        before = older_x < oldest
        return (
            np.concatenate((older_x[before], x)),
            np.concatenate((older_y[before], y)),
            exact_from,
        )

    def _held_in_memory(self) -> dict[str, tuple[float, int]]:
        """For each value, its oldest sample in memory and how many share its time."""
        held: dict[str, tuple[float, int]] = {}
        for name in self._columns:
            x, _ = self._samples_in_memory(name)
            if x.size:
                oldest = float(x[0])
                held[name] = (oldest, int(np.count_nonzero(x == oldest)))
        return held

    def _wanted_detail(self) -> DetailRequest | None:
        """The older time on screen to read in full, if the plot is held
        still and shows any time older than the samples in memory."""
        if self._history is None or not self._paused or not self._panels or self._graphics is None:
            return None
        generation = self._engine.generation
        low, high = self._time_in_view()
        low = max(low, 0.0)
        held = self._held_in_memory()
        if held and all(low >= oldest for oldest, _ in held.values()):
            return None
        return DetailRequest(generation, low, high, max(1, self._graphics.width()), held)

    def _detail_for_view(self) -> DetailResult | None:
        """The full reading of the older time on screen, once it has come."""
        history = self._history
        wanted = self._detail_in_view
        if history is None or wanted is None or not self._paused:
            return None
        detail = history.detail
        if detail is None or detail.request != wanted:
            return None
        return detail

    def _ask_for_detail(self) -> None:
        wanted = self._wanted_detail()
        self._detail_in_view = wanted
        if wanted is not None:
            self._history.request_detail(wanted)
        self._show_note()

    def _on_view_moved(self, *_: object) -> None:
        """The time shown changed; while held still, read it in full once it rests."""
        if self._paused and self._history is not None:
            self._detail_timer.start()

    def _on_history_changed(self) -> None:
        if self._paused:
            # Only traces whose samples changed are handed them again, so
            # a held chart is painted again only when what it shows changed.
            self._redraw(force=True)
            self._update_figures()
        elif self._draws_older_time():
            self._redraw_wanted = True
        self._show_note()

    def _draws_older_time(self) -> bool:
        """True when the time to draw reaches back past the samples in
        memory of any value, where the history fills it in."""
        low = max(self._time_to_draw()[0], 0.0)
        for name in self._columns:
            x, _ = self._samples_in_memory(name)
            if not x.size or low < x[0]:
                return True
        return False

    def _show_note(self) -> None:
        """Say what the chart cannot show in full, if anything.

        The note takes its room as the measuring table does: the host
        adds it to the plot, so the chart keeps its height.
        """
        text = self._note_text()
        self._note.show_text(text)
        height = 0
        if text:
            note = self._note
            height = note.heightForWidth(self.width()) if note.hasHeightForWidth() \
                else note.sizeHint().height()
        if height != self._note_height:
            change, self._note_height = height - self._note_height, height
            self.height_wanted.emit(change)

    def _note_text(self) -> str:
        history = self._history
        if history is None or not self._panels:
            return ""
        if history.stalled:
            return _STALLED_HISTORY_NOTE
        low = max(self._time_in_view()[0], 0.0)
        shown_whole = all(low >= self._exact_from.get(name, -np.inf) for name in self._columns)
        if not shown_whole:
            if not self._paused:
                caught_up = history.reached is not None and all(
                    history.reached >= self._exact_from.get(name, -np.inf) for name in self._columns
                )
                return _EXTREMES_LIVE_NOTE if caught_up else _READING_OLDER_NOTE
            detail = self._detail_for_view()
            if detail is None:
                return _READING_NOTE if history.reading_detail else _EXTREMES_HELD_NOTE
            if not detail.complete:
                return _INCOMPLETE_NOTE
            return _EXTREMES_HELD_NOTE
        if history.start_lost and low <= 0:
            return _START_LOST_NOTE
        return ""

    def _update_figures(self) -> None:
        """Bring the figures beside the chart up to date."""
        if not self._panels:
            return
        low, high = self._time_in_view()
        detail = self._detail_for_view()
        for name, row in self._rows.items():
            x, y = self._data.get(name, (np.empty(0), np.empty(0)))
            exact_from = self._exact_from.get(name, -np.inf)
            if max(low, 0.0) >= exact_from:
                row.set_stats(visible_stats(x, y, low, high))
            elif detail is not None and detail.complete and name in detail.request.held:
                older = detail.figures.get(name)
                row.set_stats(stats_after(older, x, y, detail.request.held[name][0], high))
            else:
                last = visible_stats(x, y, low, high)
                why = _FIGURES_LIVE if not self._paused else (
                    _FIGURES_READING if self._history.reading_detail else _FIGURES_ZOOM
                )
                row.set_last_only(None if last is None else last.last, why)
        self._update_measure()
        self._show_note()

    # ── Measuring ────────────────────────────────────────────────────────

    def _rebuild_cursors(self) -> None:
        palette = tokens.palette()
        for panel in self._panels:
            for line in panel.cursors:
                panel.item.removeItem(line)
            panel.cursors = []
            if not self._measuring:
                continue
            for index, letter in enumerate("AB"):
                line = pg.InfiniteLine(
                    pos=self._cursor_times[index],
                    angle=90,
                    movable=True,
                    pen=pg.mkPen(color=palette.text, width=tokens.METRICS.border),
                    hoverPen=pg.mkPen(color=palette.accent, width=tokens.METRICS.border),
                    label=letter if panel is self._panels[0] else None,
                    labelOpts={
                        "position": _CURSOR_LABEL_POSITION,
                        "anchors": [_CURSOR_LABEL_ANCHOR, _CURSOR_LABEL_ANCHOR],
                        "color": palette.paper,
                        "fill": palette.text,
                    },
                )
                line.sigPositionChanged.connect(
                    lambda moved, i=index: self.set_cursor(i, float(moved.value()))
                )
                panel.item.addItem(line, ignoreBounds=True)
                panel.cursors.append(line)

    def _update_measure(self) -> None:
        if not self._measuring:
            return
        a, b = self._cursor_times
        shown = self._shown_names()
        columns: list[tuple[str | None, str]] = [(None, _TIME_HEADING)]
        columns += [(self._colour_of(name), name) for name in shown]
        rows = [
            [_MEASURE_ROWS[0], self._moment_text(a)],
            [_MEASURE_ROWS[1], self._moment_text(b)],
            [_MEASURE_ROWS[2], f"{format_value(b - a)} s"],
        ]
        for name in shown:
            at_a, at_b = self._held(name, a), self._held(name, b)
            rows[0].append(self._held_text(name, a))
            rows[1].append(self._held_text(name, b))
            if not (self._exact_at(name, a) and self._exact_at(name, b)):
                rows[2].append(_NOT_EXACT)
            else:
                change = None if at_a is None or at_b is None else at_b - at_a
                rows[2].append(_NO_FIGURE if change is None else format_change(change))
        self._measure_table.set_figures(columns, rows)

    # ── Under the pointer ────────────────────────────────────────────────

    def _shown_names(self) -> list[str]:
        return [name for name in self._columns if self._rows[name].shown]

    def _exact_at(self, name: str, time: float) -> bool:
        """True where every sample of ``name`` is drawn, so a value held can be read."""
        return time >= self._exact_from.get(name, -np.inf)

    def _held(self, name: str, time: float) -> float | None:
        """What ``name`` last received at or before ``time``, if anything.

        None where only the least and greatest are drawn: the value
        then is not known from what is drawn.
        """
        if name not in self._data or not self._exact_at(name, time):
            return None
        x, y = self._data[name]
        return value_held(x, y, time)

    def _held_text(self, name: str, time: float) -> str:
        if not self._exact_at(name, time):
            return _NOT_EXACT
        value = self._held(name, time)
        return _NO_FIGURE if value is None else format_received(value)

    def _snapped(self, time: float) -> float:
        """The time of the received sample nearest to ``time``, among the values shown."""
        nearest = nearest_sample([self._data[n][0] for n in self._shown_names() if n in self._data], time)
        return time if nearest is None else nearest

    def _moment_text(self, time: float) -> str:
        """``time`` on the chart as the moment it was received, as the console writes it."""
        moment = self._engine.time_of(time)
        return f"{format_value(time)} s" if moment is None else format_time_of_day(moment)

    def _sample_under(self, scene_pos: QPointF) -> float | None:
        """The time of the sample nearest the pointer, if it is over a chart."""
        for panel in self._panels:
            box = panel.item.getViewBox()
            if box.sceneBoundingRect().contains(scene_pos):
                at = box.mapSceneToView(scene_pos).x()
                times = [self._data[n][0] for n in self._shown_names() if n in self._data]
                return nearest_sample(times, at)
        return None

    def _on_pointer_moved(self, scene_pos: QPointF) -> None:
        self._pointer = QPointF(scene_pos)
        self._refresh_hover()

    def _on_chart_clicked(self, event: object) -> None:
        """While measuring, a click moves the nearer cursor to the sample clicked."""
        if not self._measuring or event.button() != Qt.MouseButton.LeftButton:
            return
        time = self._sample_under(event.scenePos())
        if time is None:
            return
        a, b = self._cursor_times
        self.set_cursor(0 if abs(time - a) <= abs(time - b) else 1, time)

    def _refresh_hover(self) -> None:
        self._show_hover(None if self._pointer is None else self._sample_under(self._pointer))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # Installed on the chart's viewport alone: the pointer left the chart.
        if event.type() == QEvent.Type.Leave:
            self._pointer = None
            self._show_hover(None)
        return super().eventFilter(watched, event)

    def _show_hover(self, time: float | None) -> None:
        """Mark the sample at ``time`` and give its figures; None hides them."""
        self._hover_time = time
        for panel in self._panels:
            panel.hover.setVisible(time is not None)
            if time is not None:
                panel.hover.setValue(time)
            for name, dot in panel.dots.items():
                value = None
                if time is not None and self._rows[name].shown:
                    value = self._held(name, time)
                dot.setVisible(value is not None)
                if value is not None:
                    dot.setData([time], [value])
        if time is None:
            self._readout.hide()
            return
        values = [
            (self._colour_of(name), name, self._held_text(name, time))
            for name in self._shown_names()
        ]
        self._readout.set_moment(self._moment_text(time), f"{format_value(time)} s", values)
        self._place_readout(time)
        self._readout.show()
        self._readout.raise_()

    def _place_readout(self, time: float) -> None:
        """Beside the line under the pointer, on the side with room, within the chart."""
        if self._graphics is None or self._pointer is None:
            return
        box = self._panels[0].item.getViewBox()
        line = QPointF(box.mapViewToScene(QPointF(time, 0.0)).x(), self._pointer.y())
        at = self._graphics.viewport().mapTo(self._chart_area, self._graphics.mapFromScene(line))
        gap = tokens.METRICS.space_4
        card, area = self._readout.size(), self._chart_area.rect()
        x = at.x() + gap
        if x + card.width() > area.right():
            x = at.x() - gap - card.width()
        y = at.y() - card.height() // 2
        y = max(area.top(), min(y, area.bottom() - card.height()))
        self._readout.move(max(area.left(), x), y)

    # ── Theme ────────────────────────────────────────────────────────────

    def _apply_theme(self) -> None:
        palette = tokens.palette()
        if self._graphics is not None:
            self._graphics.setBackground(palette.paper)
        axis_pen = pg.mkPen(color=palette.edge, width=tokens.METRICS.border)
        text_colour = QColor(palette.text_tertiary)
        for panel in self._panels:
            for side in ("left", "bottom"):
                axis = panel.item.getAxis(side)
                axis.setPen(axis_pen)
                axis.setTextPen(text_colour)
            if panel is self._panels[-1]:
                panel.item.setLabel("bottom", "Time", units="s", color=palette.text_tertiary)
        for panel in self._panels:
            panel.hover.setPen(pg.mkPen(color=palette.text_tertiary, width=tokens.METRICS.border))
            for name, dot in panel.dots.items():
                dot.setBrush(pg.mkBrush(self._colour_of(name)))
                dot.setPen(pg.mkPen(color=palette.paper, width=tokens.METRICS.mark))
        for name, trace in self._traces.items():
            trace.setPen(pg.mkPen(color=self._colour_of(name), width=tokens.METRICS.mark))
        for name, row in self._rows.items():
            row.set_colour(self._colour_of(name))

    def _on_theme_changed(self, *_: object) -> None:
        self._apply_theme()
        self._rebuild_cursors()
        self._refresh_hover()
