# SPDX-License-Identifier: GPL-3.0-or-later
"""WireTrace device tab: one tab per serial device.

Central integration module. Each DeviceTab:
  - Owns one DevicePipeline (port, reader thread, session writer) and
    one DeviceSession
  - Shows the first screen (choose a port) until a device has been
    connected, then the session view: toolbar, console header, console,
    command row and status bar
  - Keeps the console on screen after a disconnect, so the last lines
    before an unplug can still be read and exported

The tab never touches the serial port or a log file itself. It asks the
pipeline, and collects received lines from it on a timer, so nothing
the window does can delay reading the port or writing the log.

Messages follow one rule. A toast says something finished. A banner
reports a problem and stays until it is over or dismissed. A dialog
appears only when a decision is needed.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from datetime import datetime
from typing import TYPE_CHECKING, cast

from PySide6.QtCore import QPoint, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QResizeEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QMenu,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app._atomic import atomic_write_text
from app.config import ConfigManager
from app.constants import (
    DISPLAY_CHUNK_LINES,
    DISPLAY_MAX_LAG_MS,
    DISPLAY_SKIP_NOTICE_INTERVAL_MS,
    DISPLAY_TICK_BUDGET_FRACTION,
    FILTER_DEBOUNCE_MS,
    MENU_SELECTION_PREVIEW_CHARS,
    MS_PER_MINUTE,
    MS_PER_SECOND,
    NAME_SEPARATOR,
    RECONNECT_RECORD_WAIT_MS,
    SAMPLE_PORT_DESCRIPTION,
    SAMPLE_PORT_NAME,
    SPOOL_DIRECTORY_NAME,
    TAG_NOTICE,
    TAG_NOTICE_EVENT,
    DisplayMode,
    ExportFormat,
    LineEnding,
    TimestampMode,
    get_cache_dir,
    timestamp_mode_of,
)
from app.diagnostic_log import described
from app.file_names import output_base_path
from app.modules import ALERTS, ANSI_COLOURS, HIGHLIGHTS, PLOT, REPORT, RESTARTS, ModuleSettings
from app.port_memory import PortMemory
from app.saved_commands import SavedCommands
from core.device_pipeline import DevicePipeline, TransportFactory
from core.export_job import ExportRequest, ExportResult
from core.highlight_rules import HighlightStore
from core.log_writer import LogRequest, LogStartError, LogSummary
from core.port_identity import PortIdentity, PortLister, read_identities
from core.reconnect import Ask, Basis, Found
from core.records import notice_record
from core.sample_device import open_sample_transport
from core.serial_settings import LineSettings, LineStates
from core.session import DeviceSession
from core.transport import PortInfo, available_ports, open_serial_transport
from ui import shortcuts
from ui.alert_notifier import AlertKind, AlertNotifier
from ui.device_claims import CLAIMS
from ui.dialogs.messages import confirm
from ui.dialogs.port_settings_dialog import PortSettingsDialog
from ui.formatting import describe_files, format_count
from ui.module_host import ModuleHost, TabContext
from ui.reconnect_watcher import ReconnectWatcher
from ui.themes import tokens
from ui.themes.theme_manager import ThemeManager
from ui.tour.targets import Target, mark
from ui.widgets import banner as banners
from ui.widgets.banner import BannerAction, BannerArea
from ui.widgets.button import Button
from ui.widgets.command_bar import LINE_ENDING_NAMES, CommandBar
from ui.widgets.connect_page import ConnectPage
from ui.widgets.console_header import ConsoleHeader
from ui.widgets.console_view import COLOURS_HIGHLIGHT, ConsoleView
from ui.widgets.search_bar import SearchBar
from ui.widgets.status_bar import DeviceStatusBar
from ui.widgets.status_dot import LinkState
from ui.widgets.styling import set_primary
from ui.widgets.tab_toolbar import TabToolbar
from ui.widgets.toast import Toast
from updater.usage import Feature, Problem

if TYPE_CHECKING:
    from core.session_counters import SessionFigures
    from ui.features.plot_feature import PlotFeature
    from ui.features.report_feature import ReportFeature

logger = logging.getLogger(__name__)

_CLOCK_FORMAT = "%H:%M:%S"
NEW_TAB_TITLE = "New tab"
_SELECTION_NAME = "Selection"

# Keys of the banners a tab can show. One banner per key.
_BANNER_CONNECT = "connect"
_BANNER_LOST = "lost"
_BANNER_SERIAL = "serial"
_BANNER_LOG_WRITE = "log-write"
_BANNER_LOG_END = "log-end"
_BANNER_DROPPED = "dropped"
_BANNER_RECORDING = "recording"
_BANNER_SPOOL = "spool"
_BANNER_SKIPPING = "skipping"
_BANNER_COMMAND = "command"
_BANNER_EXPORT = "export"
_BANNER_MODULE = "module"
_BANNER_WAITING = "waiting"
_BANNER_ASK = "reconnect-ask"


class DeviceTab(QWidget):
    """A single device tab with all UI and core components.

    Signals:
        title_changed(str):           Tab title should update.
        connection_changed(bool):     Connection state changed.
        link_state_changed(object):   The ``LinkState`` shown on the tab.
        timestamp_mode_changed(obj):  Timestamp display mode changed.
        display_mode_changed(obj):    Text or HEX display was chosen.
        following_changed(bool):      The console started or stopped
                                      following the newest line.
        line_ending_changed(obj):     The line ending for commands.
        plot_visibility_changed(bool): Plot panel was shown or hidden.
                                      MainWindow listens to this to
                                      keep the View menu in step with
                                      the actual visibility, whichever
                                      path caused the change.
        usage_noted(object):          A ``Feature`` was used or a
                                      ``Problem`` occurred, for the
                                      usage statistics.
        line_rate_measured(int):      Lines received in the last second.
        reconnect_changed(bool):      Whether this tab waits for a lost
                                      board changed.
        alerts_changed(bool):         Whether alerts are on for this tab
                                      changed.
        alerted():                    This tab alerted: the sound and the
                                      notification were asked for.
    """

    title_changed = Signal(str)
    connection_changed = Signal(bool)
    link_state_changed = Signal(object)
    timestamp_mode_changed = Signal(object)
    display_mode_changed = Signal(object)
    following_changed = Signal(bool)
    line_ending_changed = Signal(object)
    plot_visibility_changed = Signal(bool)
    usage_noted = Signal(object)
    line_rate_measured = Signal(int)
    reconnect_changed = Signal(bool)
    alerts_changed = Signal(bool)
    alerted = Signal()

    def __init__(
        self,
        theme_manager: ThemeManager,
        config_manager: ConfigManager,
        parent: QWidget | None = None,
        transport_factory: TransportFactory = open_serial_transport,
        read_ports: PortLister = read_identities,
        alert_notifier: AlertNotifier | None = None,
        *,
        sample: bool = False,
    ) -> None:
        """A ``sample`` tab reads the built-in sample board (the tour's) and
        no port: nothing about it is remembered, and its board is opened
        at once, having no identity to read."""
        super().__init__(parent)
        self._sample = sample
        if sample:
            transport_factory = open_sample_transport

        self._theme_manager = theme_manager
        self._config_manager = config_manager
        # The sound and notification of alerts, shared by the window's
        # tabs; a tab on its own has its own.
        self._alert_notifier = (
            alert_notifier if alert_notifier is not None else AlertNotifier(config_manager, self)
        )
        self._port_memory = PortMemory(config_manager, kept=not sample)

        # Core modules (per-tab isolation)
        self._session = DeviceSession()
        self._pipeline = DevicePipeline(
            spool_root=os.path.join(get_cache_dir(), SPOOL_DIRECTORY_NAME),
            display_capacity=config_manager.max_console_lines,
            max_pending_lines=config_manager.log_buffer_max_entries,
            flush_interval_ms=config_manager.log_flush_interval_ms,
            transport_factory=transport_factory,
            parent=self,
        )
        # Reconnect: the identity of the board, the search for it once
        # lost, and when it was lost while the tab waits.
        self._watcher = ReconnectWatcher(self, read=read_ports, parent=self)
        # This tab's own choice to wait for a lost board; None follows
        # Preferences.
        self._reconnect_choice: bool | None = None
        self._waiting_since: datetime | None = None
        # The port the board is being reopened on while it returns.
        self._returning: PortIdentity | None = None
        # The port and baud rate to open once the port's identity is read.
        self._pending_open: tuple[str, int] | None = None
        self._record_wait = QTimer(self)
        self._record_wait.setSingleShot(True)
        self._record_wait.setInterval(RECONNECT_RECORD_WAIT_MS)
        self._record_wait.timeout.connect(self._open_pending)
        self._wait_limit = QTimer(self)
        self._wait_limit.setSingleShot(True)
        self._wait_limit.timeout.connect(self.stop_waiting)

        # True from a connect request until the pipeline answers it.
        self._connecting = False
        # The plot whose visibility this tab relays, if one is attached.
        self._followed_plot: PlotFeature | None = None
        # The line settings of the chosen port, and those the connection
        # being opened, or open, uses.
        self._line_settings = LineSettings()
        self._open_line_settings = LineSettings()
        # Port and baud rate of the session that is, or was last,
        # recorded. An export made after the device has gone still
        # names them.
        self._recorded_port = ""
        self._recorded_baud = 0
        # Set by ordered_shutdown; the tab is not used after that.
        self._shut_down = False

        # The name on the tab: the port's description, or a name the
        # user gave it.
        self._port_description = ""
        self._custom_name = ""

        # When the running log started, on the monotonic clock.
        self._log_started = 0.0

        # Lines the console skipped and has not yet reported, and when
        # it last reported any.
        self._skipped_lines = 0
        self._last_skip_notice = 0.0

        # Collects received lines from the pipeline while connected.
        self._display_timer = QTimer(self)
        self._display_timer.timeout.connect(self._drain_display)

        # Re-renders the console once typing in the filter box pauses.
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(FILTER_DEBOUNCE_MS)
        self._filter_timer.timeout.connect(self._apply_filter)

        self._setup_ui()
        self._modules = ModuleHost(
            TabContext(
                widget=self,
                theme_manager=theme_manager,
                config=config_manager,
                port_memory=self._port_memory,
                toolbar=self._toolbar,
                panels=self._console_plot_splitter,
                title=lambda: self.title,
                recorded_port=lambda: self._recorded_port,
                recorded_baud=lambda: self._recorded_baud,
                framing=lambda: self._open_line_settings.describe(),
                is_connected=lambda: self._session.is_connected,
                is_waiting=lambda: self.is_waiting,
                note_usage=self.usage_noted.emit,
                recording=self._pipeline.recording,
                banners=self._banners,
                report_problem=lambda title, detail: self._banners.show_banner(
                    _BANNER_EXPORT, banners.ERROR, title, detail,
                ),
                open_folder=self._open_folder,
                session_figures=self._pipeline.session_figures,
                alert=self._alert,
            ),
            self._pipeline,
            ModuleSettings(config_manager),
            self,
        )
        self._modules.module_failed.connect(self._on_module_failed)
        self._connect_signals()
        self._apply_initial_preferences()
        self.apply_preferences()

    # ── Public API ───────────────────────────────────────────────────────────

    @property
    def session(self) -> DeviceSession:
        return self._session

    @property
    def status_bar(self) -> DeviceStatusBar:
        return self._status_bar

    @property
    def console(self) -> ConsoleView:
        return self._console

    @property
    def is_connected(self) -> bool:
        return self._session.is_connected

    def session_figures(self) -> SessionFigures | None:
        """The exact figures of the current or last session; None before the first."""
        return self._pipeline.session_figures()

    def lines_waiting_for_console(self) -> int:
        """Received lines the console has neither shown nor skipped yet."""
        return self._pipeline.display_waiting()

    @property
    def is_logging(self) -> bool:
        return self._session.is_logging

    @property
    def has_session(self) -> bool:
        """True once a device has been connected in this tab."""
        return self._stack.currentWidget() is self._session_view

    @property
    def port_name(self) -> str:
        return self._session.port_name

    @property
    def link_state(self) -> LinkState:
        if self._waiting_since is not None:
            return LinkState.WAITING
        if self._session.is_logging:
            return LinkState.LOGGING
        if self._session.is_connected:
            return LinkState.CONNECTED
        return LinkState.IDLE

    @property
    def title(self) -> str:
        """The text on the tab."""
        if not self._recorded_port:
            return self._custom_name or NEW_TAB_TITLE
        name = self._custom_name or self._port_description
        return f"{self._recorded_port}{NAME_SEPARATOR}{name}" if name else self._recorded_port

    @property
    def is_sample(self) -> bool:
        """Whether this tab reads the tour's sample board."""
        return self._sample

    def connect_sample(self) -> None:
        """Open the sample board, in a sample tab."""
        if self._sample:
            self._on_connect(SAMPLE_PORT_NAME, self._config_manager.default_baud_rate)

    @property
    def custom_name(self) -> str:
        return self._custom_name

    def rename(self, name: str) -> None:
        """Give the tab a name. An empty name returns to the port's description.

        The name is remembered for the port, so it is back the next time
        that port is connected.
        """
        self._custom_name = name.strip()
        if self._recorded_port:
            self._port_memory.remember(self._recorded_port, tab_name=self._custom_name)
        self._modules.title_changed()
        self.title_changed.emit(self.title)

    def ordered_shutdown(self) -> None:
        """Close the connection and wait until the session is on disk.

        Called when the tab or the application closes. The tab must not
        be used afterwards.
        """
        if self._shut_down:
            return
        self._shut_down = True
        logger.info("Ordered shutdown: %s", self._session.port_name)
        self._display_timer.stop()
        self._filter_timer.stop()
        self._wait_limit.stop()
        self._watcher.shutdown()
        CLAIMS.release(self)
        # Modules first: one may be reading the session recording.
        self._modules.shutdown()
        self._alert_notifier.forget(self)
        self._pipeline.shutdown()
        self._session.reset_connection_state()

    def apply_preferences(self) -> None:
        """Apply the saved preferences to this tab."""
        config = self._config_manager
        self._console.set_font_family(config.font_family)
        self._console.set_font_size(config.font_size)
        self._console.set_max_lines(config.max_console_lines)
        self._console.set_hide_blank_lines(config.hide_blank_lines)
        self._console.set_severity_colours(config.color_mode)
        self._display_timer.setInterval(config.gui_update_interval_ms)
        self._modules.set_interval(config.gui_update_interval_ms)
        self._apply_reconnect()
        modules = ModuleSettings(config)
        self._pipeline.set_restart_detection(modules.is_enabled(RESTARTS))
        ansi = modules.is_enabled(ANSI_COLOURS)
        self._pipeline.set_ansi_colours(ansi)
        self._console.set_ansi_colours(ansi)
        self._console.set_highlights(
            HighlightStore(config).rules() if modules.is_enabled(HIGHLIGHTS) else ()
        )
        self.sync_modules()
        self._modules.apply_preferences()
        self._pipeline.configure(
            display_capacity=config.max_console_lines,
            max_pending_lines=config.log_buffer_max_entries,
            flush_interval_ms=config.log_flush_interval_ms,
        )

    def _apply_initial_preferences(self) -> None:
        """Start the tab in the state the preferences ask for.

        These apply to a new tab only: later they are the user's choice
        for that tab and a change in Preferences does not undo it.
        """
        config = self._config_manager
        self._connect_page.set_baud(config.default_baud_rate)
        self.set_timestamp_mode(timestamp_mode_of(config.timestamp_mode))
        self._console.set_auto_follow(config.auto_scroll)
        self._header.set_following(self._console.following)

    # ── Modules ──────────────────────────────────────────────────────────────

    @property
    def plot(self) -> PlotFeature | None:
        """The live plot of this tab, or None while the plot is switched off."""
        return cast("PlotFeature | None", self._modules.module(PLOT))

    @property
    def report(self) -> ReportFeature | None:
        """The session report of this tab, or None while it is switched off."""
        return cast("ReportFeature | None", self._modules.module(REPORT))

    def open_session_report(self) -> None:
        """Show the report of the current or last session."""
        report = self.report
        if report is not None:
            report.open_report()

    def sync_modules(self) -> None:
        """Attach the modules switched on and detach those switched off."""
        self._modules.sync()
        self._follow_plot()

    @Slot(str)
    def _on_module_failed(self, message: str) -> None:
        self._banners.show_banner(_BANNER_MODULE, banners.ERROR, message, "")
        self._follow_plot()

    def _follow_plot(self) -> None:
        """Relay the visibility of the plot attached now, and announce it."""
        plot = self.plot
        if plot is not None and plot is not self._followed_plot:
            plot.visibility_changed.connect(self.plot_visibility_changed)
        self._followed_plot = plot
        self.plot_visibility_changed.emit(self.is_plot_visible())

    def plot_recent_lines(self) -> list[str]:
        """Most recent lines from the live plot, for the Configure plot dialog."""
        plot = self.plot
        return plot.recent_lines() if plot is not None else []

    def open_plot_config_dialog(self) -> None:
        """Open the Configure plot dialog for this tab."""
        plot = self.plot
        if plot is not None:
            plot.open_config_dialog()

    def set_display_mode(self, mode: DisplayMode) -> None:
        """Switch between TEXT and HEX display modes."""
        self._session.display_mode = mode
        if mode is DisplayMode.HEX:
            self.usage_noted.emit(Feature.HEX)
        self._pipeline.set_display_mode(mode)
        self._toolbar.set_display_mode(mode)
        self.display_mode_changed.emit(mode)

    def set_timestamp_mode(self, mode: TimestampMode) -> None:
        """Show the time of day, or the time since the previous line."""
        self._session.timestamp_mode = mode
        self._console.set_timestamp_mode(mode)
        self._header.set_timestamp_mode(mode)
        self.timestamp_mode_changed.emit(mode)

    @property
    def following(self) -> bool:
        return self._console.following

    def set_following(self, follow: bool) -> None:
        """Stay on the newest line, or stop where the view is."""
        self._console.set_auto_follow(follow)

    @property
    def line_ending(self) -> LineEnding:
        return self._command_bar.line_ending

    def set_line_ending(self, ending: LineEnding) -> None:
        self._command_bar.set_line_ending(ending)

    @property
    def is_exporting(self) -> bool:
        return self._pipeline.is_exporting

    def confirm_close(self) -> bool:
        """Confirm close if it would end a log or an export. Returns
        True to proceed."""
        if not self._session.is_logging and not self.is_exporting:
            return True

        if self._session.is_logging:
            return confirm(
                self, "Close tab", f"{self._session.port_name} is still logging",
                detail=(
                    "Closing the tab stops the log and disconnects the device. "
                    "Everything received so far is written to the file first.\n\n"
                    f"{os.path.basename(self._session.log_file_path)}"
                ),
                proceed="Stop log and close", stay="Keep logging",
            )
        return confirm(
            self, "Close tab", "An export is still being written",
            detail="Closing the tab now cancels the export and removes the unfinished file.",
            proceed="Cancel export and close", stay="Keep exporting",
        )

    # ── UI setup ─────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        metrics = tokens.METRICS
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._banners = BannerArea()
        layout.addWidget(self._banners)

        self._stack = QStackedWidget()
        layout.addWidget(self._stack, 1)

        # First screen: choose a port.
        self._connect_page = ConnectPage(port_note=self._port_note)
        self._stack.addWidget(self._connect_page)

        # Session view: shown from the first connection on.
        self._session_view = QWidget()
        view = QVBoxLayout(self._session_view)
        view.setContentsMargins(0, 0, 0, 0)
        view.setSpacing(0)
        self._stack.addWidget(self._session_view)

        self._toolbar = TabToolbar()
        view.addWidget(self._toolbar)

        self._header = ConsoleHeader()
        view.addWidget(self._header)

        # Search row, hidden until Find is asked for.
        self._search_bar = SearchBar()
        view.addWidget(self._search_bar)

        self._console = ConsoleView()
        mark(self._console, Target.CONSOLE)
        self._console.setMinimumHeight(metrics.pane_min_height)
        self._console.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._search_bar.set_console(self._console)
        self._search_bar.search_changed.connect(
            lambda text: self.usage_noted.emit(Feature.FIND) if text else None
        )

        # Shown over the console while the view is not on the newest line.
        self._new_lines = Button(parent=self._console)
        self._new_lines.setObjectName("newLinesButton")
        self._new_lines.setCursor(Qt.CursorShape.PointingHandCursor)
        set_primary(self._new_lines)
        self._new_lines.hide()

        # The console is the first pane of a vertical splitter, so the
        # live plot can be added below it on demand.
        self._console_plot_splitter = QSplitter(Qt.Orientation.Vertical)
        self._console_plot_splitter.setObjectName("consolePlotSplitter")
        # The panes cannot be collapsed: the Plot button is the one way to
        # close the plot, so a drag cannot make a pane vanish.
        self._console_plot_splitter.setChildrenCollapsible(False)
        self._console_plot_splitter.setHandleWidth(metrics.splitter_handle)
        self._console_plot_splitter.addWidget(self._console)
        self._console_plot_splitter.setStretchFactor(0, 1)
        with contextlib.suppress(Exception):
            handle = self._console_plot_splitter.handle(1)
            if handle is not None:
                handle.setToolTip("Drag to resize the console and the plot")
        view.addWidget(self._console_plot_splitter, 1)

        self._saved_commands = SavedCommands(self._config_manager)
        self._command_bar = CommandBar(self._saved_commands)
        self._command_bar.set_sending_enabled(False)
        view.addWidget(self._command_bar)

        self._status_bar = DeviceStatusBar()
        view.addWidget(self._status_bar)

    def _connect_signals(self) -> None:
        # First screen
        self._connect_page.connect_requested.connect(self._on_connect)
        self._connect_page.ports_wanted.connect(self.refresh_ports)
        self._connect_page.port_chosen.connect(self._on_port_chosen)
        self._connect_page.port_settings_requested.connect(self.edit_port_settings)

        # Toolbar
        self._toolbar.connect_requested.connect(self._on_connect)
        self._toolbar.disconnect_requested.connect(self.disconnect_device)
        self._toolbar.ports_wanted.connect(self.refresh_ports)
        self._toolbar.port_chosen.connect(self._on_port_chosen)
        self._toolbar.port_settings_requested.connect(self.edit_port_settings)
        self._toolbar.dtr_toggled.connect(self._pipeline.set_dtr)
        self._toolbar.rts_toggled.connect(self._pipeline.set_rts)
        self._toolbar.break_requested.connect(self._pipeline.send_break)
        self._console.colours_shown.connect(self._on_colours_shown)
        for used in (self._toolbar.dtr_toggled, self._toolbar.rts_toggled,
                     self._toolbar.break_requested):
            used.connect(lambda *_args: self.usage_noted.emit(Feature.CONTROL_LINES))
        self._toolbar.log_toggled.connect(self.toggle_log)
        self._toolbar.pause_toggled.connect(self._on_pause_toggle)
        self._toolbar.clear_requested.connect(self.clear_console)
        self._toolbar.export_requested.connect(self.export_session)
        self._toolbar.plot_toggled.connect(self._on_plot_toggled)
        self._toolbar.display_mode_changed.connect(self.set_display_mode)
        self._toolbar.search_requested.connect(self.activate_search)
        self._toolbar.hint.connect(lambda text: Toast.warning(self, text))

        # Pipeline
        self._pipeline.connected.connect(self._on_connected)
        self._pipeline.connect_failed.connect(self._on_connect_failed)
        self._pipeline.disconnected.connect(self._on_disconnected)
        self._pipeline.connection_lost.connect(self._on_connection_lost)
        self._pipeline.connection_back.connect(self._on_connection_back)
        self._pipeline.reconnect_failed.connect(self._on_reconnect_failed)
        self._watcher.recorded.connect(self._on_port_recorded)
        self._watcher.found.connect(self._on_board_found)
        self._watcher.asked.connect(self._on_board_asked)
        self._watcher.withdrawn.connect(self._on_ask_withdrawn)
        self._pipeline.transport_error.connect(self._on_serial_error)
        self._pipeline.command_failed.connect(self._on_command_failed)
        self._pipeline.control_failed.connect(lambda reason: Toast.warning(self, reason))
        self._pipeline.lines_changed.connect(self._on_lines_changed)
        self._pipeline.stats_updated.connect(self._on_stats_updated)
        self._pipeline.log_write_failed.connect(self._on_log_write_failed)
        self._pipeline.log_write_recovered.connect(self._on_log_write_recovered)
        self._pipeline.lines_dropped.connect(self._on_lines_dropped)
        self._pipeline.spool_unavailable.connect(self._on_spool_unavailable)
        self._pipeline.writer_error.connect(self._on_writer_error)
        self._pipeline.export_finished.connect(self._on_export_finished)

        # Console header
        self._header.levels_changed.connect(self._console.set_levels)
        self._header.filter_changed.connect(self._on_filter_changed)
        self._header.timestamp_mode_changed.connect(self.set_timestamp_mode)
        self._header.follow_requested.connect(self.set_following)

        # Console
        self._console.level_counts_changed.connect(self._on_level_counts_changed)
        self._console.following_changed.connect(self._on_following_changed)
        self._console.unseen_lines_changed.connect(self._on_unseen_lines_changed)
        self._console.customContextMenuRequested.connect(self._show_console_menu)
        self._new_lines.clicked.connect(lambda: self.set_following(True))

        # Command row
        self._command_bar.command_sent.connect(self._on_command_sent)
        self._command_bar.line_ending_changed.connect(self._on_line_ending_changed)
        self._command_bar.edit_saved_requested.connect(self.edit_saved_commands)
        self._command_bar.saved_command_chosen.connect(
            lambda: self.usage_noted.emit(Feature.SAVED_COMMANDS)
        )

    # ── Connection ───────────────────────────────────────────────────────────

    def refresh_ports(self) -> None:
        """Read the list of serial ports again."""
        self._show_ports()

    def _show_ports(self) -> list[PortInfo]:
        """Read the serial ports, show them in both port lists and return them."""
        ports = available_ports()
        in_use = self._get_in_use_ports()
        self._connect_page.set_ports(ports, in_use)
        self._toolbar.set_ports([(p.device, p.display_name) for p in ports], in_use)
        return ports

    def _get_in_use_ports(self) -> set[str]:
        """Get ports connected in other tabs (via parent MainWindow)."""
        main_win = self.window()
        if hasattr(main_win, 'get_connected_ports'):
            my_port = self._session.port_name if self._session.is_connected else ""
            all_ports = main_win.get_connected_ports()
            return all_ports - {my_port} if my_port else all_ports
        return set()

    def _port_note(self, port: str) -> str:
        return self._port_memory.note(port, LINE_ENDING_NAMES)

    @Slot(str)
    def _on_port_chosen(self, port: str) -> None:
        """Offer the baud rate and line settings this port was last used with."""
        settings = self._port_memory.get(port)
        if settings is not None and settings.baud_rate:
            self._connect_page.set_baud(settings.baud_rate)
            self._toolbar.set_baud_text(str(settings.baud_rate))
        self._show_line_settings(settings.line if settings is not None else LineSettings())

    def _show_line_settings(self, line: LineSettings) -> None:
        self._line_settings = line
        self._connect_page.set_line_settings(line)
        self._toolbar.set_line_settings(line)

    def edit_port_settings(self) -> None:
        """Open the line settings of the chosen port."""
        if self._session.is_connected:
            return
        port = self._toolbar.selected_port if self.has_session else self._connect_page.selected_port
        if not port:
            Toast.warning(self, "Choose a serial port first")
            return
        dialog = PortSettingsDialog(port, self._line_settings, self)
        if dialog.exec():
            self._show_line_settings(dialog.settings)
            self._port_memory.remember(port, line=dialog.settings)

    @Slot(object)
    def _on_lines_changed(self, states: LineStates) -> None:
        self._toolbar.set_line_states(states)
        self._status_bar.set_line_states(states)

    def connect_device(self) -> None:
        """Connect to the port and baud rate currently chosen."""
        if self.has_session:
            port, baud = self._toolbar.selected_port, self._toolbar.selected_baud
        else:
            port, baud = self._connect_page.selected_port, self._connect_page.selected_baud
        if port and baud is not None:
            self._on_connect(port, baud)
        else:
            Toast.warning(self, "Choose a serial port and a valid baud rate first")

    @Slot(str, int)
    def _on_connect(self, port: str, baud: int) -> None:
        if self._shut_down or self._connecting or self._session.is_connected:
            return
        self._connecting = True
        self._open_line_settings = self._line_settings
        self._banners.clear(_BANNER_CONNECT)
        self._status_bar.set_connecting(port)
        # The port is opened once its board's identity has been read, so a
        # board that drops as soon as it is opened is still known.
        self._pending_open = (port, baud)
        if self._sample:
            self._open_pending()
            return
        self._record_wait.start()
        self._watcher.record(port)

    @Slot()
    def _open_pending(self) -> None:
        """Open the port asked for, once its identity is read or the wait is over."""
        if self._pending_open is None or self._shut_down:
            return
        port, baud = self._pending_open
        self._pending_open = None
        self._record_wait.stop()
        self._pipeline.open(port, baud, self._open_line_settings)

    @Slot(str, str)
    def _on_connect_failed(self, port: str, reason: str) -> None:
        self._connecting = False
        logger.error("Could not open %s: %s", port, reason)
        self._banners.show_banner(
            _BANNER_CONNECT, banners.ERROR,
            f"Could not open {port}", f"{reason}.",
        )
        if self.has_session:
            self._status_bar.set_disconnected(port, "could not be opened")
        else:
            self._status_bar.set_idle()

    @Slot()
    def disconnect_device(self) -> None:
        # The pipeline answers with ``disconnected`` once the session is
        # on disk; the tab is reset there. A device that drops on its
        # own arrives at the same place.
        if self._session.is_connected:
            self._pipeline.close()

    @Slot(str, int)
    def _on_connected(self, port_name: str, baud: int) -> None:
        self._connecting = False
        self._session.reset_metrics()
        self._session.reset_logging_state()
        self._session.port_name = port_name
        self._session.baud_rate = baud
        self._session.is_connected = True
        self._recorded_port = port_name
        self._recorded_baud = baud
        self._session.session_start = datetime.now()

        # A new connection starts with a clean console and plot, and
        # with logging off.
        self._banners.clear_all()
        self._console.clear_console()
        self._skipped_lines = 0
        self._display_timer.start()

        self._port_description = SAMPLE_PORT_DESCRIPTION if self._sample else next(
            (p.description for p in available_ports() if p.device == port_name), "",
        )
        self._restore_port_settings(port_name)
        self._port_memory.remember(
            port_name, baud_rate=baud, line_ending=self.line_ending.name,
            line=self._open_line_settings,
        )
        if not _usual_framing(self._open_line_settings):
            self.usage_noted.emit(Feature.LINE_SETTINGS)

        self._stack.setCurrentWidget(self._session_view)
        self._toolbar.select(port_name, baud)
        self._toolbar.set_connected(True)
        self._command_bar.set_sending_enabled(True)
        self._toolbar.set_line_settings(self._open_line_settings)
        self._status_bar.set_connected(port_name, baud, self._open_line_settings.framing)
        self._status_bar.set_not_logging()
        self._status_bar.set_total_lines(0)

        CLAIMS.claim(self, port_name)
        self._modules.session_started()
        self.title_changed.emit(self.title)
        self.connection_changed.emit(True)
        self.link_state_changed.emit(self.link_state)

    # ── Reconnect ────────────────────────────────────────────────────────

    @property
    def is_waiting(self) -> bool:
        """True while the connection is lost and the tab waits for its board."""
        return self._waiting_since is not None

    @property
    def alerts_enabled(self) -> bool:
        """Whether alerts are on for this tab: its own choice, or Preferences."""
        return self._modules.module(ALERTS) is not None

    def set_alerts(self, on: bool) -> None:
        """This tab's own choice, kept until the tab closes."""
        self._modules.choose(ALERTS, on)
        self.alerts_changed.emit(self.alerts_enabled)

    @property
    def reconnect_enabled(self) -> bool:
        """Whether this tab waits for its board when the connection is lost."""
        if self._reconnect_choice is not None:
            return self._reconnect_choice
        return self._config_manager.reconnect

    def set_reconnect(self, on: bool) -> None:
        """This tab's own choice, kept until the tab closes."""
        self._reconnect_choice = on
        self._apply_reconnect()

    def _apply_reconnect(self) -> None:
        """Bring the Reconnect settings into effect, also while waiting."""
        on = self.reconnect_enabled
        self._pipeline.set_reconnect(on)
        self._watcher.set_by_port_name(self._config_manager.reconnect_by_port_name)
        if self.is_waiting:
            if on:
                self._arm_wait_limit()
            else:
                self.stop_waiting()
        self.reconnect_changed.emit(on)

    def _arm_wait_limit(self) -> None:
        """Stop waiting once the time set has passed since the board was lost."""
        minutes = self._config_manager.reconnect_limit_minutes
        if not minutes or self._waiting_since is None:
            self._wait_limit.stop()
            return
        waited = (datetime.now() - self._waiting_since).total_seconds() * MS_PER_SECOND
        self._wait_limit.start(max(0, round(minutes * MS_PER_MINUTE - waited)))

    @Slot(str)
    def _on_colours_shown(self, kind: str) -> None:
        self.usage_noted.emit(Feature.HIGHLIGHTS if kind == COLOURS_HIGHLIGHT else Feature.ANSI)

    def _alert(self, kind: AlertKind, message: str) -> None:
        self._alert_notifier.alert(self, self.title, kind, message)
        self.alerted.emit()
        self.usage_noted.emit(Feature.ALERTS)

    def stop_waiting(self) -> None:
        """Stop waiting for the lost board: the session ends as a disconnect does."""
        if self.is_waiting:
            self._pipeline.close()

    @Slot(str, str)
    def _on_connection_lost(self, port_name: str, reason: str) -> None:
        logger.warning("Waiting for the board on %s to return: %s", port_name, reason)
        CLAIMS.release(self)
        # A passing error reported while the port was open is over with it.
        self._banners.clear(_BANNER_SERIAL)
        self._waiting_since = datetime.now()
        self._status_bar.set_waiting(port_name, self._waiting_since.strftime(_CLOCK_FORMAT))
        self._status_bar.set_line_states(None)
        self._toolbar.set_line_states(None)
        self._command_bar.set_sending_enabled(False)
        self._show_waiting(port_name, "")
        self.usage_noted.emit(Problem.CONNECTION_LOST)
        self._modules.connection_lost()
        self._watcher.watch(self._config_manager.reconnect_by_port_name)
        self._arm_wait_limit()
        self.link_state_changed.emit(self.link_state)

    def _show_waiting(self, port_name: str, detail: str) -> None:
        what = (
            "The log continues in the same file" if self._session.is_logging
            else "The session continues"
        )
        self._banners.show_banner(
            _BANNER_WAITING, banners.WARNING,
            f"Connection to {port_name} lost. Waiting for the same board to return.",
            detail or f"{what} once it is back.",
            (banners.BannerAction("Stop waiting", self.stop_waiting),),
            dismissible=False,
        )

    @Slot(object)
    def _on_board_found(self, found: Found) -> None:
        port = found.identity.port
        self._banners.clear(_BANNER_ASK)
        self._returning = found.identity
        self._show_waiting(self._session.port_name, f"Found on {port}, {found.describe()}. Opening it.")
        self._pipeline.reconnect(
            port, self._recorded_baud, self._open_line_settings, found.describe(),
            self._watcher.check_for(found.identity),
        )

    @Slot(object)
    def _on_board_asked(self, ask: Ask) -> None:
        identity = ask.identity

        def reconnect() -> None:
            self._banners.clear(_BANNER_ASK)
            self._watcher.hold(identity)
            self._returning = identity
            self._pipeline.reconnect(
                identity.port, self._recorded_baud, self._open_line_settings,
                Basis.CONFIRMED.value, self._watcher.check_for(identity),
            )

        def decline() -> None:
            self._banners.clear(_BANNER_ASK)
            self._watcher.decline(identity.port)

        self._banners.show_banner(
            _BANNER_ASK, banners.INFO,
            f"A board of the same model appeared on {identity.port}. "
            "Is it the board this tab was connected to?",
            identity.describe(),
            (banners.BannerAction("Reconnect", reconnect),
             banners.BannerAction("Not this board", decline)),
            dismissible=False,
        )

    @Slot(str, str)
    def _on_reconnect_failed(self, port_name: str, reason: str) -> None:
        if not self.is_waiting:
            return
        self._show_waiting(self._session.port_name, f"Found on {port_name}, not yet ready: {reason}.")
        self._watcher.retry_later()

    @Slot(str)
    def _on_port_recorded(self, port_name: str) -> None:
        if self._pending_open is not None and self._pending_open[0] == port_name:
            self._open_pending()

    @Slot(str)
    def _on_ask_withdrawn(self, port_name: str) -> None:
        self._banners.clear(_BANNER_ASK)

    @Slot(str, int, float)
    def _on_connection_back(self, port_name: str, baud: int, seconds_away: float) -> None:
        logger.info("Reconnected on %s after %.3f s", port_name, seconds_away)
        self.usage_noted.emit(Feature.RECONNECT)
        self._end_waiting_state()
        CLAIMS.claim(self, port_name)
        if self._returning is not None and self._returning.port == port_name:
            self._watcher.adopt(self._returning)
        else:
            self._watcher.record(port_name)
        self._returning = None
        if port_name != self._session.port_name:
            self._session.port_name = port_name
            self._recorded_port = port_name
            # The port list was read before the board came back, so it may
            # not hold this port yet: read it now, so the port is listed with
            # its description and the tab's title takes the same one.
            ports = self._show_ports()
            self._port_description = next(
                (p.description for p in ports if p.device == port_name), "",
            )
            self._toolbar.select(port_name, baud)
            self.title_changed.emit(self.title)
            self._modules.title_changed()
        self._status_bar.set_connected(port_name, baud, self._open_line_settings.framing)
        self._command_bar.set_sending_enabled(True)
        what = "the same log" if self._session.is_logging else "the same session"
        Toast.success(self, f"Reconnected after {seconds_away:.1f} s, continuing {what}")
        self._modules.connection_back(seconds_away)
        self.link_state_changed.emit(self.link_state)

    def _end_waiting_state(self) -> None:
        if self._waiting_since is None:
            return
        self._waiting_since = None
        self._wait_limit.stop()
        self._watcher.stop()
        self._banners.clear(_BANNER_WAITING)
        self._banners.clear(_BANNER_ASK)

    def _restore_port_settings(self, port: str) -> None:
        """Bring back what this port was last used with."""
        settings = self._port_memory.get(port)
        self._custom_name = settings.tab_name if settings is not None else ""
        if settings is None:
            return
        ending = settings.line_ending_choice()
        if ending is not None:
            self._command_bar.set_line_ending(ending)
        plot = self.plot
        if settings.plot_profile and plot is not None:
            plot.use_profile(settings.plot_profile)

    @Slot(str, bool, object)
    def _on_disconnected(
        self, port_name: str, unexpected: bool, summary: LogSummary | None,
    ) -> None:
        """Settle the tab once the pipeline reports the connection closed.

        By now the session, including its final lines, is on disk, so
        what the tab says about the log is what actually happened. The
        console stays on screen with every line received.
        """
        logger.info("Disconnected: %s", port_name)
        lost_at = self._waiting_since
        self._end_waiting_state()
        CLAIMS.release(self)
        self._display_timer.stop()
        self._show_remaining_lines()
        self._session.reset_connection_state()
        self._modules.session_ended(unexpected)

        # A tab that stopped waiting lost its board when the wait began.
        lost = unexpected or lost_at is not None
        moment = (lost_at or datetime.now()).strftime(_CLOCK_FORMAT)
        self._toolbar.set_connected(False)
        self._toolbar.set_line_states(None)
        self._status_bar.set_line_states(None)
        self._command_bar.set_sending_enabled(False)
        self._banners.clear(_BANNER_LOG_WRITE)
        self._banners.clear(_BANNER_SKIPPING)
        self._banners.clear(_BANNER_SERIAL)
        self._status_bar.set_disconnected(
            port_name, f"lost at {moment}" if lost else f"closed at {moment}",
        )
        self._status_bar.set_not_logging()
        self.connection_changed.emit(False)
        self.link_state_changed.emit(self.link_state)

        self._note_log_losses(summary)
        if lost_at is not None:
            self._show_connection_lost(port_name, moment, summary)
        elif unexpected:
            self.usage_noted.emit(Problem.CONNECTION_LOST)
            self._add_notice("Connection lost.")
            self._show_connection_lost(port_name, moment, summary)
        elif summary is not None and self._log_had_losses(summary):
            self._show_log_losses(summary)
        else:
            message = f"Disconnected from {port_name}"
            if summary is not None:
                message += f". {self._log_outcome(summary)}"
            self._toast_with_folder(Toast.info, message, summary)

    def _show_remaining_lines(self) -> None:
        """Draw the lines that were still on their way to the console."""
        while True:
            records, skipped = self._pipeline.take_display(DISPLAY_CHUNK_LINES)
            self._skipped_lines += skipped
            if not records:
                break
            self._console.append_records(records)
        self._last_skip_notice = 0.0
        self._report_skipped_lines()

    def _show_connection_lost(
        self, port_name: str, moment: str, summary: LogSummary | None,
    ) -> None:
        detail = "The device was unplugged or another program took the port."
        if summary is not None:
            detail += f" {self._log_outcome(summary)}"
        actions = [BannerAction("Connect again", self.connect_device)]
        if summary is not None and summary.text_paths:
            folder = os.path.dirname(summary.text_paths[0])
            actions.insert(0, BannerAction("Open log folder", lambda: self._open_folder(folder)))
        actions.insert(-1, BannerAction("Export session", self.export_session))
        self._banners.show_banner(
            _BANNER_LOST, banners.WARNING,
            f"The connection to {port_name} was lost at {moment}", detail, tuple(actions),
        )

    def _note_log_losses(self, summary: LogSummary | None) -> None:
        if summary is not None and (summary.lines_lost or summary.csv_rows_lost):
            self.usage_noted.emit(Problem.LINES_LOST)

    @staticmethod
    def _log_had_losses(summary: LogSummary) -> bool:
        return bool(summary.error or summary.lines_lost or summary.csv_rows_lost)

    @staticmethod
    def _log_outcome(summary: LogSummary) -> str:
        """Describe in one sentence how a log ended."""
        described = describe_files(summary.text_paths, summary.csv_paths)
        outcome = f"The log was closed with {format_count(summary.lines_written, 'line')}"
        if described:
            outcome += f": {described}"
        outcome += "."
        if summary.lines_lost:
            outcome += f" {format_count(summary.lines_lost, 'line')} could not be written."
        if summary.csv_rows_lost:
            outcome += f" {format_count(summary.csv_rows_lost, 'CSV row')} could not be written."
        if summary.error:
            outcome += f" {summary.error}."
        return outcome

    def _show_log_losses(self, summary: LogSummary) -> None:
        self._banners.show_banner(
            _BANNER_LOG_END, banners.ERROR,
            "The log is incomplete", self._log_outcome(summary),
            self._folder_actions(summary),
        )

    def _folder_actions(self, summary: LogSummary | None) -> tuple[BannerAction, ...]:
        if summary is None or not summary.text_paths:
            return ()
        folder = os.path.dirname(summary.text_paths[0])
        return (BannerAction("Open log folder", lambda: self._open_folder(folder)),)

    def _toast_with_folder(self, show, message: str, summary: LogSummary | None) -> None:
        """Show a toast that offers the log's folder, when there is a log."""
        if summary is not None and summary.text_paths:
            folder = os.path.dirname(summary.text_paths[0])
            show(self, message, "Show", lambda: self._open_folder(folder))
        else:
            show(self, message)

    @Slot(str)
    def _on_serial_error(self, error_msg: str) -> None:
        """Report a serial error that did not end the connection.

        An error that does end it is reported by the pipeline as an
        unexpected disconnect instead.
        """
        logger.error("Serial error: %s", error_msg)
        self._banners.show_banner(
            _BANNER_SERIAL, banners.WARNING,
            f"A communication error occurred on {self._session.port_name}",
            f"{error_msg}. The connection is still open.",
        )

    # ── Data ─────────────────────────────────────────────────────────────

    @Slot()
    def _drain_display(self) -> None:
        """Move received lines from the pipeline into the console.

        A tick spends a share of the refresh interval and leaves the
        rest for input and painting. If lines arrive faster than this
        machine draws them, the console skips ahead rather than fall
        ever further behind the device; what it skips is reported and
        is still recorded and logged.
        """
        budget = (
            self._display_timer.interval() * DISPLAY_TICK_BUDGET_FRACTION
            / MS_PER_SECOND
        )
        started = time.perf_counter()
        drawn = 0
        while True:
            records, skipped = self._pipeline.take_display(DISPLAY_CHUNK_LINES)
            self._skipped_lines += skipped
            # Reported before the lines that follow the gap are drawn,
            # so the notice sits where the skipped lines would have been.
            self._report_skipped_lines()
            if not records:
                break
            self._console.append_records(records)
            drawn += len(records)
            elapsed = time.perf_counter() - started
            if elapsed >= budget:
                lines_per_second = drawn / elapsed * DISPLAY_TICK_BUDGET_FRACTION
                self._pipeline.limit_display_backlog(max(
                    DISPLAY_CHUNK_LINES,
                    int(lines_per_second * DISPLAY_MAX_LAG_MS / MS_PER_SECOND),
                ))
                break

    def _report_skipped_lines(self) -> None:
        """Tell the user, at most once per interval, how many lines the
        console skipped. The banner goes once the console keeps up again."""
        now = time.monotonic()
        if (now - self._last_skip_notice) * MS_PER_SECOND < DISPLAY_SKIP_NOTICE_INTERVAL_MS:
            return
        if not self._skipped_lines:
            self._banners.clear(_BANNER_SKIPPING)
            return
        self._last_skip_notice = now
        skipped, self._skipped_lines = self._skipped_lines, 0
        self._add_notice(
            f"{skipped:,} lines were not shown: the display could not "
            "keep up. They are in the session and in the log.",
        )
        self._banners.show_banner(
            _BANNER_SKIPPING, banners.WARNING,
            f"The console is skipping lines to keep up: {skipped:,} not shown",
            "They are all in the session and in the log.",
        )

    def _add_notice(self, message: str, tag: str = TAG_NOTICE) -> None:
        """Show a line of WireTrace's own in the console.

        Lines received earlier may still be on their way to the console,
        so the notice takes the timestamp of the line it follows. The
        timestamps in the console then never run backwards.
        """
        moment = self._console.last_timestamp or datetime.now()
        self._console.append_records([notice_record(message, moment, tag)])

    @Slot(int, int)
    def _on_stats_updated(self, lines_per_sec: int, total_lines: int) -> None:
        self._session.data_rate = lines_per_sec
        self._session.total_lines = total_lines
        self.line_rate_measured.emit(lines_per_sec)
        self._status_bar.set_data_rate(lines_per_sec)
        self._status_bar.set_total_lines(total_lines)
        self._show_log_status()

    @Slot()
    def _on_level_counts_changed(self) -> None:
        self._header.set_level_counts(self._console.level_counts())

    @Slot(bool)
    def _on_following_changed(self, following: bool) -> None:
        self._header.set_following(following)
        self.following_changed.emit(following)

    @Slot(int)
    def _on_unseen_lines_changed(self, count: int) -> None:
        self._new_lines.setVisible(count > 0)
        if count:
            self._new_lines.setText(format_count(count, "new line"))
            self._place_new_lines_button()

    def _place_new_lines_button(self) -> None:
        metrics = tokens.METRICS
        self._new_lines.adjustSize()
        viewport = self._console.viewport().geometry()
        self._new_lines.move(
            viewport.right() - self._new_lines.width() - metrics.space_6,
            viewport.bottom() - self._new_lines.height() - metrics.space_4,
        )
        self._new_lines.raise_()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._new_lines.isVisible():
            self._place_new_lines_button()

    # ── Plot ─────────────────────────────────────────────────────────────────

    @Slot(bool)
    def _on_plot_toggled(self, show: bool) -> None:
        plot = self.plot
        if plot is not None:
            plot.set_visible(show)

    def is_plot_visible(self) -> bool:
        """True when the plot is shown, wherever it is."""
        plot = self.plot
        return plot is not None and plot.is_visible()

    def is_plot_floating(self) -> bool:
        """True when the plot is in a window of its own."""
        plot = self.plot
        return plot is not None and plot.is_floating()

    def toggle_plot(self) -> None:
        """Show the plot if it is hidden, hide it if it is shown."""
        plot = self.plot
        if plot is not None:
            plot.toggle()

    def set_plot_floating(self, floating: bool) -> None:
        """Move the plot to a window of its own, or back under the console."""
        plot = self.plot
        if plot is not None:
            plot.set_floating(floating)

    # ── Logging ──────────────────────────────────────────────────────────

    @Slot()
    def toggle_log(self) -> None:
        """Start a log, or stop the one that is running."""
        if self._session.is_logging:
            self.stop_log()
        else:
            self.start_log()

    def start_log(self) -> None:
        if not self._session.is_connected or self._session.is_logging:
            return
        from ui.dialogs.new_log_dialog import NewLogDialog

        config = self._config_manager
        dialog = NewLogDialog(
            port_name=self._session.port_name,
            default_directory=config.default_log_directory,
            default_format=config.default_log_format,
            split_bytes=config.log_split_bytes,
            parent=self,
        )
        if dialog.exec() != NewLogDialog.DialogCode.Accepted:
            return
        if not self._session.is_connected:
            # The device went away while the dialog was open.
            Toast.warning(self, "Logging was not started: the device is no longer connected")
            return

        log_name = dialog.log_name
        export_format = dialog.export_format
        description = dialog.description
        text_path, csv_path = ExportRequest.paths_for(
            ExportFormat.BOTH if export_format is ExportFormat.CSV else export_format,
            self._output_base_path(dialog.log_directory, log_name),
        )

        request = LogRequest(
            text_path=text_path,
            csv_path=csv_path,
            session_name=log_name,
            port_name=self._session.port_name,
            baud_rate=self._session.baud_rate,
            description=description,
            split_bytes=config.log_split_bytes,
            line_ending=config.log_line_ending.value,
        )
        try:
            started = self._pipeline.start_log(request)
        except LogStartError as exc:
            logger.error("Could not start logging: %s", described(exc))
            self._banners.show_banner(
                _BANNER_LOG_END, banners.ERROR, "Logging could not be started", str(exc),
            )
            return

        self._banners.clear(_BANNER_LOG_END)
        self._session.is_logging = True
        self._session.is_paused = False
        self._session.log_file_path = started.text_path
        self._session.csv_file_path = started.csv_path
        self._session.export_format = export_format
        self._session.log_name = log_name
        self._session.log_comments = description
        self._log_started = time.monotonic()
        self._remember_log_folder(dialog.log_directory)
        self.usage_noted.emit(Feature.LOG)
        if started.csv_path:
            self.usage_noted.emit(Feature.CSV)
        self._toolbar.set_logging(True, False)
        self._show_log_status()
        self.link_state_changed.emit(self.link_state)
        folder = os.path.dirname(started.text_path)
        Toast.success(
            self, f"Logging to {os.path.basename(started.text_path)}",
            "Show", lambda: self._open_folder(folder),
        )

    def _remember_log_folder(self, folder: str) -> None:
        """Make the folder a log was started in the folder of the next one.

        People look for a log where they last put one. The folder is the
        "Log folder" preference, so every tab and the next start use it.
        """
        config = self._config_manager
        if folder != config.default_log_directory:
            config.default_log_directory = folder
            config.save()

    def stop_log(self) -> None:
        if not self._session.is_logging:
            return
        summary = self._pipeline.stop_log()
        self._session.reset_logging_state()
        self._toolbar.set_logging(False)
        self._banners.clear(_BANNER_LOG_WRITE)
        self._show_log_status()
        self.link_state_changed.emit(self.link_state)
        if summary is None:
            return
        self._note_log_losses(summary)
        if self._log_had_losses(summary):
            self._show_log_losses(summary)
        else:
            self._toast_with_folder(Toast.success, self._log_outcome(summary), summary)

    @Slot()
    def _on_pause_toggle(self) -> None:
        if not self._session.is_logging:
            return
        paused = not self._session.is_paused
        self._pipeline.set_log_paused(paused)
        self._session.is_paused = paused
        self._toolbar.set_logging(True, paused)
        self._show_log_status()
        Toast.info(
            self,
            "Logging paused. Lines are still received and shown." if paused
            else "Logging resumed",
        )

    def _show_log_status(self) -> None:
        """Bring the log part of the status bar up to date."""
        if not self._session.is_logging:
            self._status_bar.set_not_logging()
            return
        self._status_bar.set_logging(
            os.path.basename(self._session.log_file_path),
            time.monotonic() - self._log_started,
            self._pipeline.log_progress(),
            paused=self._session.is_paused,
        )

    def open_log_folder(self) -> None:
        """Open the folder of the running log, or the default log folder."""
        if self._session.is_logging and self._session.log_file_path:
            self._open_folder(os.path.dirname(self._session.log_file_path))
        else:
            self._open_folder(self._config_manager.default_log_directory)

    def _open_folder(self, folder: str) -> None:
        if not os.path.isdir(folder):
            Toast.warning(self, f"The folder does not exist yet: {folder}")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(folder)):
            Toast.warning(self, f"The folder could not be opened: {folder}")

    @Slot()
    def clear_console(self) -> None:
        """Clear the console. Later exports start from this point."""
        self._console.clear_console()
        self._pipeline.mark_export_start()

    @Slot()
    def export_session(self) -> None:
        """Save the session to a file chosen in the Export dialog."""
        if self._pipeline.is_exporting:
            Toast.warning(self, "An export is already running")
            return

        from ui.dialogs.export_dialog import ExportDialog

        config = self._config_manager
        dialog = ExportDialog(
            default_directory=config.export_directory,
            port_name=self._recorded_port,
            default_format=config.export_format,
            parent=self,
        )
        if dialog.exec() != ExportDialog.DialogCode.Accepted:
            return
        config.export_directory = dialog.export_directory
        config.export_format = dialog.export_format
        self._start_export(
            dialog.export_name, dialog.export_directory, dialog.export_format,
        )

    def _start_export(self, name: str, directory: str, fmt: ExportFormat) -> None:
        """Save the session in the background. ``_on_export_finished``
        reports the outcome."""
        text_path, csv_path = ExportRequest.paths_for(
            fmt, self._output_base_path(directory, name),
        )
        request = ExportRequest(
            text_path=text_path,
            csv_path=csv_path,
            name=name,
            port_name=self._recorded_port,
            baud_rate=self._recorded_baud,
            split_bytes=self._config_manager.log_split_bytes,
            line_ending=self._config_manager.log_line_ending.value,
        )
        self._banners.clear(_BANNER_EXPORT)
        if not self._pipeline.export(request):
            Toast.warning(self, "There is nothing to export: no data has been recorded")
            return
        self.usage_noted.emit(Feature.EXPORT)

    @Slot(object)
    def _on_export_finished(self, result: ExportResult) -> None:
        if result.cancelled:
            return
        if result.error:
            self._banners.show_banner(
                _BANNER_EXPORT, banners.ERROR, "The export failed", result.error,
            )
            return
        described = describe_files(result.text_paths, result.csv_paths)
        folder = os.path.dirname(result.paths[0]) if result.paths else ""
        Toast.success(
            self, f"Exported {format_count(result.lines, 'line')}: {described}",
            "Show" if folder else "", lambda: self._open_folder(folder),
        )

    def _output_base_path(self, directory: str, name: str) -> str:
        """Build the path, without extension, of a log or export file."""
        return output_base_path(directory, name, self._recorded_port, datetime.now())

    # ── Writer reports ───────────────────────────────────────────────────

    @Slot(str)
    def _on_log_write_failed(self, message: str) -> None:
        self._add_notice(f"The log cannot be written: {message}. Still trying.")
        self._banners.show_banner(
            _BANNER_LOG_WRITE, banners.ERROR,
            f"The log cannot be written: {message}",
            "WireTrace keeps trying. Logging continues by itself once the file "
            "can be written again.",
            (
                BannerAction("Open log folder", self.open_log_folder),
                BannerAction("Stop log", self.stop_log),
            ),
            dismissible=False,
        )

    @Slot()
    def _on_log_write_recovered(self) -> None:
        self._add_notice("The log is being written again.", TAG_NOTICE_EVENT)
        self._banners.clear(_BANNER_LOG_WRITE)
        Toast.success(self, "The log is being written again")

    @Slot(int)
    def _on_lines_dropped(self, count: int) -> None:
        self.usage_noted.emit(Problem.LINES_LOST)
        self._add_notice(
            f"{count:,} lines were not recorded: the disk could not keep up.",
        )
        self._banners.show_banner(
            _BANNER_DROPPED, banners.ERROR,
            f"{format_count(count, 'line')} were not recorded",
            "The disk could not keep up with the device. The lines are marked "
            "in the console and in the log.",
        )

    @Slot(str)
    def _on_writer_error(self, message: str) -> None:
        logger.error("Recording error: %s", message)
        self._add_notice(f"{message}. Recording continues.")
        self._banners.show_banner(
            _BANNER_RECORDING, banners.ERROR,
            "An unexpected error occurred while recording", f"{message}. Recording continues.",
        )

    @Slot(str)
    def _on_spool_unavailable(self, message: str) -> None:
        logger.warning("Session recording unavailable: %s", message)
        self._banners.show_banner(
            _BANNER_SPOOL, banners.WARNING,
            "Export is unavailable for this session", message,
        )

    # ── Command / filter / search ────────────────────────────────────────────

    @Slot(str)
    def _on_command_sent(self, command: str) -> None:
        if not self._session.is_connected:
            Toast.warning(self, "The command was not sent: no device is connected")
            return
        self._banners.clear(_BANNER_COMMAND)
        self._pipeline.send_command(command, self._command_bar.line_ending)

    @Slot(str)
    def _on_command_failed(self, reason: str) -> None:
        logger.error("Command not sent: %s", reason)
        self._banners.show_banner(
            _BANNER_COMMAND, banners.ERROR, "The command was not sent", f"{reason}.",
        )

    @property
    def saved_commands(self) -> SavedCommands:
        return self._saved_commands

    def fill_saved_command(self, number: int) -> None:
        """Put saved command ``number`` (from 1) in the command box.

        Nothing is sent: the command waits there for Enter.
        """
        commands = self._saved_commands.all()
        if not self._session.is_connected:
            Toast.warning(self, "Commands can be sent only while a device is connected")
        elif number > len(commands):
            Toast.info(self, f"There is no saved command {number}")
        else:
            self._command_bar.set_text(commands[number - 1])
            self.usage_noted.emit(Feature.SAVED_COMMANDS)

    @Slot()
    def edit_saved_commands(self) -> None:
        """Open the dialog that edits the saved commands."""
        from ui.dialogs.saved_commands_dialog import SavedCommandsDialog

        dialog = SavedCommandsDialog(self._saved_commands.all(), self)
        if dialog.exec() == SavedCommandsDialog.DialogCode.Accepted:
            self._saved_commands.replace(dialog.commands())

    @Slot(object)
    def _on_line_ending_changed(self, ending: LineEnding) -> None:
        if self._recorded_port:
            self._port_memory.remember(self._recorded_port, line_ending=ending.name)
        self.line_ending_changed.emit(ending)

    @Slot(str)
    def _on_filter_changed(self, text: str) -> None:
        # Choosing the rows again costs time in proportion to the lines
        # held, so it waits until typing pauses.
        self._session.filter_text = text
        if text:
            self.usage_noted.emit(Feature.FILTER)
        self._filter_timer.start()

    @Slot()
    def _apply_filter(self) -> None:
        self._console.set_filter(self._session.filter_text)

    def activate_search(self) -> None:
        if self.has_session:
            self._search_bar.activate()

    def activate_filter(self) -> None:
        if self.has_session:
            self._header.focus_filter()

    def search_next(self) -> None:
        self._search_bar.find_next()

    def search_prev(self) -> None:
        self._search_bar.find_previous()

    def clear_search_filter(self) -> None:
        self._search_bar.deactivate()
        self._header.clear_filter()

    # ── Console menu ─────────────────────────────────────────────────────

    def _selected_phrase(self) -> str:
        """The selected text, if it is a short piece of one line."""
        text = self._console.selected_messages().strip()
        return text if text and "\n" not in text else ""

    @Slot(QPoint)
    def _show_console_menu(self, position: QPoint) -> None:
        menu = self.console_menu()
        menu.exec(self._console.viewport().mapToGlobal(position))
        menu.deleteLater()

    def console_menu(self) -> QMenu:
        """The menu of the console, for what is selected right now."""
        console = self._console
        menu = QMenu(self)

        copy = menu.addAction(shortcuts.menu_text("Copy", "copy"), console.copy)
        copy_messages = menu.addAction(
            shortcuts.menu_text("Copy without timestamps", "copy_messages"), console.copy_messages,
        )
        select_all = menu.addAction(
            shortcuts.menu_text("Select all", "select_all"), console.select_all,
        )
        menu.addSeparator()

        phrase = self._selected_phrase()
        shown = phrase
        if len(shown) > MENU_SELECTION_PREVIEW_CHARS:
            shown = shown[:MENU_SELECTION_PREVIEW_CHARS] + "..."
        only = menu.addAction(
            f'Show only lines with "{shown}"' if phrase else "Show only lines with the selection",
            lambda: self._header.set_filter_text(phrase),
        )
        find = menu.addAction(
            f'Find "{shown}"' if phrase else "Find the selection",
            lambda: self._search_bar.search_for(phrase),
        )
        menu.addSeparator()
        export = menu.addAction("Export selection...", self.export_selection)
        menu.addSeparator()
        menu.addAction("Clear console", self.clear_console)

        for action in (copy, copy_messages, export):
            action.setEnabled(console.has_selection)
        select_all.setEnabled(console.visible_count > 0)
        for action in (only, find):
            action.setEnabled(bool(phrase))
        return menu

    def export_selection(self) -> None:
        """Save the selected text to a file the user chooses."""
        text = self._console.selected_text()
        if not text:
            return
        suggested = self._output_base_path(
            self._config_manager.default_log_directory, _SELECTION_NAME,
        ) + ".txt"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export selection", suggested, "Text files (*.txt);;All files (*)",
        )
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            atomic_write_text(path, text + "\n")
        except OSError as exc:
            logger.error("Could not export the selection: %s", described(exc))
            self._banners.show_banner(
                _BANNER_EXPORT, banners.ERROR, "The selection could not be saved", str(exc),
            )
            return
        self.usage_noted.emit(Feature.EXPORT)
        folder = os.path.dirname(path)
        Toast.success(
            self, f"Saved {format_count(text.count(chr(10)) + 1, 'line')}: {os.path.basename(path)}",
            "Show", lambda: self._open_folder(folder),
        )


def _usual_framing(line: LineSettings) -> bool:
    """Whether a port is opened as most are: 8N1, without flow control."""
    usual = LineSettings()
    return (line.data_bits, line.parity, line.stop_bits, line.flow_control) == (
        usual.data_bits, usual.parity, usual.stop_bits, usual.flow_control,
    )
