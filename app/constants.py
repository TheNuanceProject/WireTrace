# SPDX-License-Identifier: MIT
"""WireTrace application-wide constants.

This module contains all immutable constants used across the application.
Nothing in this module is mutable. No I/O, no state, no side effects.

Constants are grouped by domain:
  - Serial communication
  - Log engine / buffering
  - UI / display
  - File naming
  - Icon map
  - Performance budgets
  - Platform paths
"""

import os
import sys
from enum import Enum, auto

# ── Serial Communication ─────────────────────────────────────────────────────

# Dropdown list — exact order per spec section 8.2
BAUD_RATES = (2400, 4800, 9600, 28800, 38400, 57600, 76800, 115200)

DEFAULT_BAUD_RATE = 115200

# Validation range for custom baud rate entry
BAUD_RATE_MIN = 50
BAUD_RATE_MAX = 4_000_000

# ── Log Engine / Buffering ───────────────────────────────────────────────────

# collections.deque maxlen — automatic O(1) overflow protection
LOG_BUFFER_MAX_ENTRIES = 50_000

# Flush when buffer reaches this count
LOG_BUFFER_FLUSH_THRESHOLD = 5_000

# Flush interval in milliseconds
LOG_FLUSH_INTERVAL_MS = 1_000

# OS-level file write buffer size (bytes)
FILE_WRITE_BUFFER_SIZE = 65_536  # 64 KB

# Number of lines to sample for CSV auto-detect
CSV_AUTODETECT_SAMPLE_SIZE = 50

# ── UI / Display ─────────────────────────────────────────────────────────────

# GUI update interval (batched signal processing)
GUI_UPDATE_INTERVAL_MS = 50  # 20 FPS

# Maximum lines held in console viewport
MAX_CONSOLE_LINES = 100_000

# Font defaults
DEFAULT_FONT_FAMILY = "Consolas"
DEFAULT_FONT_SIZE = 12
FONT_SIZE_MIN = 8
FONT_SIZE_MAX = 32
FONT_SIZE_STEP = 1

# Splash screen dimensions
SPLASH_WIDTH = 480
SPLASH_HEIGHT = 320

# Default window dimensions
DEFAULT_WINDOW_WIDTH = 1200
DEFAULT_WINDOW_HEIGHT = 800
DEFAULT_WINDOW_X = 100
DEFAULT_WINDOW_Y = 100

# Minimum supported display resolution
MIN_DISPLAY_WIDTH = 1024
MIN_DISPLAY_HEIGHT = 768

# ── File Naming ──────────────────────────────────────────────────────────────

# Characters stripped from user-entered log names
# Per spec section 4.3: < > : " / \ | ? *
FILENAME_ILLEGAL_CHARS = '<>:"/\\|?*'

# Maximum filename length (before extension)
FILENAME_MAX_LENGTH = 128

# Default log name when user leaves it empty
FILENAME_DEFAULT_NAME = "Log"

# File timestamp format for filenames: YYYY-MM-DD_HH-MM-SS
FILENAME_TIMESTAMP_FORMAT = "%Y-%m-%d_%H-%M-%S"

# Log line timestamp format: YYYY-MM-DD HH:MM:SS.mmm
LOG_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# ── Enumerations ─────────────────────────────────────────────────────────────


class ExportFormat(Enum):
    """Log export format options."""
    TXT = auto()
    CSV = auto()
    BOTH = auto()


class DisplayMode(Enum):
    """Console display mode."""
    TEXT = auto()
    HEX = auto()


class TimestampMode(Enum):
    """Timestamp display mode in console."""
    ABSOLUTE = auto()   # [YYYY-MM-DD HH:MM:SS.mmm]
    RELATIVE = auto()   # [+Δ X.XXXs] (delta from previous line)


class CSVMode(Enum):
    """CSV export structure mode."""
    AUTO = auto()   # Auto-detect key:value, key=value, or JSON
    RAW = auto()    # Timestamp,Data (two columns)


class ThemeID(Enum):
    """Available theme identifiers."""
    STUDIO_LIGHT = "studio_light"
    MIDNIGHT_DARK = "midnight_dark"


# ── Icon Map ─────────────────────────────────────────────────────────────────

