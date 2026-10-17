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

All parsing, detection and buffering is in ``core/plot_engine.py``; the
figures are worked out in ``core/plot_stats.py``.
"""

from __future__ import annotations

import contextlib
import enum
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QEvent, QObject, QPointF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
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
    QScrollArea,
    QSizePolicy,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)

from app.constants import (
    PLOT_DEFAULT_WINDOW_SECONDS,
    PLOT_REDRAW_INTERVAL_MS,
    PLOT_STATS_INTERVAL_MS,
    PLOT_WINDOW_CHOICES,
)
from core.plot_stats import (
    SeriesStats,
    group_by_scale,
    magnitude,
    nearest_sample,
    value_held,
    visible_stats,
)
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
from ui.widgets.button import Button, join_buttons
from ui.widgets.styling import SECONDARY, SMALL, plain_label, plain_tooltip, set_compact
from ui.widgets.vertical_scroll import VerticalScrollArea

if TYPE_CHECKING:
    from core.plot_engine import PlotEngine
    from ui.themes.theme_manager import ThemeManager

logger = logging.getLogger(__name__)

#: How strongly the grid is drawn, from 0 to 1: part of the drawing of the
#: chart, named as such by the design language, like the trace colours.
_GRID_ALPHA = 0.3
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


class PlotLayout(enum.Enum):
    """How the values are arranged."""

    ONE_CHART = "one"
    PANELS = "panels"


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
        height_wanted(int):      The measuring table appeared and takes this
                                 many pixels (positive), or went and gave
                                 them back (negative). The host adds them
                                 to the plot, so the chart keeps its height.
    """

    pause_toggled = Signal(bool)
    configure_requested = Signal()
    float_requested = Signal(bool)
    close_requested = Signal()
    height_wanted = Signal(int)

    def __init__(
        self,
        plot_engine: PlotEngine,
        theme_manager: ThemeManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("plotView")
        self._engine = plot_engine
        self._theme_manager = theme_manager

        self._columns: list[str] = []
        self._rows: dict[str, ValueRow] = {}
        self._traces: dict[str, pg.PlotDataItem] = {}
        self._panels: list[_Panel] = []
        self._data: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self._layout_mode = PlotLayout.ONE_CHART
        self._window_seconds: float | None = PLOT_DEFAULT_WINDOW_SECONDS
        self._paused = False
        self._floating = False
        self._measuring = False
        # Measuring held the plot, and stopping measuring lets it go live.
        self._held_for_measuring = False
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
        self._is_shut_down = False

        # Both timers run only while the view can be seen.
        self._timer = QTimer(self)
        self._timer.setInterval(PLOT_REDRAW_INTERVAL_MS)
        self._timer.timeout.connect(self._redraw)
        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(PLOT_STATS_INTERVAL_MS)
        self._stats_timer.timeout.connect(self._update_figures)

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
        self._chart_area = body
        self._readout = _Readout(body)

        content = QWidget()
        self._stack = QStackedLayout(content)
        self._stack.setContentsMargins(0, 0, 0, 0)
        self._stack.addWidget(waiting)
        self._stack.addWidget(body)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_toolbar())
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
        modes = join_buttons(*self._layout_buttons.values())

        self._measure_btn = self._button("Measure", self.set_measuring, checkable=True)
        self._measure_btn.setToolTip(
            "Two cursors, A and B: the time between them and the change in each value"
        )

        self._window_combo = QComboBox()
        set_compact(self._window_combo)
        self._window_combo.setAccessibleName("Time shown")
        self._window_combo.setToolTip(
            "How much recent time is shown. Nothing is discarded by changing it."
        )
        for text, seconds in PLOT_WINDOW_CHOICES:
            self._window_combo.addItem(text, seconds)
        self._window_combo.setCurrentIndex(self._window_combo.findData(self._window_seconds))
        self._window_combo.currentIndexChanged.connect(self._on_window_changed)

        self._pause_btn = self._button("Pause", self._on_pause_toggled, checkable=True)
        self._pause_btn.setToolTip("Hold the picture still. Values keep being collected.")
        self._fit_btn = self._button("Fit", self.fit)
        self._fit_btn.setToolTip(
            "Undo any zoom and show the newest data. In panels, sort the values into panels again."
        )
        self._clear_btn = self._button("Clear", self._on_clear)
        self._clear_btn.setToolTip("Forget the samples drawn so far. The values stay.")
        self._configure_btn = self._button("Configure...", self.configure_requested.emit)
        self._configure_btn.setToolTip("Choose how values are found in the lines")
        self._float_btn = self._button("", lambda: self.float_requested.emit(not self._floating))
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

    def summary(self) -> str:
        """One line saying how much is shown: "4 values, last 60 s shown"."""
        values = format_count(len(self._columns), "value")
        if self._window_seconds is None:
            return f"{values}, everything held is shown"
        return f"{values}, last {self._window_combo.currentText()} shown"

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
        """Undo zoom and panning, and sort the values into panels again."""
        self._fit_pending = True
        if self._layout_mode is PlotLayout.PANELS:
            self._regroup_pending = True
        if self._paused:
            self._redraw(force=True)

    def reset_session(self) -> None:
        """Start afresh for a new connection, keeping the view itself.

        Forgets the values, their samples and the cursors, and returns
        to the waiting text. The layout chosen and the time shown stay.
        """
        self._engine.reset()
        self._set_columns([])
        self._placeholder.setText(_WAITING_TEXT)
        self._describe.setVisible(False)
        self._stack.setCurrentIndex(0)
        if self._pause_btn.isChecked():
            self._pause_btn.setChecked(False)
        if self._measure_btn.isChecked():
            self._measure_btn.setChecked(False)
        self._fit_pending = False

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
            item = self._graphics.ci.addPlot(row=row, col=0)
            item.showGrid(x=True, y=True, alpha=_GRID_ALPHA)
            item.getAxis("left").setWidth(metrics.plot_axis_width)
            if row:
                item.setXLink(self._panels[0].item)
            is_last = row == len(groups) - 1
            item.getAxis("bottom").setStyle(showValues=is_last)
            if is_last:
                item.setLabel("bottom", "Time", units="s")
            for name in names:
                trace = item.plot(pen=pg.mkPen(color=self._colour_of(name), width=tokens.METRICS.mark))
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
        if self._panels and (self._paused or not self._timer.isActive()):
            self._redraw(force=True)

    def _on_value_shown(self, name: str, shown: bool) -> None:
        trace = self._traces.get(name)
        if trace is not None:
            trace.setVisible(shown)
        self._update_measure()
        self._refresh_hover()

    # ── Toolbar ──────────────────────────────────────────────────────────

    def _on_pause_toggled(self, paused: bool) -> None:
        self._paused = paused
        self._pause_btn.setText("Resume" if paused else "Pause")
        if not paused and self._measuring:
            # Live and measuring do not go together: going live ends it.
            self._held_for_measuring = False
            self.set_measuring(False)
        self.pause_toggled.emit(paused)

    def _on_clear(self) -> None:
        self._engine.clear_buffers()
        self._data = {}
        for trace in self._traces.values():
            trace.setData([], [])
        self._fit_pending = True
        self._update_figures()

    def _on_window_changed(self, _index: int) -> None:
        self._window_seconds = self._window_combo.currentData()
        self._fit_pending = True
        if self._paused:
            self._redraw(force=True)

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

    def _redraw(self, *, force: bool = False) -> None:
        if (self._paused and not force) or not self._traces:
            return
        try:
            for name, trace in self._traces.items():
                x, y = self._engine.snapshot(name)
                self._data[name] = (x, y)
                trace.setData(x, y)
            if not any(x.size for x, _ in self._data.values()):
                return
            if self._regroup_pending:
                self._regroup_pending = False
                if self._groups() != self.panels():
                    self._rebuild_panels()
                    return
            self._place_view()
            self._refresh_hover()
        except Exception:
            # A fault while drawing must not take the tab with it: the
            # plot stops updating and says why in the log.
            logger.exception("Plot redraw failed; the plot is paused")
            self._pause_btn.setChecked(True)

    def _place_view(self) -> None:
        """Keep the newest data in view, or fit everything once."""
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

    def _update_figures(self) -> None:
        """Bring the figures beside the chart up to date."""
        if not self._panels:
            return
        low, high = self._time_in_view()
        for name, row in self._rows.items():
            x, y = self._data.get(name, (np.empty(0), np.empty(0)))
            row.set_stats(visible_stats(x, y, low, high))
        self._update_measure()

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
            rows[0].append(_NO_FIGURE if at_a is None else format_received(at_a))
            rows[1].append(_NO_FIGURE if at_b is None else format_received(at_b))
            change = None if at_a is None or at_b is None else at_b - at_a
            rows[2].append(_NO_FIGURE if change is None else format_change(change))
        self._measure_table.set_figures(columns, rows)

    # ── Under the pointer ────────────────────────────────────────────────

    def _shown_names(self) -> list[str]:
        return [name for name in self._columns if self._rows[name].shown]

    def _held(self, name: str, time: float) -> float | None:
        """What ``name`` last received at or before ``time``, if anything."""
        if name not in self._data:
            return None
        x, y = self._data[name]
        return value_held(x, y, time)

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
        values = []
        for name in self._shown_names():
            value = self._held(name, time)
            values.append(
                (self._colour_of(name), name, _NO_FIGURE if value is None else format_received(value))
            )
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
