# SPDX-License-Identifier: GPL-3.0-or-later
"""WireTrace application-wide constants.

This module contains all immutable constants used across the application.
Nothing in this module is mutable. No I/O, no state, no side effects.

Constants are grouped by domain:
  - Serial communication and framing
  - Log writer, log splitting, session spool
  - Structure detection (CSV export and live plot)
  - UI / display
  - File naming
  - Enumerations and themes
  - Platform paths
  - Preferences file
  - Severity tags
  - Updates
"""

import os
import sys
from enum import Enum, auto

from ui.themes.design_tokens import FONT_FAMILIES as _FONT_FAMILIES
from ui.themes.design_tokens import TYPE_STYLES as _TYPE_STYLES

# ── Serial Communication ─────────────────────────────────────────────────────

# Baud rates offered in the drop-down lists, in this order.
BAUD_RATES = (2400, 4800, 9600, 28800, 38400, 57600, 76800, 115200)

DEFAULT_BAUD_RATE = 115200

# Validation range for custom baud rate entry
BAUD_RATE_MIN = 50
BAUD_RATE_MAX = 4_000_000

# ── Serial Framing ───────────────────────────────────────────────────────────

# Interval of the timer that polls the port for available bytes. The
# poll runs alongside Qt's readyRead signal because that signal is
# unreliable on Windows with several common USB-to-serial chipsets.
SERIAL_POLL_INTERVAL_MS = 20

# Lines that are read together are stamped as having arrived evenly
# since the previous read, provided that read was no longer ago than
# this. After a longer silence they all carry the time of the read.
SERIAL_ARRIVAL_SPREAD_MAX_MS = 100

# A line with no terminator is emitted in pieces of this size, so a
# binary or misconfigured stream cannot grow the assembly buffer forever.
SERIAL_MAX_LINE_BYTES = 65_536

# HEX display mode: bytes shown per row, and how long an incomplete row
# waits for more bytes before it is shown as it is.
HEX_BYTES_PER_ROW = 16
# Bytes in each group of a row, with a wider gap between groups, as in
# ``hexdump -C``: two groups of eight make a row easy to count along.
HEX_BYTES_PER_GROUP = 8
HEX_IDLE_FLUSH_MS = 100

# Interval of the lines-per-second report.
STATS_INTERVAL_MS = 1_000

# ── Log Writer ───────────────────────────────────────────────────────────

# Default limit of lines waiting between the reader and the writer
# thread (the "Lines that may wait for a slow disk" preference), and its
# allowed range.
LOG_BUFFER_MAX_ENTRIES = 50_000
LOG_BUFFER_MAX_ENTRIES_MIN = 1_000
LOG_BUFFER_MAX_ENTRIES_MAX = 100_000

# Default interval between writes to the operating system (the "Write
# to disk every" preference), and its allowed range.
LOG_FLUSH_INTERVAL_MS = 1_000
LOG_FLUSH_INTERVAL_MS_MIN = 200
LOG_FLUSH_INTERVAL_MS_MAX = 5_000

# Interval between forced syncs of open log files to the storage device.
# Bounds what a power loss can cost to this window.
LOG_FSYNC_INTERVAL_MS = 30_000

# Bytes a single log file may hold in memory while its disk is failing.
# Beyond this, new lines for that file are counted as lost and reported.
LOG_PENDING_MAX_BYTES = 64 * 1024 * 1024

# The largest a log file may become (the "Maximum log file size"
# preference; 0 keeps each log in one file). A log that reaches it
# continues in a new file, so a long capture stays easy to open. While
# the limit is on, a CSV file also ends at a row count, so each one
# opens in a spreadsheet.
LOG_SPLIT_DEFAULT_MB = 100
LOG_SPLIT_MAX_MB = 4_096
CSV_MAX_ROWS_PER_PART = 1_000_000

# Inserted before the extension of the second and later files of one
# log. The first file keeps the plain name.
LOG_PART_SUFFIX_FORMAT = "_{number:03d}"

