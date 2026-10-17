# SPDX-License-Identifier: GPL-3.0-or-later
"""WireTrace CSV export engine: turns session lines into CSV rows.

Two output modes:

  AUTO  Structured data. Column names are detected from the lines
        themselves, in two supported shapes:
          - ``key: value`` / ``key=value`` pairs
          - JSON objects ``{...}``
        Output: ``Timestamp,Temperature,Humidity,Pressure``

  RAW   Everything else. Output: ``Timestamp,Data``

Detection:
  Lines are held back until the mode is known, so the header and every
  row always share one set of columns. Detection looks at the most
  recent ``CSV_AUTODETECT_SAMPLE_SIZE`` lines each time that many more
  have arrived. A device that prints a boot log before its readings
  is therefore still detected once the readings dominate the sample.
  After ``CSV_DETECTION_GIVE_UP_LINES`` lines without a structure, the
  engine settles on RAW.

Which lines become rows (AUTO):
  A line is a row if it supplies at least ``CSV_ROW_MIN_COLUMN_FRACTION``
  of the detected columns. This is decided by what the line contains,
  never by its severity tag, so a line of readings that happens to
  include a word like "errors" is exported like any other. Lines that
  do not fit the columns are left out of the .csv to keep it
  rectangular; the .txt log holds every line.

The engine only formats text. Its caller owns the file, which is what
lets the same engine feed a live log that is split into parts and a
one-off export.

This module does not touch the GUI, the disk, the serial port or any other I/O.
"""

from __future__ import annotations

import json
import logging
import math
import re

from app.constants import (
    CSV_AUTODETECT_SAMPLE_SIZE,
    CSV_DETECTION_GIVE_UP_LINES,
    CSV_ROW_MIN_COLUMN_FRACTION,
    STRUCTURE_KEY_MAX_LENGTH,
    STRUCTURE_MAX_LINE_CHARACTERS,
    CSVMode,
)
from core.structure import select_common_keys

logger = logging.getLogger(__name__)

#: Name of the first column of every CSV, and of the single data column
#: in RAW mode.
TIMESTAMP_COLUMN = "Timestamp"
RAW_DATA_COLUMN = "Data"

# Leading characters that spreadsheet applications (Excel, LibreOffice
# Calc) interpret as the start of a formula. A field beginning with any
# of these is prefixed with a single quote on export so the value is
# rendered as literal text rather than executed (OWASP CSV Injection
# guidance). Tab and carriage return are included because they can also
# introduce a formula/command context in some spreadsheet parsers.
_CSV_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")

# A plain signed number. Such a value cannot carry a formula, so it is
# exported unchanged and stays a number in the spreadsheet.
_PLAIN_NUMBER_RE = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")

# ``key: value`` or ``key=value``. A key starts with a letter or an
# underscore, which keeps clock times such as ``12:30:45`` from being
# read as a key and a value. It starts at the beginning of a word and
# has a bounded length, so the time spent on a line grows with the
# line's length and not with its square, whatever the line contains.
_KV_PATTERN = re.compile(
    rf"(?<!\w)([A-Za-z_][\w ]{{0,{STRUCTURE_KEY_MAX_LENGTH - 1}}}?)\s*[:=]\s*([^\s,;]+)",
)


