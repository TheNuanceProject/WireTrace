# SPDX-License-Identifier: GPL-3.0-or-later
"""WireTrace live-plot data engine.

Receives the lines of a session and produces plot-ready data via:
  1. Sample buffering: the first ``PLOT_AUTODETECT_SAMPLE_SIZE`` lines
     are accumulated together with their timestamps; once the buffer
     is full, the parser pipeline runs detection.
  2. Hard cap: if no structure is detected after
     ``PLOT_DETECTION_GIVE_UP_LINES`` lines, the engine emits
     ``detection_failed`` and stays idle for the rest of the session.
  3. Ring buffers: once columns are detected, every parsed line
     appends ``(timestamp, value)`` to per-column ring buffers
     (``PLOT_RING_BUFFER_SIZE`` points each, ``numpy.float64``).
  4. Snapshot API: the view pulls ordered (oldest-first) (x, y)
     arrays via ``snapshot(col)`` on its redraw timer.

Which lines are plotted:
  A line is plotted if the detected parser can extract a value from
  it. The severity tag plays no part, so readings that happen to
  contain a word like "errors" are plotted like any other line.

Activation:
  Parsing costs time on every line, so it only runs while the engine
  is active; the owning tab activates it while the plot panel is open.
  An inactive engine only remembers the most recent lines. When it is
  activated, detection starts again from those lines, so a plot opened
  late shows data at once instead of waiting for a fresh sample.

Time semantics:
  - X is the time a line arrived, in seconds since the first line the
    engine plotted. The arrival time is supplied by the caller (the
    reader stamps every line), so the plot, the console, and the log
    agree.
  - ``latest_x`` is the X of the most recently appended sample. It does
    not advance when data stops flowing. The view anchors its scrolling
    window to ``latest_x``, not to a live clock, so the trace stays put
    when data stops.

Late-bind:
  PlotView is constructed lazily on first toggle. By the time it
  comes into being, detection may already be complete. The view
  queries ``columns`` / ``detection_complete`` / ``detection_gave_up``
  in its ``__init__`` and rebuilds traces from existing buffers.

Threading:
  Both ``process`` and ``snapshot`` run in the GUI thread. No
  cross-thread shared state, no locks.

This module does not touch the GUI, the disk, the serial port or any other I/O.
"""

from __future__ import annotations

import enum
import logging
from collections import deque
from datetime import datetime, timedelta

import numpy as np
from PySide6.QtCore import QObject, Signal

from app.constants import (
    PLOT_AUTODETECT_SAMPLE_SIZE,
    PLOT_DETECTION_GIVE_UP_LINES,
    PLOT_MAX_LINE_CHARACTERS,
    PLOT_MAX_SERIES,
    PLOT_RECENT_LINES_BUFFER_SIZE,
    PLOT_REGEX_MAX_CONSECUTIVE_TIMEOUTS,
    PLOT_RING_BUFFER_SIZE,
)
from core.plot_parsers import ParserPipeline, RegexParser

logger = logging.getLogger(__name__)


class PlotMode(enum.Enum):
    """Engine operating mode.

    AUTO : ParserPipeline runs detection over a sample of lines and
            picks JSON / KV / Delimited automatically.
    MANUAL: User-declared regex with named groups; columns are known
             up-front from the pattern, no detection needed.
    """

    AUTO = "auto"
    MANUAL = "manual"


