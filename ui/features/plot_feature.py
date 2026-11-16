# SPDX-License-Identifier: GPL-3.0-or-later
"""The live plot of a tab, as a module that can be switched off.

The engine (``core/plot_engine.py``) reads the values of the session's
lines from the module's own buffer, so a console that skips lines does
not take them from the plot. It parses only while the plot is shown; a
hidden plot only remembers the most recent lines, so it shows data at
once when opened.

Older time than the engine holds is read from the session recording by
``PlotHistory``, only while the plot is shown.

The view is built on first use. pyqtgraph, which it needs, is loaded
on a thread shortly after the tab is built (``plot_preload``), so the
first Plot opens at once; should it be opened sooner, the pointer shows
it is busy until the plot is ready. It opens in a window of its own, or under the
console, as Preferences say; Pop out and Dock move an open plot either
way. Where the window was is remembered for each port.
"""

from __future__ import annotations

import logging
import math
import os
from datetime import datetime
from typing import TYPE_CHECKING

from PySide6.QtCore import QByteArray, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import QApplication, QFileDialog

from app.constants import FIELD_SEPARATOR, PLOT_PRELOAD_DELAY_MS
from app.file_names import output_base_path
from app.plot_config import PlotConfig, PlotProfileStore
from core.plot_engine import PlotEngine, PlotMode
from core.plot_export import PlotCsvJob, PlotCsvRequest, PlotCsvResult
from core.plot_history import HistorySource
from core.plot_parsers import RegexParserError
from core.records import RecordOrigin
from ui.features.plot_history import PlotHistory
from ui.features.plot_preload import plot_loaded, preload_plot
from ui.formatting import format_count
from ui.module_host import TabContext, TabModule
from ui.themes import tokens
from ui.widgets.toast import Toast
from updater.usage import Feature

if TYPE_CHECKING:
    from core.records import LineRecord
    from ui.widgets.plot_view import PlotExport, PlotView
    from ui.widgets.plot_window import PlotWindow

logger = logging.getLogger(__name__)

#: The name a saved picture or values file is given, before the port and time.
_FILE_NAME = "plot"
_PICTURE_TYPES = {"PNG": ("png", "PNG pictures (*.png)"), "SVG": ("svg", "SVG pictures (*.svg)")}
_VALUES_TYPE = ("csv", "CSV files (*.csv)")
_ALL_FILES = "All files (*)"
_NOT_SAVED = "The plot could not be saved"