class CSVEngine:
    """Detects the structure of a session and formats its CSV rows."""

    def __init__(self) -> None:
        self._mode: CSVMode = CSVMode.RAW
        self._columns: list[str] = []
        self._min_columns_per_row = 0
        self._detection_complete = False
        # (timestamp, line) pairs held back until the mode is known.
        # Each keeps its own timestamp so the rows written after
        # detection carry the time their line actually arrived.
        self._held: list[tuple[str, str]] = []

    # ── Public API ───────────────────────────────────────────────────────

    @property
    def mode(self) -> CSVMode:
        """Current CSV mode (AUTO or RAW)."""
        return self._mode

    @property
    def columns(self) -> list[str]:
        """Detected column names (empty in RAW mode)."""
        return list(self._columns)

    @property
    def detection_complete(self) -> bool:
        """True once the mode is known and rows are being produced."""
        return self._detection_complete

    @property
    def header(self) -> str:
        """The column header line. Meaningful once detection is complete."""
        if self._mode == CSVMode.AUTO:
            names = [self._csv_escape(column) for column in self._columns]
            return ",".join([TIMESTAMP_COLUMN, *names])
        return f"{TIMESTAMP_COLUMN},{RAW_DATA_COLUMN}"

    def detect_mode(self, lines: list[str]) -> CSVMode:
        """Decide the mode from a sample of lines and settle on it."""
        columns = self._detect_columns(lines)
        if columns:
            self._settle(CSVMode.AUTO, columns)
        else:
            self._settle(CSVMode.RAW, [])
        return self._mode

    def feed(self, timestamp: str, line: str) -> list[str]:
        """Add one line and return the rows that are now ready to write.

        Returns an empty list while lines are being held for detection,
        the rows of every held line at the moment detection settles,
        and zero or one row per line after that.
        """
        if self._detection_complete:
            row = self._format_row(timestamp, line)
            return [] if row is None else [row]

        self._held.append((timestamp, line))
        held = len(self._held)
        if held % CSV_AUTODETECT_SAMPLE_SIZE != 0:
            return []

        columns = self._detect_columns(self._recent_sample())
        if columns:
            self._settle(CSVMode.AUTO, columns)
        elif held >= CSV_DETECTION_GIVE_UP_LINES:
            self._settle(CSVMode.RAW, [])
        else:
            return []
        return self._release_held()

    def finalize(self) -> list[str]:
        """Settle the mode with whatever was received and return its rows.

        Called when the session ends before detection settled. Tries the
        most recent sample first, then every held line.
        """
        if self._detection_complete:
            return []
        columns = self._detect_columns(self._recent_sample())
        if not columns:
            columns = self._detect_columns([line for _, line in self._held])
        if columns:
            self._settle(CSVMode.AUTO, columns)
        else:
            self._settle(CSVMode.RAW, [])
        return self._release_held()

    # ── Internal: Detection ──────────────────────────────────────────────

    def _recent_sample(self) -> list[str]:
        return [line for _, line in self._held[-CSV_AUTODETECT_SAMPLE_SIZE:]]

    def _settle(self, mode: CSVMode, columns: list[str]) -> None:
        self._mode = mode
        self._columns = list(columns)
        self._min_columns_per_row = max(
            1, math.ceil(len(columns) * CSV_ROW_MIN_COLUMN_FRACTION),
        )
        self._detection_complete = True
        logger.info("CSV structure settled: %s, columns=%s", mode.name, columns)

    def _release_held(self) -> list[str]:
        rows = [
            row
            for timestamp, line in self._held
            if (row := self._format_row(timestamp, line)) is not None
        ]
        self._held.clear()
        return rows

    @staticmethod
    def _detect_columns(lines: list[str]) -> list[str] | None:
        """Return the column names found in ``lines``, or None."""
        if not lines:
            return None

        json_keys = [
            list(obj)
            for line in lines
            if (obj := CSVEngine._parse_json_object(line)) is not None and obj
        ]
        columns = select_common_keys(json_keys, len(lines))
        if columns:
            return columns

        kv_keys = [
            [key.strip() for key, _ in matches]
            for line in lines
            if (matches := _KV_PATTERN.findall(line, 0, STRUCTURE_MAX_LINE_CHARACTERS))
        ]
        return select_common_keys(kv_keys, len(lines))

    @staticmethod
    def _parse_json_object(line: str) -> dict | None:
        stripped = line.strip()
        if not stripped.startswith("{"):
            return None
        try:
            obj = json.loads(stripped)
        except (ValueError, RecursionError):
            # Not JSON, or nested too deeply to be a data line.
            return None
        return obj if isinstance(obj, dict) else None

    # ── Internal: Row Formatting ─────────────────────────────────────────

    def _format_row(self, timestamp: str, line: str) -> str | None:
        """Format one line as a row, or None if it is not a row."""
        if self._mode == CSVMode.RAW:
            return f"{timestamp},{self._csv_escape(line)}"

        values = self._extract_values(line)
        if len(values) < self._min_columns_per_row:
            return None
        cells = [self._csv_escape(values.get(column, "")) for column in self._columns]
        return ",".join([timestamp, *cells])

    def _extract_values(self, line: str) -> dict[str, str]:
        """Return the detected columns present on ``line`` with their values."""
        obj = self._parse_json_object(line)
        if obj is not None:
            return {
                key: self._json_cell(value)
                for key, value in obj.items()
                if key in self._columns
            }
        return {
            key.strip(): value.strip()
            for key, value in _KV_PATTERN.findall(line, 0, STRUCTURE_MAX_LINE_CHARACTERS)
            if key.strip() in self._columns
        }

    @staticmethod
    def _json_cell(value: object) -> str:
        """Render a JSON value as cell text."""
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _csv_escape(value: str) -> str:
        """Escape a value for CSV output, safe against formula injection.

        Two layers, in order:

          1. Formula-injection neutralisation: a value whose first
             character is one a spreadsheet treats as a formula start
             (``=`` ``+`` ``-`` ``@`` tab CR) is prefixed with a single
             quote, so Excel and LibreOffice render it as literal text
             and never execute it. A plain signed number is exempt: it
             cannot carry a formula, and prefixing it would turn every
             negative reading into text.

          2. RFC 4180 structural escaping: the (possibly prefixed) value
             is wrapped in quotes if it contains a comma, quote, or
             newline, with internal quotes doubled.
        """
        if not value:
            return ""
        if value[0] in _CSV_FORMULA_TRIGGERS and not _PLAIN_NUMBER_RE.fullmatch(value):
            value = "'" + value
        if "," in value or '"' in value or "\n" in value or "\r" in value:
            return '"' + value.replace('"', '""') + '"'
        return value
