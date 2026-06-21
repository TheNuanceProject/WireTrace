# SPDX-License-Identifier: MIT
"""WireTrace live-plot widget.

Visual side of the plotter: a pyqtgraph PlotWidget plus a small
toolbar (Pause / Clear / Reset View / Window / Configure… / Legend),
wrapped in a QStackedLayout that swaps between a status placeholder
and the plot itself based on detection state.

The widget owns no data — it pulls (x, y) snapshots from a PlotEngine
on a 30 FPS timer. Pause halts the redraw but the engine keeps
buffering; resume picks up exactly where the data is now.

Late-bind: PlotView is constructed lazily on first toggle. The engine
may already have detected columns and accumulated samples by then.
``__init__`` queries the engine's state and rebuilds traces from the
existing ring buffers if so.

Configure path: when auto-detect gives up, the placeholder shows a
``[ Configure manually… ]`` button that emits ``configure_requested``.
The same signal is emitted by the toolbar's Configure… button. The
host (DeviceTab) opens the Configure Plot dialog in response.

Theme switches are hot-swapped via ThemeManager's signal — trace pen
colors, plot chrome (background, axis, grid, label) all re-apply
without data loss. The legend is REBUILT rather than retinted because
pyqtgraph caches per-entry label colour at addItem time and
``setLabelTextColor`` doesn't update existing entries on the versions
in our supply chain.

This module owns no domain logic — every parsing, detection, and
buffering decision lives in core/plot_engine.py and core/plot_parsers.py.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING

import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)

from app.constants import ThemeID

if TYPE_CHECKING:
    from core.plot_engine import PlotEngine
    from ui.themes.theme_manager import ThemeManager

logger = logging.getLogger(__name__)


# ── Configuration ────────────────────────────────────────────────────────────

#: Plot redraw interval in milliseconds (~30 FPS).
PLOT_REDRAW_INTERVAL_MS = 33

#: Default toolbar button height — matches LogControlBar (_BTN_H = 26).
_TOOLBAR_BTN_H = 26


# ── Trace Palettes ───────────────────────────────────────────────────────────
#
# Okabe-Ito-derived; designed to be distinguishable for users with the
# common forms of color vision deficiency. Eight colors per theme.

_TRACE_PALETTES: dict[ThemeID, tuple[str, ...]] = {
    ThemeID.STUDIO_LIGHT: (
        "#0072B2",  # blue
        "#D55E00",  # vermillion
        "#009E73",  # bluish green
        "#CC79A7",  # reddish purple
        "#E69F00",  # orange
        "#56B4E9",  # sky blue
        "#B45F06",  # amber (replaces yellow which is illegible on white)
        "#666666",  # grey
    ),
    ThemeID.MIDNIGHT_DARK: (
        "#56B4E9",
        "#FF8C42",
        "#5DD39E",
        "#E58FBF",
        "#FFCC66",
        "#9DC9F0",
        "#FFE066",
        "#BBBBBB",
    ),
}


# ── Plot Chrome (background, axis, grid, label colors) ───────────────────────

_CHROME: dict[ThemeID, dict[str, str]] = {
    ThemeID.STUDIO_LIGHT: {
        "background": "#FFFFFF",
        "axis": "#9E9E9E",
        "grid": "#EEEEEE",
        "tick_text": "#424242",
        "label_text": "#212121",
        "legend_bg": "#FFFFFFE6",   # near-opaque white
        "legend_border": "#BDBDBD",
    },
    ThemeID.MIDNIGHT_DARK: {
        "background": "#1E1E1E",
        "axis": "#5A5A5A",
        "grid": "#2A2A2A",
        "tick_text": "#B0B0B0",
        "label_text": "#E0E0E0",
        "legend_bg": "#1E1E1EE6",
        "legend_border": "#444444",
    },
}


# ── PlotView ─────────────────────────────────────────────────────────────────

class PlotView(QWidget):
    """Live data plot widget.

    Subscribes to a ``PlotEngine`` reference. Renders detected traces on
    a 30 FPS timer. Toolbar provides Pause / Clear / Reset View /
    Window / Legend controls.

    Signals:
        pause_toggled(bool): Emitted when the user toggles plot pause.
                             ``True`` means paused.
    """

    pause_toggled = Signal(bool)
    #: Emitted when the user clicks the toolbar's Configure… button or
    #: the placeholder's "Configure manually" CTA. The host (DeviceTab)
    #: opens the Configure Plot dialog in response.
    configure_requested = Signal()

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
        self._paused = False
        self._traces: dict[str, pg.PlotDataItem] = {}
        self._time_window_seconds: float | None = 60.0
        self._legend_visible = True
        self._legend = None  # pg.LegendItem; created on detection
        # When True, the next redraw lets pyqtgraph autoscale both axes
        # (used for the Reset View button and for the "All" window).
        self._auto_range_pending = False

        self._setup_ui()

        # Engine signal wiring
        self._engine.columns_detected.connect(self._on_columns_detected)
        self._engine.detection_failed.connect(self._on_detection_failed)

        # Late-bind: if the engine has already detected (because it was
        # processing data before this view was constructed), rebuild
        # traces from existing state immediately.
        if self._engine.detection_complete:
            self._on_columns_detected(self._engine.columns)
        elif self._engine.detection_gave_up:
            self._on_detection_failed()

        # Theme handling
        self._apply_theme()
        change_signal = getattr(theme_manager, "theme_changed", None)
        if change_signal is not None:
            try:
                change_signal.connect(self._on_theme_changed)
            except Exception:
                logger.exception("Failed to connect theme_changed signal")

        # Redraw timer
        self._timer = QTimer(self)
        self._timer.setInterval(PLOT_REDRAW_INTERVAL_MS)
        self._timer.timeout.connect(self._redraw)
        self._timer.start()

    # ── UI construction ──────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 4, 8, 4)
        outer.setSpacing(4)

        # Toolbar
        outer.addWidget(self._build_toolbar())

        # Stacked content: placeholder vs plot
        content = QWidget()
        content.setObjectName("plotContent")
        self._stack = QStackedLayout(content)
        self._stack.setContentsMargins(0, 0, 0, 0)

        # Placeholder — composite widget so we can include a Configure
        # CTA when auto-detect fails. Plain text shown by default
        # while we're waiting for data.
        placeholder_holder = QWidget()
        placeholder_holder.setObjectName("plotPlaceholder")
        ph_layout = QVBoxLayout(placeholder_holder)
        ph_layout.setContentsMargins(24, 24, 24, 24)
        ph_layout.setSpacing(12)
        ph_layout.addStretch()

        self._placeholder = QLabel("Waiting for structured data\u2026")
        self._placeholder.setObjectName("plotPlaceholderLabel")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setWordWrap(True)
        ph_layout.addWidget(self._placeholder)

        # CTA button — only visible after detection_failed fires. Lets
        # the engineer recover with one click instead of digging into
        # the View menu.
        cta_row = QHBoxLayout()
        cta_row.addStretch()
        self._configure_cta_btn = QPushButton("Configure manually\u2026")
        self._configure_cta_btn.setObjectName("plotPlaceholderCtaBtn")
        self._configure_cta_btn.setMinimumWidth(180)
        self._configure_cta_btn.setMinimumHeight(28)
        self._configure_cta_btn.setVisible(False)
        self._configure_cta_btn.clicked.connect(
            lambda: self.configure_requested.emit(),
        )
        cta_row.addWidget(self._configure_cta_btn)
        cta_row.addStretch()
        ph_layout.addLayout(cta_row)

        ph_layout.addStretch()
        placeholder_holder.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding,
        )
        self._placeholder_holder = placeholder_holder
        self._stack.addWidget(placeholder_holder)

        # Plot
        self._plot_widget = pg.PlotWidget()
        self._plot_widget.setObjectName("plotWidget")
        self._plot_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding,
        )
        self._plot_widget.showGrid(x=True, y=True, alpha=0.3)
        # Discoverability: the plot area supports pan/zoom/right-click
        # interactions that aren't visible in the chrome. A tooltip
        # keeps the surface clean while letting beginners discover
        # what they can do. Right-click in pyqtgraph opens a view-box
        # menu (auto-range, export image, etc) that's otherwise
        # completely hidden.
        self._plot_widget.setToolTip(
            "Drag to pan · scroll to zoom · right-click for options",
        )
        # Axis labels — these are persistent, theme-independent.
        plot_item = self._plot_widget.getPlotItem()
        plot_item.setLabel("bottom", "Time", units="s")
        plot_item.setLabel("left", "Value")
        # Per-axis tooltips: scroll-zoom on a single axis is a hidden
        # affordance specific to pyqtgraph. Engineers viewing tightly-
        # clustered traces benefit from knowing they can zoom only Y.
        with contextlib.suppress(Exception):
            plot_item.getAxis("bottom").setToolTip(
                "Drag to pan · scroll to zoom (time axis only)",
            )
            plot_item.getAxis("left").setToolTip(
                "Drag to pan · scroll to zoom (Y axis only)",
            )
        # Mouse pan/zoom enabled by default; wheel zooms.
        self._stack.addWidget(self._plot_widget)

        self._stack.setCurrentIndex(0)  # show placeholder until detection
        outer.addWidget(content, 1)

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("plotToolbar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._pause_btn = QPushButton("Pause")
        self._pause_btn.setObjectName("plotPauseBtn")
        self._pause_btn.setFixedHeight(_TOOLBAR_BTN_H)
        self._pause_btn.setMinimumWidth(60)
        self._pause_btn.setCheckable(True)
        self._pause_btn.setToolTip("Pause/resume plot updates (engine keeps buffering)")
        self._pause_btn.toggled.connect(self._on_pause_toggled)

        self._clear_btn = QPushButton("Clear")
        self._clear_btn.setObjectName("plotClearBtn")
        self._clear_btn.setFixedHeight(_TOOLBAR_BTN_H)
        self._clear_btn.setMinimumWidth(60)
        self._clear_btn.setToolTip("Clear plot history (column structure preserved)")
        self._clear_btn.clicked.connect(self._on_clear)

        self._reset_btn = QPushButton("Reset View")
        self._reset_btn.setObjectName("plotResetBtn")
        self._reset_btn.setFixedHeight(_TOOLBAR_BTN_H)
        self._reset_btn.setMinimumWidth(80)
        self._reset_btn.setToolTip(
            "Reset zoom and pan; return to the scrolling window view"
        )
        self._reset_btn.clicked.connect(self._on_reset_view)

        layout.addWidget(self._pause_btn)
        layout.addWidget(self._clear_btn)
        layout.addWidget(self._reset_btn)

        layout.addSpacing(12)
        window_label = QLabel("Window:")
        window_label.setObjectName("plotWindowLabel")
        # The "Window" control is easily misread as "how much data
        # to keep" — clarify that it's purely a view filter.
        window_label.setToolTip(
            "How much recent history to display. The engine keeps "
            "buffering all data regardless of this setting.",
        )
        layout.addWidget(window_label)

        self._window_combo = QComboBox()
        self._window_combo.setObjectName("plotWindowCombo")
        self._window_combo.setFixedHeight(_TOOLBAR_BTN_H)
        self._window_combo.setToolTip(
            "Display the last N seconds of data. 'All' shows the full "
            "ring buffer. Changing this does not discard any samples.",
        )
        self._window_combo.addItem("10 s", 10.0)
        self._window_combo.addItem("30 s", 30.0)
        self._window_combo.addItem("60 s", 60.0)
        self._window_combo.addItem("5 min", 300.0)
        self._window_combo.addItem("All", None)
        self._window_combo.setCurrentIndex(2)  # default 60 s
        self._window_combo.currentIndexChanged.connect(self._on_window_changed)
        layout.addWidget(self._window_combo)

        layout.addSpacing(12)

        # Configure button — opens the Configure Plot dialog. Discoverable
        # in context, alongside the other plot controls.
        self._configure_btn = QPushButton("Configure\u2026")
        self._configure_btn.setObjectName("plotConfigureBtn")
        self._configure_btn.setFixedHeight(_TOOLBAR_BTN_H)
        self._configure_btn.setMinimumWidth(96)
        self._configure_btn.setToolTip(
            "Configure plot mode (auto-detect or manual regex) and "
            "manage saved profiles",
        )
        self._configure_btn.clicked.connect(
            lambda: self.configure_requested.emit(),
        )
        layout.addWidget(self._configure_btn)

        layout.addStretch()

        self._legend_check = QCheckBox("Legend")
        self._legend_check.setObjectName("plotLegendCheck")
        self._legend_check.setChecked(True)
        self._legend_check.toggled.connect(self._on_legend_toggled)
        layout.addWidget(self._legend_check)

        return bar

    # ── Engine signal handlers ───────────────────────────────────────────

    def _on_columns_detected(self, columns: list[str]) -> None:
        # If we've already initialized for these columns, no-op.
        if self._traces and list(self._traces.keys()) == list(columns):
            return

        self._traces.clear()
        self._plot_widget.clear()

        palette = self._current_palette()
        for i, col in enumerate(columns):
            color = palette[i % len(palette)]
            pen = pg.mkPen(color=color, width=1.6)
            trace = self._plot_widget.plot(pen=pen, name=col)
            self._traces[col] = trace

        # Build the legend AFTER traces exist so the theme-aware label
        # colour is baked in at construction time. pyqtgraph caches the
        # text colour per legend entry; rebuilding is the only reliable
        # way to retheme.
        self._rebuild_legend()

        self._stack.setCurrentIndex(1)
        # Force a fresh autoscale on the next redraw so the user
        # sees the data immediately rather than empty default ranges.
        self._auto_range_pending = True

    def _rebuild_legend(self) -> None:
        """Destroy the existing legend (if any) and build a fresh one.

        pyqtgraph's ``setLabelTextColor`` does not retroactively update
        existing legend entries on the versions in our supply chain —
        the colour is baked in when the entry is added. Rebuilding the
        legend from scratch is the only reliable way to apply theme
        changes. Used both at first detection and on every theme switch.
        """
        self._remove_legend()

        self._legend = self._plot_widget.addLegend(offset=(-10, 10))
        # Discoverability: pyqtgraph supports click-to-toggle on legend
        # entries (clicking a label hides or shows that trace) — a
        # genuinely useful affordance that is completely invisible
        # without a hint. One tooltip on the legend container covers
        # all entries without per-entry duplication.
        with contextlib.suppress(Exception):
            self._legend.setToolTip(
                "Click an entry to show or hide that trace",
            )
        self._apply_legend_styling()

        # Re-add each existing trace as a legend entry. addLegend()
        # auto-picks-up traces created via plot_widget.plot(name=...),
        # but that auto-pickup happens only at addLegend time and only
        # for items with names. Calling addItem explicitly guarantees
        # entries exist regardless of pyqtgraph version.
        for col, trace in self._traces.items():
            with contextlib.suppress(Exception):
                self._legend.addItem(trace, name=col)

        if not self._legend_visible:
            self._legend.setVisible(False)

    def _remove_legend(self) -> None:
        """Remove the LegendItem from the plot scene.

        Single source of truth for legend teardown. Used by:
          - ``_rebuild_legend()`` before creating a fresh legend
          - ``reset_session()`` on disconnect, so the next detection
            doesn't accumulate orphaned legends in the scene

        ``PlotWidget.clear()`` removes PlotDataItems but NOT the
        LegendItem (the legend is attached to the ViewBox as a
        separate scene item via ``addLegend``). Without explicit
        removal here, every disconnect-reconnect cycle would leave
        the previous legend in the scene and the new one would stack
        on top — the exact symptom seen in early reports where the
        legend doubled, tripled, etc. across reconnects.

        Idempotent: safe to call when no legend exists.
        """
        plot_item = self._plot_widget.getPlotItem()

        # pyqtgraph tracks the legend on the PlotItem itself as
        # ``plot_item.legend``. Remove from the scene first (this is
        # what actually deletes the visual element), then null the
        # PlotItem's reference so subsequent addLegend() calls don't
        # find a stale pointer.
        legend = getattr(plot_item, "legend", None)
        if legend is not None:
            with contextlib.suppress(Exception):
                scene = legend.scene()
                if scene is not None:
                    scene.removeItem(legend)
            with contextlib.suppress(Exception):
                plot_item.legend = None

        # Also remove our tracked reference if it differs (defensive
        # against any pyqtgraph version that doesn't update
        # ``plot_item.legend`` symmetrically). Idempotent: this block
        # is a no-op when ``self._legend`` is None or already the
        # same object we just removed.
        if self._legend is not None and self._legend is not legend:
            with contextlib.suppress(Exception):
                scene = self._legend.scene()
                if scene is not None:
                    scene.removeItem(self._legend)

        self._legend = None

    def _on_detection_failed(self) -> None:
        self._placeholder.setText(
            "<b>Plot couldn't auto-detect your format.</b><br><br>"
            "Click below to declare a regex pattern manually \u2014 "
            "the dialog includes recent lines from this session "
            "as reference and a 'Capture from sample' assistant "
            "that scaffolds the pattern for you.",
        )
        self._placeholder.setTextFormat(Qt.TextFormat.RichText)
        self._configure_cta_btn.setVisible(True)
        self._stack.setCurrentIndex(0)

    # ── Toolbar handlers ─────────────────────────────────────────────────

    def _on_pause_toggled(self, checked: bool) -> None:
        self._paused = checked
        self._pause_btn.setText("Resume" if checked else "Pause")
        self.pause_toggled.emit(checked)

    def _on_clear(self) -> None:
        self._engine.clear_buffers()
        for trace in self._traces.values():
            trace.setData([], [])
        # After clear, request a fresh autoscale so the empty plot
        # snaps back to neutral instead of staying zoomed somewhere.
        self._auto_range_pending = True

    def _on_reset_view(self) -> None:
        """Reset zoom/pan and return to the scrolling-window view.

        Behaviour:
          - If a fixed window is selected (10 s / 30 s / 60 s / 5 min),
            the next redraw re-anchors X to the latest sample and lets
            Y autorange to the data within that window.
          - If "All" is selected, both axes autorange to the entire
            ring buffer.
        """
        self._auto_range_pending = True
        # Take the user out of "manual zoom" mode if they panned.
        self._plot_widget.getPlotItem().enableAutoRange(axis="y", enable=True)

    def _on_window_changed(self, _idx: int) -> None:
        self._time_window_seconds = self._window_combo.currentData()
        # Whenever the window changes, refit so the user sees the
        # right slice immediately.
        self._auto_range_pending = True

    def _on_legend_toggled(self, checked: bool) -> None:
        self._legend_visible = checked
        if self._legend is not None:
            self._legend.setVisible(checked)

    # ── Redraw ───────────────────────────────────────────────────────────

    def _redraw(self) -> None:
        if self._paused:
            return
        if not self._traces:
            return

        try:
            has_data = False
            for col, trace in self._traces.items():
                x, y = self._engine.snapshot(col)
                if x.size == 0:
                    trace.setData([], [])
                    continue
                trace.setData(x, y)
                has_data = True

            if not has_data:
                # Nothing to anchor on yet — leave the view alone.
                return

            plot_item = self._plot_widget.getPlotItem()

            if self._auto_range_pending:
                # User pressed Reset View, or the window changed, or
                # we just gained traces — let pyqtgraph fit everything
                # once, then resume the windowed-anchor behaviour.
                if self._time_window_seconds is None:
                    plot_item.enableAutoRange(axis="xy", enable=True)
                else:
                    plot_item.enableAutoRange(axis="y", enable=True)
                    latest = self._engine.latest_x
                    self._plot_widget.setXRange(
                        max(0.0, latest - self._time_window_seconds),
                        latest,
                        padding=0.02,
                    )
                self._auto_range_pending = False
                return

            if self._time_window_seconds is not None:
                latest = self._engine.latest_x
                self._plot_widget.setXRange(
                    max(0.0, latest - self._time_window_seconds),
                    latest,
                    padding=0.02,
                )
            # If window is "All", leave pyqtgraph in auto-range from the
            # last Reset View; user pan/zoom takes over from there.
        except Exception:
            # Defensive boundary — any pyqtgraph rendering exception is
            # logged and the plot pauses cleanly. Tab stays alive.
            logger.exception("Plot redraw failed; pausing plot")
            self._paused = True
            self._pause_btn.setChecked(True)

    # ── Theme handling ───────────────────────────────────────────────────

    def _current_theme_id(self) -> ThemeID:
        cur = getattr(self._theme_manager, "current_theme", None)
        if isinstance(cur, ThemeID):
            return cur
        return ThemeID.STUDIO_LIGHT

    def _current_palette(self) -> tuple[str, ...]:
        return _TRACE_PALETTES[self._current_theme_id()]

    def _current_chrome(self) -> dict[str, str]:
        return _CHROME[self._current_theme_id()]

    def _apply_theme(self) -> None:
        chrome = self._current_chrome()

        self._plot_widget.setBackground(chrome["background"])

        plot_item = self._plot_widget.getPlotItem()
        axis_pen = pg.mkPen(color=chrome["axis"], width=1)
        tick_color = QColor(chrome["tick_text"])
        for axis_name in ("left", "bottom", "right", "top"):
            axis = plot_item.getAxis(axis_name)
            axis.setPen(axis_pen)
            axis.setTextPen(tick_color)
        # Re-apply axis labels with theme-coloured text. pyqtgraph picks
        # up the colour from the foreground option but also accepts
        # direct CSS; we use the option to keep behaviour consistent.
        plot_item.setLabel(
            "bottom", "Time", units="s", color=chrome["label_text"],
        )
        plot_item.setLabel(
            "left", "Value", color=chrome["label_text"],
        )

        # Grid alpha remains; pyqtgraph computes the grid colour from
        # the foreground option, which we set globally for new items.
        pg.setConfigOption("foreground", chrome["label_text"])

        # Legend theming (if it exists)
        self._apply_legend_styling()

    def _apply_legend_styling(self) -> None:
        """Make the legend readable in the current theme.

        pyqtgraph's default legend renders white-on-white in light
        themes and is barely visible in either theme — this fixes both.
        """
        if self._legend is None:
            return
        chrome = self._current_chrome()
        try:
            self._legend.setLabelTextColor(chrome["label_text"])
        except Exception:
            # Older pyqtgraph versions may lack this method.
            logger.debug("Legend setLabelTextColor unavailable")
        try:
            self._legend.setBrush(chrome["legend_bg"])
        except Exception:
            logger.debug("Legend setBrush unavailable")
        try:
            self._legend.setPen(pg.mkPen(chrome["legend_border"], width=1))
        except Exception:
            logger.debug("Legend setPen unavailable")

    def _on_theme_changed(self, *_: object) -> None:
        # Re-apply chrome (background, axes, labels). The legend is
        # handled separately because retroactively patching pyqtgraph's
        # legend label colours is unreliable.
        self._apply_theme()
        # Re-pen each existing trace using the new palette
        palette = self._current_palette()
        for i, trace in enumerate(self._traces.values()):
            pen = pg.mkPen(color=palette[i % len(palette)], width=1.6)
            trace.setPen(pen)
        # Rebuild the legend from scratch — this is the only reliable
        # way to update label colours across pyqtgraph versions.
        if self._traces:
            self._rebuild_legend()

    # ── Public API ───────────────────────────────────────────────────────

    def reset_session(self) -> None:
        """Reset for a new connection cycle without killing the view.

        This is the per-disconnect/reconnect teardown. Critically, it
        leaves the redraw timer running so a subsequent reconnect
        immediately sees fresh data rendered. Used by ``DeviceTab``
        from ``ordered_shutdown``.

        Clears: detected columns, ring buffers, traces, the legend,
        and toolbar transient state. Returns the widget to its
        placeholder state.
        """
        self._engine.reset()
        self._traces.clear()
        self._plot_widget.clear()
        # CRITICAL: PlotWidget.clear() removes PlotDataItems (traces)
        # but NOT the LegendItem — the legend is a separate scene item
        # attached to the ViewBox via addLegend(). Without explicit
        # removal, every disconnect-reconnect cycle would leave the
        # previous legend orphaned in the scene; the next detection's
        # addLegend() would stack a new one on top. _remove_legend()
        # is the single source of truth for legend teardown.
        self._remove_legend()
        # Reset placeholder text + hide the manual-configure CTA. The
        # CTA reappears only if detection_failed fires again on the
        # next session.
        self._placeholder.setText("Waiting for structured data\u2026")
        self._placeholder.setTextFormat(Qt.TextFormat.AutoText)
        self._configure_cta_btn.setVisible(False)
        self._stack.setCurrentIndex(0)
        # Clear toolbar transient state so a re-opened plot starts
        # fresh rather than inheriting stale settings.
        if self._pause_btn.isChecked():
            self._pause_btn.setChecked(False)
        self._auto_range_pending = False

    # Backwards-compat alias for any caller still using ``reset()``.
    # The connection cycle uses ``reset_session()`` going forward.
    reset = reset_session

    def shutdown(self) -> None:
        """Stop the redraw timer permanently and detach from the theme manager.

        Called only on tab close / final widget teardown. NOT called
        on disconnect — a per-connection cycle uses ``reset_session()``.
        Stopping the timer here was previously bug B: stopping it on
        every disconnect meant the next connection's data never
        rendered because the redraw never fired again.
        """
        # B7: the theme manager outlives this widget. A lingering
        # theme_changed → _on_theme_changed connection would keep this
        # PlotView alive after the tab closes (a leak proportional to the
        # number of tab cycles) and could invoke the slot on a
        # partially-destroyed widget if the theme changes afterwards.
        # Disconnect first, mirroring the defensive getattr used when the
        # signal was connected in __init__.
        change_signal = getattr(self._theme_manager, "theme_changed", None)
        if change_signal is not None:
            with contextlib.suppress(RuntimeError, TypeError):
                change_signal.disconnect(self._on_theme_changed)

        self._timer.stop()