# Suffix inserted before the extension when a file name is already taken,
# and how many numbered alternatives are tried before giving up.
FILENAME_CLASH_SUFFIX_FORMAT = "_{number}"
FILENAME_CLASH_MAX_ATTEMPTS = 1_000

# Bytes a log file collects in memory before they are handed to the
# operating system ahead of the flush interval.
LOG_FLUSH_THRESHOLD_BYTES = 256 * 1024

# ── Session Spool ────────────────────────────────────────────────────────

# Every connected session is recorded to temporary segment files so
# Export can save the whole session while the console stays bounded.
# The oldest segments are discarded once the total exceeds the cap.
SPOOL_SEGMENT_BYTES = 32 * 1024 * 1024
SPOOL_MAX_BYTES = 1024 * 1024 * 1024
SPOOL_DIRECTORY_NAME = "sessions"

# Cleanup at startup leaves spool directories younger than this alone.
# A directory that young may belong to another WireTrace that is
# starting at the same moment and has not locked it yet.
SPOOL_STALE_MIN_AGE_S = 60

# ── Structure Detection (CSV export and live plot) ───────────────────────────

# Number of lines examined per detection attempt.
CSV_AUTODETECT_SAMPLE_SIZE = 50

# Lines received without a detected structure before CSV detection
# settles on raw (two-column) output.
CSV_DETECTION_GIVE_UP_LINES = 200

# Share of sample lines that must match a format for it to be considered.
STRUCTURE_MIN_MATCHING_FRACTION = 0.5

# Share of matching lines a key must appear in to become a column.
STRUCTURE_KEY_FREQUENCY_THRESHOLD = 0.7

# Longest key recognised in a ``key: value`` pair. The limit bounds the
# work done per line; real field names are far shorter.
STRUCTURE_KEY_MAX_LENGTH = 64

# The most characters at the start of a line in which ``key: value``
# pairs are looked for. A device that sends very long lines cannot slow
# the log writer down; what a line holds beyond this stays in the raw
# log and the console.
STRUCTURE_MAX_LINE_CHARACTERS = 4096

# Share of the detected columns a line must supply to count as a CSV row.
CSV_ROW_MIN_COLUMN_FRACTION = 0.5

# Per-line time budget for a user-supplied plot regex (manual mode).
# A pattern that exceeds this on a single line is treated as a non-match
# and the line is skipped, so a catastrophic-backtracking pattern cannot
# freeze the parsing thread or the UI (see core/plot_parsers RegexParser).
PLOT_REGEX_TIMEOUT_SECONDS = 0.1

# A pattern that runs out of time on this many lines in a row is not
# going to recover on the lines that follow. Plotting stops and says so,
# so that the window is not held up line after line.
PLOT_REGEX_MAX_CONSECUTIVE_TIMEOUTS = 5

# The most characters of a line the plot looks at. Readings are found at
# the start of a line; what a very long line holds beyond this is left
# to the console and the log.
PLOT_MAX_LINE_CHARACTERS = 4096

# Lines the plot collects before it looks for numbers in them, and the
# number of lines after which it stops looking and says so.
PLOT_AUTODETECT_SAMPLE_SIZE = 50
PLOT_DETECTION_GIVE_UP_LINES = 200
# Samples kept for each plotted value. At 8 values, each a time and a
# number of 8 bytes, that is about 1.3 MB per tab.
PLOT_RING_BUFFER_SIZE = 10_000
# Recent lines kept. They are what the Configure plot dialog shows and
# what a plot that is opened late starts from.
PLOT_RECENT_LINES_BUFFER_SIZE = 200
# Panels by scale: a value shares a panel with smaller ones while it is
# at most this many times the smallest of them. No more than this many
# panels are shown, so each stays tall enough to read.
PLOT_PANEL_SCALE_RATIO = 10.0
PLOT_MAX_PANELS = 4
# The most values the plot follows. Lines that name more give the first
# ones; a device cannot make the plot keep hundreds of buffers.
PLOT_MAX_SERIES = 32