# Icons are referenced by semantic name, never by filename.
# To rebrand: replace SVG files in resources/icons/, rebuild.
ICON_MAP = {
    "connect": ":/icons/connect.svg",
    "disconnect": ":/icons/disconnect.svg",
    "log_on": ":/icons/log_on.svg",
    "log_off": ":/icons/log_off.svg",
    "pause": ":/icons/pause.svg",
    "resume": ":/icons/resume.svg",
    "clear": ":/icons/clear.svg",
    "search": ":/icons/search.svg",
    "filter": ":/icons/filter.svg",
    "export": ":/icons/export.svg",
    "settings": ":/icons/settings.svg",
    "add_tab": ":/icons/add_tab.svg",
    "close_tab": ":/icons/close_tab.svg",
    "refresh": ":/icons/refresh.svg",
}

# ── Performance / Low-End Device Thresholds ──────────────────────────────────

# Auto-scaling thresholds (detected at startup via QSysInfo)
LOW_CPU_CORE_THRESHOLD = 2          # CPU cores below this → reduce GUI FPS
LOW_RAM_THRESHOLD_MB = 2048         # RAM below this → reduce console buffer
GUI_UPDATE_INTERVAL_LOW_END_MS = 100  # Reduced FPS for low-end CPUs
MAX_CONSOLE_LINES_LOW_END = 50_000    # Reduced buffer for low-RAM systems
LOG_FLUSH_INTERVAL_HDD_MS = 2_000     # Slower flush for HDD (not SSD)

# ── Performance Budgets (for validation / testing) ───────────────────────────

BUDGET_COLD_STARTUP_MS = 2_000       # < 2 seconds
BUDGET_CONNECT_MS = 200              # < 200ms
BUDGET_LINE_PROCESSING_MS = 5        # < 5ms per line
BUDGET_DISK_FLUSH_MS = 50            # < 50ms
BUDGET_SHUTDOWN_MS = 1_000           # < 1 second
BUDGET_THEME_SWITCH_MS = 100         # < 100ms
BUDGET_MEMORY_PER_TAB_IDLE_MB = 15   # < 15 MB idle
BUDGET_MEMORY_PER_TAB_100K_MB = 50   # < 50 MB at 100K lines

# ── Platform-Specific Paths ──────────────────────────────────────────────────


def get_config_dir() -> str:
    """Return platform-appropriate configuration directory path.

    Windows: %APPDATA%/WireTrace/Config/
    macOS:   ~/Library/Application Support/WireTrace/
    Linux:   ~/.config/WireTrace/
    """
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA", ""), "WireTrace", "Config")
    elif sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library",
                            "Application Support", "WireTrace")
    else:
        return os.path.join(os.path.expanduser("~"), ".config", "WireTrace")


def get_default_log_dir() -> str:
    """Return platform-appropriate default log directory path.

    Windows: ~/Documents/WireTrace/Logs/
    macOS:   ~/Documents/WireTrace/Logs/
    Linux:   ~/Documents/WireTrace/Logs/
    """
    return os.path.join(os.path.expanduser("~"), "Documents", "WireTrace", "Logs")


# ── Preferences File ─────────────────────────────────────────────────────────

PREFERENCES_FILENAME = "preferences.ini"

# ── Tag Severity Keywords ────────────────────────────────────────────────────
# Defined here as constants; used by core/tag_detector.py.
# 7 tags total: CRITICAL, ERROR, WARNING, INFO, DEBUG, COMMAND, DATA.

SEVERITY_KEYWORDS = {
    "CRITICAL": ("fatal", "critical", "panic", "crash"),
    "ERROR":    ("error", "fail", "exception", "fault"),
    "WARNING":  ("warn", "warning", "caution"),
    "INFO":     ("info", "connected", "ready", "started",
                 "success", "ok", "initialized", "complete"),
    "DEBUG":    ("debug", "trace", "verbose"),
}

# Tags that are not keyword-based
TAG_COMMAND = "COMMAND"
TAG_DATA = "DATA"

# All valid tags (for validation)
ALL_TAGS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "COMMAND", "DATA")

# ── Update Manager ───────────────────────────────────────────────────────────

# Delay before first update check after startup (seconds)
UPDATE_CHECK_STARTUP_DELAY_S = 10

# Default interval between automatic update checks (hours)
UPDATE_CHECK_INTERVAL_HOURS = 24

# Snooze duration for "Remind Later" (hours)
UPDATE_SNOOZE_HOURS = 24
