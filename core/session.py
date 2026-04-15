# SPDX-License-Identifier: MIT
"""WireTrace per-device session state container.

Each DeviceTab owns exactly one DeviceSession instance.
This is a pure data container — it performs no I/O of any kind.

Responsibilities:
  - Store connection state (port, baud, connected status)
  - Store logging state (is_logging, is_paused, file paths, format)
  - Store display preferences (mode, auto-scroll, color, timestamps, filter)
  - Store runtime metrics (data rate, line count, byte count)
  - Store future plotter hook (format_profile for v2.1)

This module does NOT touch: serial ports, files, GUI, or network.
"""

from dataclasses import dataclass
from datetime import datetime

from app.constants import (
    DEFAULT_BAUD_RATE,
    DisplayMode,
    ExportFormat,
    TimestampMode,
)


@dataclass
class DeviceSession:
    """State container for a single connected device tab.

    All fields have sensible defaults matching spec section 5.5.
    This dataclass is mutable — the owning DeviceTab updates fields
    as user actions and serial events occur.
    """

    # ── Connection ───────────────────────────────────────────────────────

    port_name: str = ""
    baud_rate: int = DEFAULT_BAUD_RATE
    is_connected: bool = False

    # ── Logging ──────────────────────────────────────────────────────────

    is_logging: bool = False
    is_paused: bool = False
    log_file_path: str | None = None
    csv_file_path: str | None = None
    export_format: ExportFormat = ExportFormat.TXT
    log_name: str = ""
    log_comments: str = ""

    # ── Display ──────────────────────────────────────────────────────────

    display_mode: DisplayMode = DisplayMode.TEXT
    auto_scroll: bool = True
    color_mode: bool = True
    timestamp_mode: TimestampMode = TimestampMode.ABSOLUTE
    filter_text: str = ""  # Active filter (empty = show all)

    # ── Metrics ──────────────────────────────────────────────────────────

    data_rate: int = 0              # lines/sec (updated by SerialReader)
    total_lines: int = 0
    total_bytes: int = 0
    session_start: datetime | None = None

    # ── Future (v2.1 — Plotter) ─────────────────────────────────────────

    format_profile: str | None = None  # Saved parse profile name

    # ── Methods ──────────────────────────────────────────────────────────

    def reset_metrics(self) -> None:
        """Reset all runtime metrics to zero. Called on new connection."""
        self.data_rate = 0
        self.total_lines = 0
        self.total_bytes = 0
        self.session_start = None

    def reset_logging_state(self) -> None:
        """Reset logging-related state. Called when logging stops."""
        self.is_logging = False
        self.is_paused = False
        self.log_file_path = None
        self.csv_file_path = None
        self.log_name = ""
        self.log_comments = ""

    def reset_connection_state(self) -> None:
        """Reset all state for a fresh disconnection. Preserves display prefs."""
        self.is_connected = False
        self.port_name = ""
        self.reset_logging_state()
        self.reset_metrics()