# How often the plot is redrawn, and how often the figures beside it
# (last, min, max, avg) are worked out again.
PLOT_REDRAW_INTERVAL_MS = 33
PLOT_STATS_INTERVAL_MS = 250

# How often the main thread checks whether garbage is due for collection.
# See app/main_thread_gc.py.
GC_CHECK_INTERVAL_MS = 1000

# How much recent time the plot can show, in seconds, and the choice a
# new plot starts with. None shows everything held.
PLOT_WINDOW_CHOICES: tuple[tuple[str, float | None], ...] = (
    ("10 s", 10.0),
    ("30 s", 30.0),
    ("60 s", 60.0),
    ("5 min", 300.0),
    ("All", None),
)
PLOT_DEFAULT_WINDOW_SECONDS = 60.0

# ── UI / Display ─────────────────────────────────────────────────────────

# The dot that separates parts of a text, as on the website: between the
# parts of one name ("COM7 \u00b7 USB-SERIAL CH340"), and with more room
# between the fields of a status line, as the website's meta rows.
NAME_SEPARATOR = " \u00b7 "
FIELD_SEPARATOR = "  \u00b7  "

# Interval at which the console collects new lines from the reader (the
# "Screen refresh" preference), and its allowed range.
GUI_UPDATE_INTERVAL_MS = 50
GUI_UPDATE_INTERVAL_MS_MIN = 16
GUI_UPDATE_INTERVAL_MS_MAX = 500

# Lines appended to the console per step within one refresh, and the
# share of the refresh interval those steps may use. A step is small so
# the time check after it is frequent; the share leaves the rest of the
# interval for input and painting, so the window stays responsive on a
# slow machine.
DISPLAY_CHUNK_LINES = 100
DISPLAY_TICK_BUDGET_FRACTION = 0.5

# When lines arrive faster than this machine can draw them, the console
# skips ahead so that it is never further behind the device than this.
# The skipped lines are still recorded and logged.
DISPLAY_MAX_LAG_MS = 1000

# Skipped lines are reported in the console as one line per this
# interval, rather than one per refresh.
DISPLAY_SKIP_NOTICE_INTERVAL_MS = 2000

# Maximum lines held in the console (the "Console line limit"
# preference), and its allowed range.
MAX_CONSOLE_LINES = 100_000
MAX_CONSOLE_LINES_MIN = 10_000
MAX_CONSOLE_LINES_MAX = 500_000

# The most characters the console holds, whatever the line limit. Lines
# are counted, but a device that sends very long lines, or bytes that
# are shown as escapes, would otherwise fill the memory long before the
# line limit is reached. The oldest lines go first, as with the limit.
CONSOLE_MAX_CHARACTERS = 128 * 1024 * 1024

# The same bound for the lines waiting to be drawn, and for the lines
# waiting to be written. Past it, lines are skipped or dropped and
# counted, exactly as when the count of lines is exceeded.
DISPLAY_MAX_CHARACTERS = 32 * 1024 * 1024
LOG_PENDING_MAX_CHARACTERS = 64 * 1024 * 1024

# The most characters of a row the console shapes for drawing when the
# row is not plain ASCII. Shaping costs time in proportion to length;
# the whole line is still held, copied, searched and logged.
CONSOLE_SHAPED_CHARACTERS = 4096

# Delay between the last keystroke in the filter box and the console
# being re-rendered.
FILTER_DEBOUNCE_MS = 200

# Delay between the last keystroke in the search box and the search
# being run. Enter, F3 and Shift+F3 search at once.
SEARCH_DEBOUNCE_MS = 200

# The console draws a tab as spaces up to the next multiple of this
# many columns. The tab itself is kept in the text that is copied.
CONSOLE_TAB_WIDTH = 8

# Search stops collecting matches at this number, so a search for a
# single common letter cannot hold an unbounded list.
CONSOLE_SEARCH_MATCH_LIMIT = 100_000

# Ports whose last-used settings are remembered; the least recently
# used is forgotten beyond this. A tab name is cut at the second limit.
PORT_MEMORY_LIMIT = 64
PORT_TAB_NAME_MAX = 64