class PlotFeature(TabModule):
    """The live plot of one tab.

    Signals:
        visibility_changed(bool):  The plot was shown or hidden.
    """

    reads_lines = True
    visibility_changed = Signal(bool)
    _values_saved = Signal(object)

    def __init__(self, context: TabContext) -> None:
        super().__init__(context)
        self._engine = PlotEngine(self)
        self._engine.set_active(False)
        self._history = PlotHistory(self._engine, context.recording, self)
        self._lines_skipped = 0
        # When the board was lost, each time it was in this session.
        self._breaks: list[datetime] = []
        self._values_job: PlotCsvJob | None = None
        self._values_saved.connect(self.guarded(self._on_values_saved))
        self._view: PlotView | None = None
        self._window: PlotWindow | None = None
        self._visible = False
        self._split_ratio: tuple[int, int] = (1, 1)
        self._profiles = PlotProfileStore(context.config)
        self._active_config = self._profiles.default_profile()
        self._apply_config(self._active_config, emit_toast=False)

    # ── Module ───────────────────────────────────────────────────────────

    def start(self) -> None:
        self.context.toolbar.set_plot_available(True)
        QTimer.singleShot(PLOT_PRELOAD_DELAY_MS, preload_plot)

    def stop(self) -> None:
        self._history.shutdown()
        if self._values_job is not None:
            self._values_job.cancel()
            self._values_job.join()
            self._values_job = None
        if self._window is not None:
            self.set_floating(False)
        if self._view is not None:
            self._view.shutdown()
            self._view.hide()
            self._view.setParent(None)
            self._view.deleteLater()
            self._view = None
        self._engine.set_active(False)
        self.context.toolbar.set_plot_open(False)
        self.context.toolbar.set_plot_available(False)
        if self._visible:
            self._visible = False
            self.visibility_changed.emit(False)

    def take_records(self, records: list[LineRecord], skipped: int) -> None:
        if skipped:
            self._lines_skipped += skipped
            if self._view is not None:
                self._view.set_lines_skipped(self._lines_skipped)
        for record in records:
            if record.origin is RecordOrigin.DEVICE:
                self._engine.process(record.text, record.timestamp)

    def session_started(self) -> None:
        self._history.reset()
        self._lines_skipped = 0
        self._breaks.clear()
        if self._view is not None:
            self._view.reset_session()
            self._view.set_breaks(self._breaks)
        else:
            self._engine.reset()
        self._engine.set_active(self._visible)
        self._show_window_state()

    def session_ended(self, unexpected: bool) -> None:
        self._engine.set_active(False)
        self._show_window_state()

    def connection_lost(self) -> None:
        self._show_window_state()
        figures = self.context.session_figures()
        if figures is None or not figures.disconnects:
            return
        self._breaks.append(figures.disconnects[-1].lost)
        if self._view is not None:
            self._view.set_breaks(self._breaks)

    def connection_back(self, seconds_away: float) -> None:
        self._show_window_state()

    def title_changed(self) -> None:
        self._show_window_state()

    # ── Showing ──────────────────────────────────────────────────────────

    @property
    def engine(self) -> PlotEngine:
        return self._engine

    @property
    def view(self) -> PlotView | None:
        return self._view

    def is_visible(self) -> bool:
        """True when the plot is shown, wherever it is."""
        return self._visible

    def is_floating(self) -> bool:
        """True when the plot is in a window of its own."""
        return self._window is not None

    def toggle(self) -> None:
        self.set_visible(not self._visible)

    @Slot(bool)
    def set_visible(self, show: bool) -> None:
        """Show or hide the plot.

        A plot being opened goes where Preferences say: its own window,
        or under the console. Under the console the split starts even;
        later the split the user dragged is kept across hiding and
        showing.
        """
        if show:
            self._ensure_view()
            if not self._visible:
                self.set_floating(self.context.config.plot_own_window)
        if self._window is not None:
            if show:
                self._show_window()
            else:
                self._hide_window()
        elif show:
            self._view.setVisible(True)
            self._restore_split()
        elif self._view is not None:
            self._remember_split()
            self._view.setVisible(False)
        self._set_visible(show)

    @Slot(bool)
    def set_floating(self, floating: bool) -> None:
        """Move the plot to a window of its own, or back under the console."""
        if self._view is None or floating == self.is_floating():
            return
        panels = self.context.panels
        if floating:
            from ui.widgets.plot_window import PlotWindow

            if self._visible:
                self._remember_split()
            self._window = PlotWindow(self.context.title(), self.context.widget)
            self._window.closed.connect(self.guarded(self._close))
            self._window.take(self._view)
            self._show_window_state()
            if self._visible:
                self._show_window()
        else:
            self._hide_window()
            window, self._window = self._window, None
            window.release()
            window.setParent(None)
            window.deleteLater()
            panels.addWidget(self._view)
            panels.setStretchFactor(1, 1)
            self._view.setVisible(self._visible)
            if self._visible:
                self._restore_split()
        self._view.set_floating(floating)

    def _show_window(self) -> None:
        """Show the plot's window where it last was for this port."""
        window = self._window
        if window.isVisible():
            return
        settings = self.context.port_memory.get(self.context.recorded_port())
        if settings is not None and settings.plot_window:
            window.restoreGeometry(QByteArray.fromBase64(settings.plot_window.encode("ascii")))
        window.show()

    def _hide_window(self) -> None:
        """Hide the plot's window, remembering where it was for this port."""
        window = self._window
        if window is None or not window.isVisible():
            return
        port = self.context.recorded_port()
        if port:
            geometry = bytes(window.saveGeometry().toBase64().data()).decode("ascii")
            self.context.port_memory.remember(port, plot_window=geometry)
        window.hide()

    def _set_visible(self, value: bool) -> None:
        """Record whether the plot is shown, and tell everything that shows it.

        The engine reads values from lines only while the plot is shown
        and connected; it announces even when nothing changed, so a
        listener can bring itself up to date after a tab switch.
        """
        self._visible = value
        self._engine.set_active(value and self.context.is_connected())
        self._history.set_reading(value)
        if value:
            self.context.note_usage(Feature.PLOT)
            if self._engine.mode is PlotMode.MANUAL:
                self.context.note_usage(Feature.PLOT_PATTERN)
        self.context.toolbar.set_plot_open(value)
        self.visibility_changed.emit(value)

    def _ensure_view(self) -> None:
        """Build the view on first use, showing a busy pointer if pyqtgraph is still loading."""
        if self._view is not None:
            return
        waiting = not plot_loaded()
        if waiting:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            from ui.widgets.plot_view import PlotView

            # No minimum of its own: the plot's floor is what its content
            # needs (a chart at least a pane high, its toolbar, and the
            # measuring table while measuring).
            self._view = PlotView(self._engine, self.context.theme_manager, history=self._history)
        finally:
            if waiting:
                QApplication.restoreOverrideCursor()
        self._view.set_lines_skipped(self._lines_skipped)
        self._view.set_breaks(self._breaks)
        self._view.configure_requested.connect(self.guarded(self.open_config_dialog))
        self._view.export_requested.connect(self.guarded(self._on_export_requested))
        self._view.float_requested.connect(self.guarded(self.set_floating))
        self._view.close_requested.connect(self.guarded(self._close))
        self._view.height_wanted.connect(self.guarded(self._give_height))
        self._view.pause_toggled.connect(self.guarded(self._on_pause_toggled))
        self.context.panels.addWidget(self._view)
        self.context.panels.setStretchFactor(1, 1)

    def _close(self) -> None:
        self.set_visible(False)

    def _on_pause_toggled(self, _paused: bool) -> None:
        self._show_window_state()

    def _remember_split(self) -> None:
        sizes = self.context.panels.sizes()
        if len(sizes) == 2 and sizes[0] > 0 and sizes[1] > 0:
            self._split_ratio = (sizes[0], sizes[1])

    def _restore_split(self) -> None:
        """Give the console and the plot the share of the height they last had."""
        panels = self.context.panels
        total = panels.size().height()
        console_share, plot_share = self._split_ratio
        floor = tokens.METRICS.pane_min_height
        console_height = max(floor, total * console_share // (console_share + plot_share))
        panels.setSizes([console_height, max(floor, total - console_height)])

    @Slot(int)
    def _give_height(self, pixels: int) -> None:
        """Add ``pixels`` to the plot's height (fewer, when negative).

        Under the console they are taken from the console, down to its
        floor, and given back to it. A window of its own keeps the size
        the user left it at, so the chart gives them up there: a window
        that resized itself would move away from where it was placed,
        and Windows refuses a size changed while it places a window.
        """
        if self._window is not None:
            return
        panels = self.context.panels
        sizes = panels.sizes()
        if len(sizes) != 2 or sizes[1] == 0:
            return
        console, plot = sizes
        if pixels > 0:
            moved = max(0, min(pixels, console - tokens.METRICS.pane_min_height))
        else:
            moved = pixels
        if moved:
            panels.setSizes([console - moved, plot + moved])

    def _show_window_state(self) -> None:
        """Tell the plot's own window which port it shows, how its
        connection stands, and whether the plot is paused."""
        if self._window is None:
            return
        self._window.set_title(self.context.title())
        if self.context.is_waiting():
            link = "waiting for the board"
        elif self.context.is_connected():
            link = "connected"
        else:
            link = "not connected"
        state = f"{self.context.recorded_port()}, {link}"
        if self._view is not None and self._view.paused:
            state += f"{FIELD_SEPARATOR}paused"
        self._window.set_state(state)

    # ── Saving ───────────────────────────────────────────────────────────

    @Slot(object)
    def _on_export_requested(self, kind: PlotExport) -> None:
        if self._view is None:
            return
        if kind.name in _PICTURE_TYPES:
            self._save_picture(kind)
        else:
            self._save_values(kind)

    def _ask_where(self, title: str, extension: str, file_filter: str) -> str:
        """The file the user chose to save to, or "" if none."""
        config = self.context.config
        suggested = output_base_path(
            config.export_directory, _FILE_NAME, self.context.recorded_port(), datetime.now(),
        ) + f".{extension}"
        parent = self._window if self._window is not None else self.context.widget
        path, _ = QFileDialog.getSaveFileName(parent, title, suggested, f"{file_filter};;{_ALL_FILES}")
        if path:
            config.export_directory = os.path.dirname(path)
        return path

    def _save_picture(self, kind: PlotExport) -> None:
        extension, file_filter = _PICTURE_TYPES[kind.name]
        path = self._ask_where("Save the plot as a picture", extension, file_filter)
        if not path:
            return
        try:
            self._view.save_picture(path)
        except Exception as exc:
            # Whatever went wrong, the user is told and the plot carries on.
            logger.exception("Could not save the plot picture")
            self._report(str(exc) or type(exc).__name__)
            return
        self._saved(f"Saved the plot: {os.path.basename(path)}", path)

    def _save_values(self, kind: PlotExport) -> None:
        from ui.widgets.plot_view import PlotExport

        if self._values_job is not None:
            Toast.warning(self._view, "The plot values are already being saved")
            return
        origin = self._engine.origin
        parser = self._engine.history_parser()
        if origin is None or parser is None:
            Toast.warning(self._view, "There are no values to save yet")
            return
        low, high = (0.0, math.inf)
        if kind is PlotExport.VALUES_SHOWN:
            low, high = self._view.time_in_view()
        extension, file_filter = _VALUES_TYPE
        path = self._ask_where("Save the plot values", extension, file_filter)
        if not path:
            return
        snapshot = self.context.recording(None, None)
        if snapshot is None:
            self._report("The session recording is not available, so the values cannot be read.")
            return
        request = PlotCsvRequest(
            path=path,
            name=self.context.title(),
            port_name=self.context.recorded_port(),
            baud_rate=self.context.recorded_baud(),
            low=low,
            high=high,
            line_ending=self.context.config.log_line_ending.value,
            source=HistorySource(self._engine.generation, origin, tuple(self._engine.columns), parser),
        )
        self._values_job = PlotCsvJob(snapshot, request, self._values_saved.emit)
        self._values_job.start()

    @Slot(object)
    def _on_values_saved(self, result: PlotCsvResult) -> None:
        if self._values_job is not None:
            self._values_job.join()
            self._values_job = None
        if result.cancelled:
            return
        if result.error:
            self._report(result.error)
            return
        self._saved(
            f"Saved {format_count(result.rows, 'row')} of plot values: "
            f"{os.path.basename(result.path)}",
            result.path,
        )

    def _saved(self, message: str, path: str) -> None:
        self.context.note_usage(Feature.PLOT_EXPORT)
        if self._view is None:
            return
        folder = os.path.dirname(path)
        Toast.success(self._view, message, "Show", lambda: self.context.open_folder(folder))

    def _report(self, detail: str) -> None:
        """A banner on the tab; and on the plot too, when it is in a window of its own."""
        self.context.report_problem(_NOT_SAVED, detail)
        if self._window is not None and self._view is not None:
            Toast.error(self._view, f"{_NOT_SAVED}: {detail}")

    # ── Configuration ────────────────────────────────────────────────────

    def recent_lines(self) -> list[str]:
        """Most recent lines, for the Configure plot dialog. Never raises."""
        try:
            return self._engine.recent_lines()
        except Exception:
            logger.exception("plot_engine.recent_lines() failed")
            return []

    def open_config_dialog(self) -> None:
        """Open the Configure plot dialog for this tab."""
        from ui.dialogs.plot_config_dialog import PlotConfigDialog

        dialog = PlotConfigDialog(
            store=self._profiles,
            recent_lines_provider=self.recent_lines,
            current_config=self._active_config,
            parent=self.context.widget,
        )
        dialog.configChanged.connect(self.guarded(self._on_config_changed))
        dialog.exec()

    def use_profile(self, name: str) -> None:
        """Apply the saved profile ``name``, if it exists."""
        profile = self._profiles.get(name)
        if profile is not None:
            self._apply_config(profile, emit_toast=False)

    @Slot(object)
    def _on_config_changed(self, config: PlotConfig) -> None:
        if not isinstance(config, PlotConfig):
            return
        self._apply_config(config, emit_toast=True)
        port = self.context.recorded_port()
        if port and self._active_config is config:
            self.context.port_memory.remember(port, plot_profile=config.name)

    def _apply_config(self, config: PlotConfig, *, emit_toast: bool) -> None:
        """Push a PlotConfig down to the engine; an invalid pattern falls back to auto."""
        try:
            if config.mode == "auto":
                self._engine.set_auto_config()
            else:
                self._engine.set_manual_config(config.pattern)
                if self._visible:
                    self.context.note_usage(Feature.PLOT_PATTERN)
        except RegexParserError as exc:
            logger.warning("Plot config rejected by engine: %s", exc)
            if emit_toast:
                Toast.error(self.context.widget, f"Plot pattern rejected: {exc}")
            self._engine.set_auto_config()
            self._active_config = PlotConfig.auto()
            return

        self._active_config = config
        if emit_toast:
            mode_label = "Auto-detect" if config.mode == "auto" else "manual"
            Toast.info(self.context.widget, f"Plot mode: {mode_label} ({config.name})")
