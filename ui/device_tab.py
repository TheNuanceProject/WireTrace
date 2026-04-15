# SPDX-License-Identifier: MIT
"""WireTrace device tab — one tab per connected serial device.

Central integration module. Each DeviceTab:
  - Owns one SerialManager, SerialReader, LogEngine, CSVEngine, DeviceSession
  - Contains all UI widgets (console, connection, command, search, filter, status, log controls)
  - Implements progressive disclosure (spec section 6.1)
  - Manages complete tab lifecycle (spec section 4.5)
  - Guarantees ordered shutdown: stop reader → flush → close port → release
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
from datetime import datetime

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QLabel,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from app.constants import (
    FILENAME_DEFAULT_NAME,
    FILENAME_MAX_LENGTH,
    FILENAME_TIMESTAMP_FORMAT,
    LOG_TIMESTAMP_FORMAT,
    ExportFormat,
    TimestampMode,
)
from core.csv_engine import CSVEngine
from core.log_engine import LogConfig, LogEngine
from core.serial_manager import SerialManager
from core.serial_reader import SerialReader
from core.session import DeviceSession
from core.tag_detector import TagDetector
from ui.themes.theme_manager import ThemeManager
from ui.widgets.command_bar import CommandBar
from ui.widgets.connection_panel import ConnectionPanel
from ui.widgets.console_view import ConsoleView
from ui.widgets.filter_bar import FilterBar
from ui.widgets.log_control_bar import LogControlBar
from ui.widgets.search_bar import SearchBar
from ui.widgets.status_bar import DeviceStatusBar
from ui.widgets.toast import Toast

logger = logging.getLogger(__name__)


class DeviceTab(QWidget):
    """A single device tab with all UI and core components.

    Signals:
        title_changed(str):        Tab title should update.
        connection_changed(bool):  Connection state changed.
        close_requested():         User wants to close this tab.
    """

    title_changed = Signal(str)
    connection_changed = Signal(bool)
    close_requested = Signal()
    timestamp_mode_changed = Signal(object)  # TimestampMode — bubbles to MainWindow

    def __init__(
        self,
        theme_manager: ThemeManager,
        config_manager=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        self._theme_manager = theme_manager
        self._config_manager = config_manager

        # Core modules (per-tab isolation)
        self._session = DeviceSession()
        self._serial_manager = SerialManager(self)
        self._serial_reader: SerialReader | None = None
        self._log_engine: LogEngine | None = None
        self._csv_engine: CSVEngine | None = None
        self._tag_detector = TagDetector()

        # Build UI
        self._setup_ui()
        self._connect_signals()

        # Progressive disclosure: start with only connection panel
        self._set_connected_ui_visible(False)

        # Load available ports
        self._refresh_ports()

    # ══════════════════════════════════════════════════════════════════════
    # PUBLIC API
    # ══════════════════════════════════════════════════════════════════════

    @property
    def session(self) -> DeviceSession:
        return self._session

    @property
    def is_connected(self) -> bool:
        return self._session.is_connected

    @property
    def is_logging(self) -> bool:
        return self._session.is_logging

    @property
    def port_name(self) -> str:
        return self._session.port_name

    def ordered_shutdown(self) -> None:
        """Ordered shutdown per spec section 4.5."""
        logger.info("Ordered shutdown: %s", self._session.port_name)

        # 1. Disconnect data relay and stop reader
        if self._serial_reader is not None:
            # Stop data flow: disconnect relay signal first.
            # Already-disconnected signals raise RuntimeError/TypeError —
            # safe to suppress (idempotent teardown).
            with contextlib.suppress(RuntimeError, TypeError):
                self._serial_manager.data_received.disconnect(
                    self._serial_reader.enqueue_data)
            self._serial_reader.stop()
            self._serial_reader.wait(2000)
            self._serial_reader = None

        # 2-3. Stop logging (flush + close files)
        if self._log_engine is not None:
            if self._log_engine.is_logging:
                self._log_engine.stop_logging()
            self._log_engine.stop()
            self._log_engine.wait(2000)
            self._log_engine = None

        self._csv_engine = None

        # 4. Close serial port
        self._serial_manager.close()

        # 5. Update session
        self._session.reset_connection_state()

    def apply_theme(self) -> None:
        """Re-apply tag colors from current theme.

        set_tag_colors triggers a full rebuild of all console lines,
        so existing data is immediately visible in the new theme.
        """
        self._console.set_tag_colors(self._theme_manager.get_tag_colors())

    def set_display_mode(self, mode) -> None:
        """Switch between TEXT and HEX display modes."""
        from app.constants import DisplayMode
        self._session.display_mode = mode
        if self._serial_reader is not None:
            self._serial_reader.display_mode = mode
        Toast.info(self, f"Display mode: {'HEX' if mode == DisplayMode.HEX else 'Text'}")

    def confirm_close(self) -> bool:
        """Confirm close if logging is active. Returns True to proceed."""
        if not self._session.is_logging:
            return True

        msg = QMessageBox(self)
        msg.setWindowTitle("Close Tab")
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setText(f"Logging is active on {self._session.port_name}.")
        msg.setInformativeText(
            "Closing will stop logging and disconnect the device."
        )
        cancel_btn = msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        close_btn = msg.addButton("Close Tab", QMessageBox.ButtonRole.AcceptRole)
        msg.setDefaultButton(cancel_btn)
        msg.exec()
        return msg.clickedButton() == close_btn

    # ══════════════════════════════════════════════════════════════════════
    # UI SETUP
    # ══════════════════════════════════════════════════════════════════════

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Connection panel (always visible)
        self._connection_panel = ConnectionPanel()
        layout.addWidget(self._connection_panel)

        # Log controls
        self._log_control_bar = LogControlBar()
        layout.addWidget(self._log_control_bar)

        # Empty state placeholder (shown before connection)
        self._placeholder = self._build_placeholder()
        layout.addWidget(self._placeholder, 1)

        # Console (with padding so it doesn't touch edges)
        self._console = ConsoleView()
        self._console.set_tag_colors(self._theme_manager.get_tag_colors())
        console_wrapper = QWidget()
        cw_layout = QVBoxLayout(console_wrapper)
        cw_layout.setContentsMargins(8, 4, 8, 4)
        cw_layout.setSpacing(0)
        cw_layout.addWidget(self._console)
        self._console_wrapper = console_wrapper
        layout.addWidget(console_wrapper, 1)

        # Command bar
        self._command_bar = CommandBar()
        layout.addWidget(self._command_bar)

        # Filter bar (always visible when connected)
        self._filter_bar = FilterBar()
        layout.addWidget(self._filter_bar)

        # Search bar (hidden by default, shown via Ctrl+F — appears below filter)
        self._search_bar = SearchBar()
        self._search_bar.set_console(self._console)
        layout.addWidget(self._search_bar)

        # Status bar
        self._status_bar = DeviceStatusBar()
        layout.addWidget(self._status_bar)

    def _connect_signals(self) -> None:
        # Connection
        self._connection_panel.connect_requested.connect(self._on_connect)
        self._connection_panel.disconnect_requested.connect(self._on_disconnect)
        self._connection_panel.refresh_requested.connect(self._refresh_ports)
        self._connection_panel.error_feedback.connect(
            lambda msg: Toast.warning(self, msg)
        )

        # Serial manager
        self._serial_manager.connected.connect(self._on_connected)
        self._serial_manager.disconnected.connect(self._on_disconnected)
        self._serial_manager.error_occurred.connect(self._on_serial_error)

        # Log controls
        self._log_control_bar.log_on_clicked.connect(self._on_log_on)
        self._log_control_bar.log_off_clicked.connect(self._on_log_off)
        self._log_control_bar.pause_clicked.connect(self._on_pause_toggle)
        self._log_control_bar.clear_clicked.connect(self._on_clear)
        self._log_control_bar.export_clicked.connect(self._on_export)

        # Command bar
        self._command_bar.command_sent.connect(self._on_command_sent)

        # Filter
        self._filter_bar.filter_changed.connect(self._on_filter_changed)

        # Console line count
        self._console.line_count_changed.connect(self._on_line_count_changed)

        # Timestamp toggle
        self._status_bar.timestamp_mode_toggled.connect(self._on_timestamp_toggled)

    def _build_placeholder(self) -> QWidget:
        """Build a clean empty-state placeholder shown before device connection."""
        from PySide6.QtGui import QFont as _QFont

        placeholder = QWidget()
        placeholder.setObjectName("emptyTabPlaceholder")
        lay = QVBoxLayout(placeholder)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setSpacing(6)

        title = QLabel("No Device Connected")
        title.setObjectName("placeholderTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tf = _QFont()
        tf.setPointSize(15)
        tf.setWeight(_QFont.Weight.DemiBold)
        title.setFont(tf)

        subtitle = QLabel(
            "Select a port and baud rate above, then click Connect."
        )
        subtitle.setObjectName("placeholderSubtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sf = subtitle.font()
        sf.setPointSize(11)
        subtitle.setFont(sf)

        shortcut_text = QLabel(
            "Ctrl+T  New Tab   ·   Ctrl+N  Start Log   ·   Ctrl+F  Search"
        )
        shortcut_text.setObjectName("placeholderHint")
        shortcut_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scf = shortcut_text.font()
        scf.setPointSize(9)
        shortcut_text.setFont(scf)

        lay.addStretch(3)
        lay.addWidget(title)
        lay.addSpacing(4)
        lay.addWidget(subtitle)
        lay.addStretch(2)
        lay.addWidget(shortcut_text)
        lay.addSpacing(24)

        return placeholder

    def _set_connected_ui_visible(self, visible: bool) -> None:
        self._log_control_bar.setVisible(visible)
        self._console_wrapper.setVisible(visible)
        self._command_bar.setVisible(visible)
        self._filter_bar.setVisible(visible)
        self._log_control_bar.set_connected(visible)
        self._command_bar.set_enabled(visible)
        # Toggle placeholder vs console
        self._placeholder.setVisible(not visible)

    # ══════════════════════════════════════════════════════════════════════
    # CONNECTION
    # ══════════════════════════════════════════════════════════════════════

    def _refresh_ports(self) -> None:
        ports = SerialManager.available_ports()
        in_use = self._get_in_use_ports()
        self._connection_panel.set_ports(
            [(p.device, p.display_name) for p in ports],
            in_use_ports=in_use,
        )

    def _get_in_use_ports(self) -> set[str]:
        """Get ports connected in other tabs (via parent MainWindow)."""
        main_win = self.window()
        if hasattr(main_win, 'get_connected_ports'):
            my_port = self._session.port_name if self._session.is_connected else ""
            all_ports = main_win.get_connected_ports()
            return all_ports - {my_port} if my_port else all_ports
        return set()

    @Slot(str, int)
    def _on_connect(self, port: str, baud: int) -> None:
        Toast.info(self, f"Connecting to {port}...")
        self._status_bar.set_status(f"Connecting to {port}...")
        if not self._serial_manager.open(port, baud):
            Toast.error(self, f"Could not open {port} — the port may be in use")
            self._status_bar.set_status("Connection failed")

    @Slot()
    def _on_disconnect(self) -> None:
        port = self._session.port_name
        was_logging = self._session.is_logging
        if was_logging:
            self._on_log_off()
        self.ordered_shutdown()
        self._set_connected_ui_visible(False)
        self._connection_panel.set_connected(False)
        self._status_bar.set_status("Disconnected")
        self.title_changed.emit("New Tab")
        self.connection_changed.emit(False)
        if port:
            Toast.info(self, f"Disconnected from {port}")

    @Slot(str)
    def _on_connected(self, port_name: str) -> None:
        baud = self._connection_panel.selected_baud

        self._session.port_name = port_name
        self._session.baud_rate = baud
        self._session.is_connected = True
        self._session.session_start = datetime.now()
        self._session.reset_metrics()

        # Start reader thread (producer-consumer: manager relays bytes → reader processes)
        self._serial_reader = SerialReader(self._session.display_mode)
        self._serial_manager.data_received.connect(self._serial_reader.enqueue_data)
        self._serial_reader.line_received.connect(self._on_line_received)
        self._serial_reader.rate_updated.connect(self._on_rate_updated)
        self._serial_reader.error_occurred.connect(self._on_serial_error)
        self._serial_reader.start()

        # Prepare log engine
        self._log_engine = LogEngine()
        self._log_engine.error_occurred.connect(self._on_log_error)
        self._log_engine.start()

        # UI
        self._connection_panel.set_connected(True)
        self._set_connected_ui_visible(True)
        self._status_bar.set_connection_info(port_name, baud)

        # Tab title
        ports = SerialManager.available_ports()
        desc = next((p.description for p in ports if p.device == port_name), "")
        title = f"{port_name} — {desc}" if desc else port_name
        self.title_changed.emit(title)
        self.connection_changed.emit(True)
        Toast.success(self, f"Connected to {port_name} @ {baud} baud")

    @Slot(str)
    def _on_disconnected(self, port_name: str) -> None:
        logger.info("Disconnected: %s", port_name)
        self._status_bar.set_status("Disconnected")

    @Slot(str)
    def _on_serial_error(self, error_msg: str) -> None:
        """Handle serial errors with professional user-facing feedback."""
        logger.error("Serial error: %s", error_msg)

        fatal_keywords = ("access", "denied", "removed", "disappeared",
                          "device not connected", "permission", "resource",
                          "i/o", "broken pipe", "no such")
        is_fatal = any(kw in error_msg.lower() for kw in fatal_keywords)

        if is_fatal and self._session.is_connected:
            port = self._session.port_name
            was_logging = self._session.is_logging
            self._on_disconnect()

            # Prominent dialog — user MUST see this
            msg = (
                f"The connection to {port} was lost unexpectedly.\n\n"
                "This typically happens when:\n"
                "  • The device was unplugged\n"
                "  • Another application took control of the port\n"
                "  • The USB connection was interrupted\n"
            )
            if was_logging:
                msg += "\nYour log data has been saved and flushed to disk."
            msg += "\n\nPlease reconnect when the device is available."

            QMessageBox.warning(self, "Device Disconnected", msg)
        elif self._session.is_connected:
            Toast.warning(self, "Communication error — connection still active")

    # ══════════════════════════════════════════════════════════════════════
    # DATA
    # ══════════════════════════════════════════════════════════════════════

    @Slot(str, str)
    def _on_line_received(self, line: str, tag: str) -> None:
        now = datetime.now()
        ts = now.strftime(LOG_TIMESTAMP_FORMAT) + f".{now.microsecond // 1000:03d}"

        # Console (may be filtered)
        self._console.append_line(line, tag)

        # LogEngine ALWAYS receives everything
        if self._log_engine and self._log_engine.is_logging:
            self._log_engine.enqueue(ts, line, tag)

        # Metrics
        self._session.total_lines += 1
        self._session.total_bytes += len(line.encode("utf-8"))

    @Slot(int)
    def _on_rate_updated(self, lines_per_sec: int) -> None:
        self._session.data_rate = lines_per_sec
        self._status_bar.set_data_rate(lines_per_sec)

    @Slot(int)
    def _on_line_count_changed(self, count: int) -> None:
        self._status_bar.set_total_lines(count)
        self._filter_bar.update_counts(self._console.visible_count, self._console.total_count)

    # ══════════════════════════════════════════════════════════════════════
    # LOGGING
    # ══════════════════════════════════════════════════════════════════════

    @Slot()
    def _on_log_on(self) -> None:
        from ui.dialogs.new_log_dialog import NewLogDialog

        dialog = NewLogDialog(
            port_name=self._session.port_name,
            default_directory=self._get_default_log_dir(),
            parent=self,
        )
        if dialog.exec() != NewLogDialog.DialogCode.Accepted:
            return

        log_name = dialog.log_name
        log_dir = dialog.log_directory
        export_format = dialog.export_format
        description = dialog.description

        timestamp = datetime.now().strftime(FILENAME_TIMESTAMP_FORMAT)
        safe_name = self._sanitize_filename(log_name)
        port_safe = self._session.port_name.replace("/", "_")
        base_name = f"{safe_name}_{port_safe}_{timestamp}"

        txt_path = os.path.join(log_dir, f"{base_name}.txt")
        csv_path = None
        if export_format in (ExportFormat.CSV, ExportFormat.BOTH):
            csv_path = os.path.join(log_dir, f"{base_name}.csv")

        log_config = LogConfig(
            session_name=log_name, port_name=self._session.port_name,
            baud_rate=self._session.baud_rate, description=description,
        )
        self._log_engine._config = log_config

        csv_engine = None
        if csv_path:
            self._csv_engine = CSVEngine()
            csv_engine = self._csv_engine

        if not self._log_engine.start_logging(txt_path, csv_path, csv_engine):
            Toast.error(self, "Failed to start logging — check directory permissions")
            return

        self._session.is_logging = True
        self._session.is_paused = False
        self._session.log_file_path = txt_path
        self._session.csv_file_path = csv_path
        self._session.export_format = export_format
        self._session.log_name = log_name
        self._session.log_comments = description
        self._log_control_bar.set_logging_state(True, False)
        Toast.success(self, f"Logging started — {safe_name}")

    @Slot()
    def _on_log_off(self) -> None:
        was_logging = self._session.is_logging
        log_name = self._session.log_name
        if self._log_engine and self._log_engine.is_logging:
            self._log_engine.stop_logging()
        self._csv_engine = None
        self._session.reset_logging_state()
        self._log_control_bar.set_logging_state(False)
        if was_logging:
            Toast.info(self, f"Logging stopped — {log_name or 'session'} saved")

    @Slot()
    def _on_pause_toggle(self) -> None:
        if not self._log_engine or not self._log_engine.is_logging:
            return
        if self._session.is_paused:
            self._log_engine.resume()
            self._session.is_paused = False
            Toast.info(self, "Logging resumed")
        else:
            self._log_engine.pause()
            self._session.is_paused = True
            Toast.warning(self, "Logging paused — data is still being received")
        self._log_control_bar.set_logging_state(True, self._session.is_paused)

    @Slot()
    def _on_clear(self) -> None:
        self._console.clear_console()

    @Slot()
    def _on_export(self) -> None:
        """Export current console content to file via dialog."""
        lines = self._console.get_all_lines()
        if not lines:
            Toast.warning(self, "Nothing to export — console is empty")
            return

        from ui.dialogs.export_dialog import ExportDialog

        dialog = ExportDialog(
            default_directory=self._get_default_log_dir(),
            port_name=self._session.port_name,
            parent=self,
        )
        if dialog.exec() != ExportDialog.DialogCode.Accepted:
            return

        export_name = dialog.export_name
        export_dir = dialog.export_directory
        export_format = dialog.export_format

        self._write_export(lines, export_name, export_dir, export_format)

    def quick_save(self) -> None:
        """Quick save console content as .txt (Ctrl+S) — no dialog."""
        lines = self._console.get_all_lines()
        if not lines:
            Toast.warning(self, "Nothing to save — console is empty")
            return

        self._write_export(
            lines, "QuickSave", self._get_default_log_dir(), ExportFormat.TXT,
        )

    def _write_export(
        self,
        lines: list[tuple[datetime, str, str]],
        name: str,
        directory: str,
        fmt: ExportFormat,
    ) -> None:
        """Write console lines to file(s).

        This is the shared implementation for Export (dialog) and Quick Save.

        Args:
            lines: Console content as (datetime, line_text, tag) tuples.
            name: User-provided export name (sanitized).
            directory: Target directory.
            fmt: Export format (TXT / CSV / BOTH).
        """
        import platform

        from version import APP_NAME, APP_VERSION

        # Build filename
        safe_name = self._sanitize_filename(name)
        port_safe = (self._session.port_name or "NoPort").replace("/", "_")
        timestamp = datetime.now().strftime(FILENAME_TIMESTAMP_FORMAT)
        base_name = f"{safe_name}_{port_safe}_{timestamp}"

        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as e:
            Toast.error(self, f"Cannot create directory — {e}")
            return

        files_written = []

        # ── Write .txt ────────────────────────────────────────────────
        if fmt in (ExportFormat.TXT, ExportFormat.BOTH):
            txt_path = os.path.join(directory, f"{base_name}.txt")
            try:
                with open(txt_path, "w", encoding="utf-8") as f:
                    # Header
                    sep = "=" * 80
                    now_str = datetime.now().strftime(LOG_TIMESTAMP_FORMAT)
                    platform_info = f"{platform.system()} {platform.release()}"
                    header_lines = [
                        sep,
                        f"{APP_NAME} v{APP_VERSION} — Exported Data",
                        sep,
                        f"Export Name   : {name}",
                        f"Port          : {self._session.port_name or 'N/A'}",
                        f"Baud Rate     : {self._session.baud_rate or 'N/A'}",
                        f"Exported      : {now_str}",
                        f"Platform      : {platform_info}",
                        f"Lines         : {len(lines):,}",
                        sep,
                        "",
                    ]
                    f.write("\n".join(header_lines) + "\n")

                    # Data lines
                    for dt, line, tag in lines:
                        ts = dt.strftime(LOG_TIMESTAMP_FORMAT)
                        ms = f".{dt.microsecond // 1000:03d}"
                        f.write(f"[{ts}{ms}] {line}\n")

                files_written.append(txt_path)
            except OSError as e:
                Toast.error(self, f"Export failed — {e}")
                return

        # ── Write .csv ────────────────────────────────────────────────
        if fmt in (ExportFormat.CSV, ExportFormat.BOTH):
            csv_path = os.path.join(directory, f"{base_name}.csv")
            try:
                with open(csv_path, "w", encoding="utf-8", newline="") as f:
                    # CSV header comment
                    now_str = datetime.now().strftime(LOG_TIMESTAMP_FORMAT)
                    f.write(f"# {APP_NAME} v{APP_VERSION} — Exported Data\n")
                    f.write(
                        f"# Export: {name}"
                        f" | Port: {self._session.port_name or 'N/A'}"
                        f" | Baud: {self._session.baud_rate or 'N/A'}\n"
                    )
                    f.write(f"# Exported: {now_str}\n")

                    # Try auto-detect mode on first 50 lines
                    csv_engine = CSVEngine()
                    sample = [line for _, line, _ in lines[:50]]
                    csv_engine.detect_mode(sample)
                    csv_engine.write_header(f)

                    # Data rows
                    for dt, line, tag in lines:
                        ts = dt.strftime(LOG_TIMESTAMP_FORMAT)
                        ms = f".{dt.microsecond // 1000:03d}"
                        csv_engine.write_row(f, f"{ts}{ms}", line)

                files_written.append(csv_path)
            except OSError as e:
                Toast.error(self, f"CSV export failed — {e}")
                return

        # Success
        count = len(lines)
        file_names = " + ".join(os.path.basename(p) for p in files_written)
        Toast.success(self, f"Exported {count:,} lines — {file_names}")

    @Slot(str)
    def _on_log_error(self, msg: str) -> None:
        logger.error("Log error: %s", msg)
        Toast.error(self, f"Log write error — {msg}")

    # ══════════════════════════════════════════════════════════════════════
    # COMMAND / FILTER / SEARCH
    # ══════════════════════════════════════════════════════════════════════

    @Slot(str)
    def _on_command_sent(self, command: str) -> None:
        if not self._serial_manager.is_open():
            Toast.warning(self, "Cannot send — device is not connected")
            return
        data = (command + "\n").encode("utf-8")
        if self._serial_manager.write(data):
            self._console.append_line(f">>> {command}", "COMMAND")
            if self._log_engine and self._log_engine.is_logging:
                now = datetime.now()
                ts = now.strftime(LOG_TIMESTAMP_FORMAT) + f".{now.microsecond // 1000:03d}"
                self._log_engine.enqueue(ts, f">>> {command}", "COMMAND")
        else:
            Toast.error(self, "Failed to send command — connection may be lost")

    @Slot(str)
    def _on_filter_changed(self, text: str) -> None:
        self._session.filter_text = text
        self._console.set_filter(text)
        self._filter_bar.update_counts(self._console.visible_count, self._console.total_count)

    @Slot(object)
    def _on_timestamp_toggled(self, mode: TimestampMode) -> None:
        """Handle timestamp mode change from ANY source (View menu or status bar).

        Syncs all three: session state, console display, status bar indicator.
        Emits timestamp_mode_changed so MainWindow can sync the View menu.
        """
        self._session.timestamp_mode = mode
        self._console.set_timestamp_mode(mode)
        self._status_bar.set_timestamp_mode(mode)
        self.timestamp_mode_changed.emit(mode)

    # Keyboard shortcuts (called from MainWindow)
    def activate_search(self) -> None:
        self._search_bar.activate()

    def activate_filter(self) -> None:
        self._filter_bar.focus_input()

    def search_next(self) -> None:
        self._search_bar._on_next()

    def search_prev(self) -> None:
        self._search_bar._on_prev()

    def clear_search_filter(self) -> None:
        self._search_bar.deactivate()
        self._filter_bar.clear_filter()

    # ══════════════════════════════════════════════════════════════════════
    # UTILITY
    # ══════════════════════════════════════════════════════════════════════

    def _get_default_log_dir(self) -> str:
        if self._config_manager:
            return self._config_manager.default_log_directory
        from app.constants import get_default_log_dir
        return get_default_log_dir()

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        if not name or not name.strip():
            return FILENAME_DEFAULT_NAME
        sanitized = name.strip().replace(" ", "_")
        sanitized = re.sub(r'[<>:"/\\|?*]', '', sanitized)
        if len(sanitized) > FILENAME_MAX_LENGTH:
            sanitized = sanitized[:FILENAME_MAX_LENGTH]
        return sanitized or FILENAME_DEFAULT_NAME