# The console menu quotes the selected text up to this many characters.
MENU_SELECTION_PREVIEW_CHARS = 24

# Commands the command box remembers in one session.
COMMAND_HISTORY_LIMIT = 50

# Commands the user can save to send again, how long one may be, and
# how many of them have a keyboard shortcut of their own.
SAVED_COMMANDS_MAX = 20
SAVED_COMMAND_MAX_LENGTH = 512
SAVED_COMMAND_SHORTCUTS = 9

# A tooltip appears after the pointer has rested this long, and the
# next one appears at once if the pointer moves on within the second time.
TOOLTIP_WAKE_UP_MS = 300
TOOLTIP_FALL_ASLEEP_MS = 100

# How long a toast stays. One that carries a button stays longer, so
# there is time to reach it.
TOAST_DURATION_MS = 3500
TOAST_WITH_ACTION_DURATION_MS = 7000

# How often the first screen looks for serial ports that were plugged
# in or removed.
PORT_SCAN_INTERVAL_MS = 1500

# The console's font, and its text size in logical pixels. It starts as
# the design language's mono family at the size of its ``mono`` style;
# the user can make it larger or smaller. Never smaller than the smallest
# text of the language; no larger than leaves enough rows to follow a
# device on a laptop screen.
DEFAULT_FONT_FAMILY = _FONT_FAMILIES["mono"][0]
DEFAULT_FONT_SIZE = _TYPE_STYLES["mono"][1]
FONT_SIZE_MIN = min(style[1] for style in _TYPE_STYLES.values())
FONT_SIZE_MAX = 40
FONT_SIZE_STEP = 1

# Default window dimensions
DEFAULT_WINDOW_WIDTH = 1200
DEFAULT_WINDOW_HEIGHT = 800
DEFAULT_WINDOW_X = 100
DEFAULT_WINDOW_Y = 100

# Bounds for a window size and position read from the preferences file,
# wide enough for any arrangement of screens. The window is then fitted
# to the screens that are actually there.
WINDOW_SIZE_MIN = 1
WINDOW_SIZE_MAX = 16_384
WINDOW_POSITION_MIN = -32_768
WINDOW_POSITION_MAX = 32_767

# ── File Naming ──────────────────────────────────────────────────────────

# Maximum filename length (before extension)
FILENAME_MAX_LENGTH = 128

# Default log name when user leaves it empty
FILENAME_DEFAULT_NAME = "Log"
# Stands in for the port in the name of a file made with no port.
FILENAME_NO_PORT = "NoPort"
# How often a dialog brings the file name it previews up to date.
FILENAME_PREVIEW_INTERVAL_MS = 1000

# File timestamp format for filenames: YYYY-MM-DD_HH-MM-SS
FILENAME_TIMESTAMP_FORMAT = "%Y-%m-%d_%H-%M-%S"

# Date and time of a log line timestamp, to the second.
# core.records.format_timestamp appends the milliseconds.
LOG_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# ── Enumerations ─────────────────────────────────────────────────────────


class ExportFormat(Enum):
    """Log export format options."""
    TXT = auto()
    CSV = auto()
    BOTH = auto()


class DisplayMode(Enum):
    """Console display mode."""
    TEXT = auto()
    HEX = auto()


class LineEnding(Enum):
    """What is sent after a command. The value is the characters themselves."""
    NONE = ""
    LF = "\n"
    CR = "\r"
    CRLF = "\r\n"


#: Line endings a log or export file can be written with, and the one
#: used until the user chooses another.
LOG_LINE_ENDINGS = (LineEnding.LF, LineEnding.CRLF)
DEFAULT_LOG_LINE_ENDING = LineEnding.LF

#: Line ending sent after a command until the user chooses another.
DEFAULT_LINE_ENDING = LineEnding.LF


class TimestampMode(Enum):
    """Timestamp display mode in console."""
    ABSOLUTE = auto()   # HH:MM:SS.mmm, the time of day
    RELATIVE = auto()   # +X.XXX s, the time since the previous line


