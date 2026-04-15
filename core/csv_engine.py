# SPDX-License-Identifier: MIT
"""WireTrace CSV export engine — two-mode structured export.

Produces formatted CSV from serial data in two modes:

Mode 1 — Auto-detect (for structured data):
  Detects from first N lines (CSV_AUTODETECT_SAMPLE_SIZE = 50).
  Supports two common patterns:
    - key:value / key=value → keys become column headers
    - JSON {...} → JSON keys become column headers
  Output: Timestamp,Temperature,Humidity,Pressure

Mode 2 — Raw (for everything else):
  Output: Timestamp,Data

Architecture note for v2.1 plotter:
  The structure detection output (column names + parsed values) is exposed
  via the structure_detected signal. The future plotter subscribes to this
  signal without duplicating parsing logic.

This module does NOT touch: GUI or serial I/O.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import IO

from PySide6.QtCore import QObject, Signal

from app.constants import CSV_AUTODETECT_SAMPLE_SIZE, CSVMode

logger = logging.getLogger(__name__)


# ── Parsed Row ───────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class ParsedRow:
    """A single parsed data row with named columns.

    Used by CSVEngine and exposed to future plotter via signal.
    """
    columns: dict[str, str]  # column_name → value


# ── CSV Engine ───────────────────────────────────────────────────────────────

class CSVEngine(QObject):
    """Produces formatted CSV from serial data.

    Auto-detects data structure from the first sample lines, then
    applies consistent parsing to all subsequent lines.

    Signals:
        structure_detected(list):   Emitted when column names are determined.
                                    Carries list[str] of column names.
                                    (v2.1 plotter hook)
        row_parsed(dict):           Emitted for each parsed row.
                                    Carries dict[str, str] of column→value.
                                    (v2.1 plotter hook)
    """

    # ── Signals (v2.1 plotter hooks) ─────────────────────────────────────

    structure_detected = Signal(list)   # list[str] column names
    row_parsed = Signal(dict)           # dict[str, str] column→value

    # ── Regex Patterns ───────────────────────────────────────────────────

    # Matches: key:value or key=value (with optional whitespace)
    # key must be word characters, value is everything after separator
    _KV_PATTERN = re.compile(
        r"(\w[\w\s]*?)\s*[:=]\s*([^\s,;]+)"
    )

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self._mode: CSVMode = CSVMode.RAW
        self._columns: list[str] = []
        # Buffer of (timestamp, line) pairs collected during detection.
        # Pairs preserve each row's original timestamp for re-write after
        # detection completes — using a single "current" timestamp for
        # all buffered rows corrupts the log (see audit C2).
        self._sample_buffer: list[tuple[str, str]] = []
        self._sample_size = CSV_AUTODETECT_SAMPLE_SIZE
        self._detection_complete = False
        self._header_written = False

    # ── Public API ───────────────────────────────────────────────────────

    @property
    def mode(self) -> CSVMode:
        """Current CSV mode (AUTO or RAW)."""
        return self._mode

    @property
    def columns(self) -> list[str]:
        """Detected column names (empty if RAW mode)."""
        return list(self._columns)

    @property
    def detection_complete(self) -> bool:
        """True if auto-detection has finished."""
        return self._detection_complete

    def reset(self) -> None:
        """Reset engine state for a new session."""
        self._mode = CSVMode.RAW
        self._columns.clear()
        self._sample_buffer.clear()
        self._detection_complete = False
        self._header_written = False

    def detect_mode(self, lines: list[str]) -> CSVMode:
        """Analyze a batch of lines to determine the CSV mode.

        This is the explicit detection API. Can be called manually
        with a pre-collected sample, or detection happens automatically
        as lines are fed via write_row().

        Args:
            lines: Sample lines to analyze.

        Returns:
            CSVMode.AUTO if structure was detected, CSVMode.RAW otherwise.
        """
        if not lines:
            return CSVMode.RAW

        # Try JSON detection first (more specific)
        json_columns = self._try_detect_json(lines)
        if json_columns:
            self._mode = CSVMode.AUTO
            self._columns = json_columns
            self._detection_complete = True
            self.structure_detected.emit(self._columns)
            logger.info("CSV auto-detect: JSON mode, columns=%s", self._columns)
            return CSVMode.AUTO

        # Try key:value / key=value detection
        kv_columns = self._try_detect_kv(lines)
        if kv_columns:
            self._mode = CSVMode.AUTO
            self._columns = kv_columns
            self._detection_complete = True
            self.structure_detected.emit(self._columns)
            logger.info("CSV auto-detect: KV mode, columns=%s", self._columns)
            return CSVMode.AUTO

        # No structure detected — fall back to RAW
        self._mode = CSVMode.RAW
        self._detection_complete = True
        logger.info("CSV auto-detect: RAW mode (no structure found)")
        return CSVMode.RAW

    def write_header(self, file_handle: IO[str]) -> None:
        """Prepare to write the CSV column header.

        The header is NOT written immediately. Until auto-detection has
        a chance to inspect real data, we cannot know whether to write
        ``Timestamp,Data`` (RAW) or ``Timestamp,col1,col2,...`` (AUTO).

        The actual header line is written by :meth:`_write_header_once`
        when the first row is flushed — either after detection completes
        or after :meth:`finalize` runs with fewer samples than expected.

        The argument is accepted (and validated) to keep the call-site
        protocol uniform with :meth:`write_row` and :meth:`finalize`.

        Args:
            file_handle: Open file handle for the .csv file.
        """
        # No-op: header write is deferred until the mode is known.
        # file_handle is retained in the signature so callers pass it
        # consistently, but nothing is written here.
        del file_handle  # explicit "unused on purpose"

    def write_row(
        self,
        file_handle: IO[str],
        timestamp: str,
        line: str,
    ) -> None:
        """Write a single data row to the CSV file.

        During the first :attr:`_sample_size` rows, the timestamp/line
        pair is buffered — nothing is written to disk. Once the buffer
        is full, auto-detection runs, the column header is written,
        then all buffered rows are flushed in the detected mode.

        After detection completes, rows are written directly.

        Args:
            file_handle: Open file handle for the .csv file.
            timestamp: Formatted timestamp string for this row.
            line: The decoded serial line.
        """
        # Auto-detection phase: buffer (timestamp, line) pairs. Nothing
        # is written to disk yet — we must know the column structure
        # before committing the header, and writing raw rows first
        # would corrupt the CSV's structural consistency.
        if not self._detection_complete:
            self._sample_buffer.append((timestamp, line))
            if len(self._sample_buffer) >= self._sample_size:
                self._flush_sample_buffer(file_handle)
            return

        # Detection has already completed — write directly.
        self._write_header_once(file_handle)
        self._write_single_row(file_handle, timestamp, line)

    def finalize(self, file_handle: IO[str]) -> None:
        """Finalize CSV output.

        Called when logging stops. If auto-detection never completed
        (fewer rows than :attr:`_sample_size`), run detection on what
        we have, write the header, and flush every buffered row with
        its original timestamp.

        Args:
            file_handle: Open file handle for the .csv file.
        """
        if not self._detection_complete:
            self._flush_sample_buffer(file_handle)

    # ── Internal: Buffer Flush ───────────────────────────────────────────

    def _flush_sample_buffer(self, file_handle: IO[str]) -> None:
        """Run detection on the buffered samples and flush them to disk.

        Writes the column header (if not already written) using the
        detected mode, then writes every buffered row with its original
        timestamp. Clears the buffer.

        Safe to call with an empty buffer — it runs detection on an
        empty sample (producing RAW mode) and writes just the header.
        """
        # Extract the raw line strings for detection while preserving
        # the (timestamp, line) pairs for replay.
        sample_lines = [line for _, line in self._sample_buffer]
        self.detect_mode(sample_lines)

        # Write the header now that we know the mode.
        self._write_header_once(file_handle)

        # Replay every buffered row with its original timestamp.
        for original_timestamp, original_line in self._sample_buffer:
            self._write_single_row(
                file_handle, original_timestamp, original_line,
            )
        self._sample_buffer.clear()

    def _write_header_once(self, file_handle: IO[str]) -> None:
        """Write the column header line, but only the first time."""
        if self._header_written:
            return

        if self._mode == CSVMode.AUTO and self._columns:
            header = ",".join(["Timestamp", *self._columns])
        else:
            header = "Timestamp,Data"

        file_handle.write(header + "\n")
        self._header_written = True

    def parse_line(self, line: str) -> ParsedRow | None:
        """Parse a single line according to the detected structure.

        Returns:
            ParsedRow with column values, or None if parsing fails.
            Emits row_parsed signal on success (v2.1 plotter hook).
        """
        if self._mode == CSVMode.RAW or not self._columns:
            return None

        values = self._extract_values(line)
        if values:
            self.row_parsed.emit(values)
            return ParsedRow(columns=values)

        return None

    # ── Internal: Detection ──────────────────────────────────────────────

    def _try_detect_json(self, lines: list[str]) -> list[str] | None:
        """Try to detect JSON structure from sample lines.

        Returns column names if >= 50% of lines parse as JSON with
        consistent keys, None otherwise.
        """
        all_keys: list[set[str]] = []

        for line in lines:
            stripped = line.strip()
            if not stripped.startswith("{"):
                continue
            try:
                obj = json.loads(stripped)
                if isinstance(obj, dict) and obj:
                    all_keys.append(set(obj.keys()))
            except (json.JSONDecodeError, ValueError):
                continue

        if not all_keys or len(all_keys) < len(lines) * 0.5:
            return None

        # Use intersection of all key sets for consistency
        common_keys = all_keys[0]
        for keys in all_keys[1:]:
            common_keys &= keys

        if not common_keys:
            return None

        # Return keys in the order they appeared in the first valid object
        first_line = None
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("{"):
                try:
                    first_line = json.loads(stripped)
                    break
                except (json.JSONDecodeError, ValueError):
                    continue

        if first_line and isinstance(first_line, dict):
            return [k for k in first_line if k in common_keys]

        return sorted(common_keys)

    def _try_detect_kv(self, lines: list[str]) -> list[str] | None:
        """Try to detect key:value or key=value structure.

        Returns column names if >= 50% of lines contain consistent
        key-value pairs, None otherwise.
        """
        all_keys: list[list[str]] = []

        for line in lines:
            matches = self._KV_PATTERN.findall(line)
            if matches:
                keys = [m[0].strip() for m in matches]
                all_keys.append(keys)

        if not all_keys or len(all_keys) < len(lines) * 0.5:
            return None

        # Find consistent key set across samples
        key_sets = [set(keys) for keys in all_keys]
        common_keys = key_sets[0]
        for ks in key_sets[1:]:
            common_keys &= ks

        if not common_keys:
            return None

        # Return keys in order of first appearance
        return [k for k in all_keys[0] if k in common_keys]

    # ── Internal: Row Writing ────────────────────────────────────────────

    def _write_single_row(
        self,
        file_handle: IO[str],
        timestamp: str,
        line: str,
    ) -> None:
        """Write a single row in the detected mode."""
        if self._mode == CSVMode.AUTO and self._columns:
            values = self._extract_values(line)
            if values:
                row_values = [values.get(col, "") for col in self._columns]
                row = ",".join([timestamp] + [self._csv_escape(v) for v in row_values])
                file_handle.write(row + "\n")

                # Emit for plotter hook (v2.1)
                self.row_parsed.emit(values)
                return

        # Fall back to raw
        self._write_raw_row(file_handle, timestamp, line)

    def _write_raw_row(
        self,
        file_handle: IO[str],
        timestamp: str,
        line: str,
    ) -> None:
        """Write a raw CSV row: Timestamp,Data."""
        escaped = self._csv_escape(line)
        file_handle.write(f"{timestamp},{escaped}\n")

    def _extract_values(self, line: str) -> dict[str, str] | None:
        """Extract column values from a line based on detected structure.

        Returns:
            Dict mapping column name → value string, or None if extraction fails.
        """
        stripped = line.strip()

        # Try JSON
        if stripped.startswith("{"):
            try:
                obj = json.loads(stripped)
                if isinstance(obj, dict):
                    return {k: str(v) for k, v in obj.items()
                            if k in self._columns}
            except (json.JSONDecodeError, ValueError):
                pass

        # Try key:value / key=value
        matches = self._KV_PATTERN.findall(line)
        if matches:
            result = {}
            for key, value in matches:
                key = key.strip()
                if key in self._columns:
                    result[key] = value.strip()
            if result:
                return result

        return None

    @staticmethod
    def _csv_escape(value: str) -> str:
        """Escape a value for CSV output (RFC 4180 compliant).

        Wraps in quotes if the value contains commas, quotes, or newlines.
        Internal quotes are doubled.
        """
        if not value:
            return ""
        if "," in value or '"' in value or "\n" in value or "\r" in value:
            return '"' + value.replace('"', '""') + '"'
        return value