class PlotEngine(QObject):
    """Owns the parser pipeline and per-column ring buffers.

    One PlotEngine per DeviceTab. Constructed eagerly so it can keep
    the most recent lines from session start, even if the user opens
    the plot panel later.

    Signals:
        columns_detected(list):   Emitted when the columns become known:
                                  after detection in auto mode, and at
                                  once in manual mode and after a reset.
                                  Carries the column names.
        detection_failed():       Emitted if no structure is detected
                                  after the hard-cap line count.
        pattern_stalled():        Emitted when the user's pattern ran out
                                  of time on several lines in a row.
                                  Plotting has stopped.
    """

    # ── Signals ──────────────────────────────────────────────────────────

    columns_detected = Signal(list)
    detection_failed = Signal()
    pattern_stalled = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._mode: PlotMode = PlotMode.AUTO
        self._pipeline = ParserPipeline()
        self._regex_parser: RegexParser | None = None
        # Sample buffer holds (line, timestamp) pairs so replayed
        # samples land at their original X positions, not all at the
        # detection moment.
        self._sample_buffer: list[tuple[str, float]] = []
        self._sample_size = PLOT_AUTODETECT_SAMPLE_SIZE
        self._max_attempts = PLOT_DETECTION_GIVE_UP_LINES
        self._buffer_size = PLOT_RING_BUFFER_SIZE
        self._lines_seen = 0
        self._detection_complete = False
        self._detection_failed_emitted = False
        self._stalled = False

        # Ring buffers: populated after columns_detected
        self._columns: list[str] = []
        self._x: dict[str, np.ndarray] = {}
        self._y: dict[str, np.ndarray] = {}
        self._head: dict[str, int] = {}   # next-write index
        self._count: dict[str, int] = {}  # samples written so far

        # Session timing: the first plotted line establishes t=0.
        self._session_start: datetime | None = None
        # Most recent sample's X. Stays put when no data flows; this
        # is what the view uses to anchor its window.
        self._latest_x: float = 0.0

        # The most recent lines with their arrival times, kept whether
        # or not the engine is active.
        self._recent: deque[tuple[str, datetime]] = deque(
            maxlen=PLOT_RECENT_LINES_BUFFER_SIZE,
        )

        # A standalone engine parses everything it is given. The owning
        # tab switches it off while the plot panel is closed.
        self._active = True

    # ── Public API ───────────────────────────────────────────────────────

    @property
    def mode(self) -> PlotMode:
        return self._mode

    @property
    def manual_pattern(self) -> str | None:
        """The active manual-mode regex pattern, or None in auto mode."""
        return self._regex_parser.pattern if self._regex_parser else None

    @property
    def columns(self) -> list[str]:
        """Detected column names (empty before detection)."""
        return list(self._columns)

    @property
    def detection_complete(self) -> bool:
        """True once a parser has detected structure."""
        return self._detection_complete

    @property
    def detection_gave_up(self) -> bool:
        """True if the engine exceeded the hard-cap without detecting."""
        return self._detection_failed_emitted

    @property
    def stalled(self) -> bool:
        """True if plotting stopped because the pattern kept running out
        of time. Cleared when the configuration changes or on reset."""
        return self._stalled

    @property
    def active_parser_name(self) -> str | None:
        """Name of the parser that won detection, or None."""
        if self._mode == PlotMode.MANUAL:
            return RegexParser.name if self._regex_parser else None
        return self._pipeline.active_parser_name

    @property
    def latest_x(self) -> float:
        """X coordinate of the most recently appended sample.

        This is what the view uses to anchor its scrolling window.
        It does not advance past the last sample when data stops
        flowing, so a paused or disconnected stream keeps its view in
        place rather than drifting into empty space.
        """
        return self._latest_x

    def time_of(self, x: float) -> datetime | None:
        """When the line plotted at ``x`` was received; None before any was."""
        if self._session_start is None:
            return None
        return self._session_start + timedelta(seconds=x)

    @property
    def has_any_data(self) -> bool:
        """True once at least one sample has been appended to a buffer."""
        return any(c > 0 for c in self._count.values())

    def set_active(self, active: bool) -> None:
        """Switch parsing on or off.

        Switching on restarts detection from the most recent lines, so
        the plot has data to show immediately.
        """
        if active == self._active:
            return
        self._active = active
        if active:
            self._restart_from_recent()

    def recent_lines(self) -> list[str]:
        """Snapshot of the most recent lines.

        Used by the Configure plot dialog to display reference material
        and to evaluate user-entered patterns. Returned list is a copy.
        """
        return [line for line, _ in self._recent]

    def set_auto_config(self) -> None:
        """Switch to AUTO mode and reset.

        The pipeline is recreated so any previously-selected parser
        state (delimiter, column names) is discarded. Subsequent
        ``process`` calls run detection from scratch.
        """
        self._mode = PlotMode.AUTO
        self._regex_parser = None
        self.reset()

    def set_manual_config(self, pattern: str) -> None:
        """Switch to MANUAL mode with the given regex pattern.

        Columns become the pattern's named groups, in declaration
        order. Detection state goes to "complete" immediately and
        ``columns_detected`` fires so the view rebuilds its traces.

        Raises:
            RegexParserError: pattern is empty, fails to compile,
                or contains no named groups.
        """
        # Construct first: raises before any state mutation if invalid.
        parser = RegexParser(pattern)

        self._mode = PlotMode.MANUAL
        self._regex_parser = parser
        # Old data is from a different schema; the recent lines stay,
        # because they are what the pattern is tested against.
        self._reset_runtime_state()

    def reset(self) -> None:
        """Reset the engine for a new session.

        Discards detected structure, clears ring buffers and the recent
        lines, and resets the sample state. Safe to call mid-session:
        subsequent ``process`` calls run detection again from scratch.

        Manual-mode behaviour:
          The user-declared regex pattern is preserved across reset.
          A reconnect on a manually-configured tab keeps the same
          pattern so the engineer doesn't have to re-enter it. To
          discard the pattern, switch to auto via ``set_auto_config()``.
        """
        self._recent.clear()
        self._reset_runtime_state()

    def _reset_runtime_state(self) -> None:
        """Discard detection and buffers; keep mode, pattern, and recent lines."""
        self._pipeline = ParserPipeline()
        self._sample_buffer.clear()
        self._lines_seen = 0
        self._detection_complete = False
        self._detection_failed_emitted = False
        self._stalled = False
        self._columns = []
        self._x.clear()
        self._y.clear()
        self._head.clear()
        self._count.clear()
        self._session_start = None
        self._latest_x = 0.0

        if self._mode == PlotMode.MANUAL and self._regex_parser is not None:
            # Columns come from the pattern, so they are known at once.
            # Re-firing the signal lets the view rebuild its traces.
            self._init_columns(self._regex_parser.columns)
            self._detection_complete = True
            self.columns_detected.emit(list(self._regex_parser.columns))

    def _restart_from_recent(self) -> None:
        """Run detection again, feeding the recent lines as the first input."""
        self._reset_runtime_state()
        for line, timestamp in list(self._recent):
            if self._detection_failed_emitted or self._stalled:
                break
            self._process_guarded(line, timestamp)

    def clear_buffers(self) -> None:
        """Clear ring buffers but retain detected columns.

        Called by the plot's "Clear" button. Trace history disappears
        but new data continues feeding the same columns.
        """
        for col in self._columns:
            self._head[col] = 0
            self._count[col] = 0
        # Reset session start so the cleared plot starts X at 0 again.
        self._session_start = None
        self._latest_x = 0.0

    def snapshot(
        self, col: str,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return ordered (oldest-first) ``(x, y)`` arrays for ``col``.

        Called by the view's redraw timer. Returns empty arrays if the
        column isn't known or no samples have been written yet. The
        returned arrays are copies: safe to retain.
        """
        if col not in self._x:
            return np.empty(0, dtype=np.float64), np.empty(0, dtype=np.float64)
        count = self._count[col]
        head = self._head[col]
        if count < self._buffer_size:
            x = self._x[col][:count].copy()
            y = self._y[col][:count].copy()
        else:
            # Buffer wrapped: reorder so oldest sample is first
            x = np.concatenate([self._x[col][head:], self._x[col][:head]])
            y = np.concatenate([self._y[col][head:], self._y[col][:head]])
        return x, y

    # ── Input ────────────────────────────────────────────────────────────

    def process(self, line: str, timestamp: datetime) -> None:
        """Process one line that arrived at ``timestamp``.

        Accumulates samples for detection, then extracts numeric values
        and appends to ring buffers. Exceptions are logged and
        swallowed: a malformed line never propagates to crash the tab.
        """
        line = line[:PLOT_MAX_LINE_CHARACTERS]
        self._recent.append((line, timestamp))
        if not self._active or self._detection_failed_emitted or self._stalled:
            return
        self._process_guarded(line, timestamp)

    # ── Internal ─────────────────────────────────────────────────────────

    def _process_guarded(self, line: str, timestamp: datetime) -> None:
        try:
            self._process_one(line, timestamp)
        except Exception:
            logger.exception("Plot engine swallowed exception on line: %r", line)

    def _process_one(self, line: str, timestamp: datetime) -> None:
        if self._session_start is None:
            self._session_start = timestamp

        ts = (timestamp - self._session_start).total_seconds()
        self._lines_seen += 1

        # Manual mode: extract directly, with no buffering or detection.
        if self._mode == PlotMode.MANUAL:
            assert self._regex_parser is not None
            values = self._regex_parser.extract(line)
            if values:
                self._append(values, ts)
            self._latest_x = ts
            if self._regex_parser.consecutive_timeouts >= PLOT_REGEX_MAX_CONSECUTIVE_TIMEOUTS:
                self._stalled = True
                self.pattern_stalled.emit()
            return

        # Auto mode: detect first, then extract.
        if not self._detection_complete:
            self._sample_buffer.append((line, ts))

            should_attempt = (
                len(self._sample_buffer) >= self._sample_size
                or self._lines_seen >= self._max_attempts
            )
            if not should_attempt:
                # Track latest_x even before detection so any UI that
                # peeks at it gets a sensible value.
                self._latest_x = ts
                return

            # Detection runs over the lines only; timestamps come
            # along separately so replay places points at their
            # original X coordinates.
            sample_lines = [buf_line for buf_line, _ in self._sample_buffer]
            cols = self._pipeline.detect(sample_lines)
            if cols is not None:
                cols = cols[:PLOT_MAX_SERIES]
                self._init_columns(cols)
                self._detection_complete = True
                self.columns_detected.emit(list(cols))
                # Replay each buffered line at its own timestamp, so
                # the ring buffer reflects the actual arrival cadence.
                for buf_line, buf_ts in self._sample_buffer:
                    values = self._pipeline.extract(buf_line)
                    if values:
                        self._append(values, buf_ts)
                self._sample_buffer.clear()
                self._latest_x = ts
            elif self._lines_seen >= self._max_attempts:
                self._detection_failed_emitted = True
                self._sample_buffer.clear()
                self.detection_failed.emit()
            return

        # Detection complete: extract directly
        values = self._pipeline.extract(line)
        if values:
            self._append(values, ts)
        self._latest_x = ts

    def _init_columns(self, cols: list[str]) -> None:
        self._columns = list(cols)
        for col in cols:
            self._x[col] = np.zeros(self._buffer_size, dtype=np.float64)
            self._y[col] = np.zeros(self._buffer_size, dtype=np.float64)
            self._head[col] = 0
            self._count[col] = 0

    def _append(self, values: dict[str, float], ts: float) -> None:
        for col, val in values.items():
            if col not in self._x:
                continue
            idx = self._head[col]
            self._x[col][idx] = ts
            self._y[col][idx] = val
            self._head[col] = (idx + 1) % self._buffer_size
            if self._count[col] < self._buffer_size:
                self._count[col] += 1