class CSVMode(Enum):
    """CSV export structure mode."""
    AUTO = auto()   # Auto-detect key:value, key=value, or JSON
    RAW = auto()    # Timestamp,Data (two columns)


class ThemeID(Enum):
    """The themes the application can draw."""
    LIGHT = "light"
    DARK = "dark"


#: Theme preference meaning "use whichever theme the system is set to".
THEME_FOLLOW_SYSTEM = "system"

#: Theme used when the system does not report a light or dark setting.
THEME_FALLBACK = ThemeID.LIGHT

#: Version of the preferences file layout. Raised when a release changes
#: a default that existing installations should also receive; the file
#: is brought up to date once, the first time that release starts.
#: A file with no version was written by WireTrace 1.x and counts as 1.
SETTINGS_VERSION = 2
SETTINGS_VERSION_UNVERSIONED = 1

#: Theme names found in a 1.x preferences file, mapped to the current names.
LEGACY_THEME_VALUES = {
    "studio_light": ThemeID.LIGHT.value,
    "midnight_dark": ThemeID.DARK.value,
}

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


def get_cache_dir() -> str:
    """Return platform-appropriate directory for disposable working files.

    Windows: %LOCALAPPDATA%/WireTrace/Cache/
    macOS:   ~/Library/Caches/WireTrace/
    Linux:   $XDG_CACHE_HOME/WireTrace/ (default ~/.cache/WireTrace/)
    """
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        return os.path.join(base, "WireTrace", "Cache")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Caches", "WireTrace")
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(home, ".cache")
    return os.path.join(base, "WireTrace")


def get_default_log_dir() -> str:
    """Return the default log directory: ~/Documents/WireTrace/Logs on
    every platform."""
    return os.path.join(os.path.expanduser("~"), "Documents", "WireTrace", "Logs")


# ── Preferences File ─────────────────────────────────────────────────────────

PREFERENCES_FILENAME = "preferences.ini"

# Several WireTrace windows can be open at once. A shared file is written
# only while its lock (the file's name plus this suffix) is held, after
# reading what the other windows wrote; a wait for the lock gives up
# after this many seconds. See app/file_lock.py.
SHARED_FILE_LOCK_SUFFIX = ".lock"
SHARED_FILE_LOCK_TIMEOUT_S = 5

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

# Tag carried by lines WireTrace itself adds to the stream (skipped
# lines, paused logging, write failures).
TAG_NOTICE = "WARNING"

# Level markers printed by firmware logging frameworks, mapped to tags.
# These are exact, so they are recognised before the keyword search.
#   ESP-IDF:  "E (1234) wifi: ..."      Zephyr:  "<err> net: ..."
#   Generic:  "[ERROR] ..."
ESP_IDF_LEVEL_TAGS = {
    "E": "ERROR",
    "W": "WARNING",
    "I": "INFO",
    "D": "DEBUG",
    "V": "DEBUG",
}
ZEPHYR_LEVEL_TAGS = {
    "err": "ERROR",
    "wrn": "WARNING",
    "inf": "INFO",
    "dbg": "DEBUG",
}
BRACKETED_LEVEL_TAGS = {
    "FATAL": "CRITICAL",
    "CRITICAL": "CRITICAL",
    "CRIT": "CRITICAL",
    "ERROR": "ERROR",
    "ERR": "ERROR",
    "WARNING": "WARNING",
    "WARN": "WARNING",
    "INFO": "INFO",
    "DEBUG": "DEBUG",
    "DBG": "DEBUG",
    "TRACE": "DEBUG",
    "VERBOSE": "DEBUG",
}

# ── Updates ──────────────────────────────────────────────────────────────

# The first check is made this long after the window appears, and another
# after every interval for as long as the application stays open.
UPDATE_CHECK_STARTUP_DELAY_S = 10
UPDATE_CHECK_INTERVAL_HOURS = 12

# "Remind later" keeps an update from being announced for this long.
UPDATE_SNOOZE_HOURS = 24

# How long the manifest and the download may each wait for the server.
UPDATE_MANIFEST_TIMEOUT_S = 15
UPDATE_DOWNLOAD_TIMEOUT_S = 60

# The signed manifest is a small file. A larger answer is refused before
# it is read into memory.
UPDATE_MANIFEST_MAX_BYTES = 256 * 1024

# No installer is larger than this. A manifest that claims more is refused.
UPDATE_DOWNLOAD_MAX_BYTES = 1024 * 1024 * 1024

# Bytes read from the network at a time while downloading.
UPDATE_DOWNLOAD_CHUNK_BYTES = 64 * 1024

# Installers that earlier versions left in the system's temporary folder
# are removed once they are older than this.
UPDATE_STALE_DOWNLOAD_DAYS = 7

# The header with which a check names the day of the previous check, the
# word it sends when there was none, and the header in which the server
# names its own current day. See updater/update_manager.py.
UPDATE_LAST_CHECK_HEADER = "X-WireTrace-Last-Check"
UPDATE_FIRST_CHECK_VALUE = "new"
UPDATE_SERVER_DAY_HEADER = "X-WireTrace-Day"

# The name of the mutex the Windows installer waits for (AppMutex in
# build/windows/installer.iss), so that it can tell the application is
# still open.
# The server names its own day as a count of days. One that differs from
# this computer's count by more than this is not stored, so that the
# value sent back can only ever be one of a few that every installation
# checking on those days shares.
UPDATE_SERVER_DAY_TOLERANCE = 2

WINDOWS_INSTALLER_MUTEX = "WireTrace_SingleInstance_Mutex"

# ── Usage statistics ─────────────────────────────────────────────────────
# Off unless the user switches them on. See updater/usage.py.

# The file beside the preferences that holds the installation's
# identifier, the notes of the current day, the reports not yet sent and
# the deletion requests not yet delivered.
USAGE_NOTES_FILENAME = "usage.json"

# The folder beside it in which each open WireTrace holds a lock while
# statistics are on: a lock left behind by a run that has ended means it
# did not close normally.
USAGE_RUNS_FOLDER = "usage-runs"

# Reports waiting to be sent are first sent this long after the window
# appears, and again this long after each change of day while the
# application stays open.
USAGE_SEND_DELAY_S = 30
USAGE_DAY_CHANGE_DELAY_S = 300

# Waiting reports are sent one at a time, each after a wait chosen at
# random within these bounds, so that their timing does not join them.
USAGE_SEND_SPACING_MIN_S = 60
USAGE_SEND_SPACING_MAX_S = 300

# A deletion request that could not be delivered is tried again this
# often while WireTrace is open, and at every start, until it is.
USAGE_DELETION_RETRY_S = 3600

# A deletion request is delivered twice: once when statistics are
# switched off, and again at least this long after it was first
# delivered. A report already on its way when they were switched off, in
# this window or another, may reach the server after the first request;
# the second deletes it. It is far longer than such a report can take.
USAGE_DELETION_CONFIRM_AFTER_S = 600

# What the server does with reports, as the privacy statement states it.
# They must match REPORTS_KEPT_MONTHS in wiretrace-updates/src/fold.ts and
# the database's point-in-time history, which Cloudflare keeps for at
# most 30 days.
USAGE_RECORDS_KEPT_MONTHS = 14
USAGE_DELETION_COMPLETE_DAYS = 30

# A report that could not be sent within this many days is dropped.
USAGE_REPORT_MAX_AGE_DAYS = 7

# How long sending a report may wait for the server, and the most of its
# answer that is read.
USAGE_SEND_TIMEOUT_S = 15
USAGE_ANSWER_MAX_BYTES = 4 * 1024

# The question about usage statistics is shown on at most this many
# starts. Answering it, or closing it, ends it at once.
USAGE_QUESTION_MAX_SHOWINGS = 2

# Where the Windows installer records what was chosen about update
# checks: a file beside the executable, read once on first start.
INSTALLER_CHOICES_FILE = "install.ini"
INSTALLER_CHOICES_SECTION = "Updates"
INSTALLER_UPDATE_CHECK_KEY = "check"
