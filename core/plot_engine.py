# SPDX-License-Identifier: MIT
"""WireTrace live-plot data engine.

Subscribes to the serial line stream and produces plot-ready data via:
  1. Tag filter — only DATA-tagged lines reach the engine (severity
     and command lines are excluded, mirroring CSV Option C).
  2. Sample buffering — the first ``PLOT_AUTODETECT_SAMPLE_SIZE`` lines
     are accumulated together with their timestamps; once the buffer
     is full, the parser pipeline runs detection.
  3. Hard cap — if no structure is detected after
     ``PLOT_DETECTION_GIVE_UP_LINES`` DATA lines, the engine emits
     ``detection_failed`` and stays idle for the rest of the session.
  4. Ring buffers — once columns are detected, every parsed line
     appends ``(timestamp, value)`` to per-column ring buffers
     (``PLOT_RING_BUFFER_SIZE`` points each, ``numpy.float64``).
  5. Snapshot API — the view pulls ordered (oldest-first) (x, y)
     arrays via ``snapshot(col)`` on its redraw timer.

Time semantics:
  - Each tab establishes ``session_start = time.perf_counter()`` on
    its first DATA line. Every sample's X is ``time.perf_counter() -
    session_start`` (always >= 0).
  - ``perf_counter`` is used in preference to ``monotonic`` because
    its resolution is uniformly sub-microsecond across Linux, macOS,
    and Windows. ``monotonic`` on Windows has ~15.6 ms granularity,
    which is fine for wall-clock measurements but collapses cluster-
    arrival timestamps (e.g. when ParserPipeline replays its 50-line
    buffer in a tight loop) into a single tick. Both clocks share the
    same monotonic, no-wallclock-drift contract — only the resolution
    differs.
  - ``latest_x`` is the X of the most recently appended sample; it
    DOES NOT drift past that value when data stops flowing. The view
    uses ``latest_x`` (not the live monotonic clock) to anchor its
    scrolling window, so the trace stays put when data stops.

Late-bind:
  PlotView is constructed lazily on first toggle. By the time it
  comes into being, detection may already be complete. The view
  queries ``columns`` / ``detection_complete`` / ``detection_gave_up``
  in its ``__init__`` and rebuilds traces from existing buffers.

Threading:
  Both ``process`` (main-thread Qt slot from DeviceTab) and ``snapshot``
  (main-thread redraw timer) run in the GUI thread. No cross-thread
  shared state, no locks.

This module does NOT touch GUI, disk, serial, or any I/O.
"""

from __future__ import annotations

import enum
import logging
import time

import numpy as np
from PySide6.QtCore import QObject, Signal, Slot

from app.constants import TAG_DATA
from core.plot_parsers import ParserPipeline, RegexParser

logger = logging.getLogger(__name__)


# ── Engine Configuration ─────────────────────────────────────────────────────

#: Number of DATA-tagged lines collected before detection runs.
PLOT_AUTODETECT_SAMPLE_SIZE = 50

#: Hard cap on DATA-tagged lines processed without detection success.
#: Beyond this, the engine gives up and emits ``detection_failed``.
PLOT_DETECTION_GIVE_UP_LINES = 200

#: Per-column ring buffer capacity in samples. At 8 traces x 10 000
#: float64 x 2 (x, y) ~ 1.3 MB of plot memory per tab.
PLOT_RING_BUFFER_SIZE = 10_000

#: Number of recent DATA lines retained for the Configure Plot dialog
#: to display as reference and to test patterns against. Independent of
#: the auto-detect sample buffer because the dialog should work even
#: when auto-detect already succeeded or gave up long ago.
PLOT_RECENT_LINES_BUFFER_SIZE = 200


class PlotMode(enum.Enum):
    """Engine operating mode.

    AUTO  — ParserPipeline runs detection over a sample of lines and
            picks JSON / KV / Delimited automatically.
    MANUAL — User-declared regex with named groups; columns are known
             up-front from the pattern, no detection needed.
    """

    AUTO = "auto"
    MANUAL = "manual"


class PlotEngine(QObject):
    """Owns the parser pipeline and per-column ring buffers.

    One PlotEngine per DeviceTab. Constructed eagerly so it can buffer
    detection samples from session start, even if the user opens the
    plot panel later.

    Signals:
        columns_detected(list):   Emitted once per session when the
                                  parser pipeline detects structure.
                                  Carries the list of column names.
        detection_failed():       Emitted if no structure is detected
                                  after the hard-cap line count.
    """

    # ── Signals ──────────────────────────────────────────────────────────

    columns_detected = Signal(list)
    detection_failed = Signal()

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

        # Ring buffers — populated after columns_detected
        self._columns: list[str] = []
        self._x: dict[str, np.ndarray] = {}
        self._y: dict[str, np.ndarray] = {}
        self._head: dict[str, int] = {}   # next-write index
        self._count: dict[str, int] = {}  # samples written so far

        # Session timing — first DATA line establishes t=0.
        self._session_start: float | None = None
        # Most recent sample's X. Stays put when no data flows; this
        # is what the view uses to anchor its window.
        self._latest_x: float = 0.0

        # Rolling buffer of recent DATA lines, available to the
        # Configure Plot dialog as live reference material and as the
        # corpus for the Test button. Independent of the detection
        # sample buffer because this stays alive across sessions.
        self._recent_lines: list[str] = []

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
    def active_parser_name(self) -> str | None:
        """Name of the parser that won detection, or None."""
        if self._mode == PlotMode.MANUAL:
            return RegexParser.name if self._regex_parser else None
        return self._pipeline.active_parser_name

    @property
    def latest_x(self) -> float:
        """X coordinate of the most recently appended sample.

        This is what the view uses to anchor its scrolling window.
        Critically, it does NOT advance past the last sample when data
        stops flowing — so a paused or disconnected stream keeps its
        view in place rather than drifting into empty space.
        """
        return self._latest_x

    @property
    def has_any_data(self) -> bool:
        """True once at least one sample has been appended to a buffer."""
        return any(c > 0 for c in self._count.values())

    def recent_lines(self) -> list[str]:
        """Snapshot of the most recent DATA lines.

        Used by the Configure Plot dialog to display reference material
        and to evaluate user-entered patterns. Returned list is a copy.
        """
        return list(self._recent_lines)

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
        # Construct first — raises before any state mutation if invalid.
        parser = RegexParser(pattern)

        self._mode = PlotMode.MANUAL
        self._regex_parser = parser

        # Discard any auto-detect state: the pipeline isn't used in
        # manual mode but might still hold sample data from before.
        self._pipeline = ParserPipeline()
        self._sample_buffer.clear()
        self._lines_seen = 0
        self._detection_failed_emitted = False
        # Reset ring buffers — old data is from a different schema.
        self._columns = []
        self._x.clear()
        self._y.clear()
        self._head.clear()
        self._count.clear()
        self._session_start = None
        self._latest_x = 0.0

        # Initialise columns from the pattern's named groups.
        self._init_columns(parser.columns)
        self._detection_complete = True
        self.columns_detected.emit(list(parser.columns))

    def reset(self) -> None:
        """Reset the engine for a new session.

        Discards detected structure, clears ring buffers, resets the
        sample state. Safe to call mid-session — subsequent ``process``
        calls run detection again from scratch.

        Manual-mode behaviour:
          The user-declared regex pattern is preserved across reset.
          A reconnect on a manually-configured tab keeps the same
          pattern so the engineer doesn't have to re-enter it. To
          discard the pattern, switch to auto via ``set_auto_config()``.
        """
        if self._mode == PlotMode.MANUAL and self._regex_parser is not None:
            # Preserve the manual config; just clear runtime state.
            preserved_parser = self._regex_parser
            self._pipeline = ParserPipeline()
            self._sample_buffer.clear()
            self._lines_seen = 0
            self._detection_failed_emitted = False
            self._columns = []
            self._x.clear()
            self._y.clear()
            self._head.clear()
            self._count.clear()
            self._session_start = None
            self._latest_x = 0.0
            self._recent_lines.clear()
            # Re-initialise from the preserved pattern and re-fire the
            # signal so the view rebuilds its traces for the new
            # session.
            self._regex_parser = preserved_parser
            self._init_columns(preserved_parser.columns)
            self._detection_complete = True
            self.columns_detected.emit(list(preserved_parser.columns))
            return

        # Auto mode: full reset
        self._pipeline = ParserPipeline()
        self._sample_buffer.clear()
        self._lines_seen = 0
        self._detection_complete = False
        self._detection_failed_emitted = False
        self._columns = []
        self._x.clear()
        self._y.clear()
        self._head.clear()
        self._count.clear()
        self._session_start = None
        self._latest_x = 0.0
        self._recent_lines.clear()

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
        returned arrays are copies — safe to retain.
        """
        if col not in self._x:
            return np.empty(0, dtype=np.float64), np.empty(0, dtype=np.float64)
        count = self._count[col]
        head = self._head[col]
        if count < self._buffer_size:
            x = self._x[col][:count].copy()
            y = self._y[col][:count].copy()
        else:
            # Buffer wrapped — reorder so oldest sample is first
            x = np.concatenate([self._x[col][head:], self._x[col][:head]])
            y = np.concatenate([self._y[col][head:], self._y[col][:head]])
        return x, y

    # ── Slot ─────────────────────────────────────────────────────────────

    @Slot(str, str)
    def process(self, line: str, tag: str) -> None:
        """Process one serial line. Main-thread Qt slot.

        Filters non-DATA tags, accumulates samples for detection, then
        extracts numeric values and appends to ring buffers. Exceptions
        are logged and swallowed — a malformed line never propagates to
        crash the tab.
        """
        if tag != TAG_DATA:
            return

        # Track recent DATA lines for the Configure Plot dialog. This
        # runs BEFORE any other early-return so the dialog has
        # reference material even after auto-detect gave up — that's
        # exactly when the user wants to switch to manual mode.
        self._recent_lines.append(line)
        if len(self._recent_lines) > PLOT_RECENT_LINES_BUFFER_SIZE:
            del self._recent_lines[: len(self._recent_lines)
                                     - PLOT_RECENT_LINES_BUFFER_SIZE]

        if self._detection_failed_emitted:
            return

        try:
            self._process_one(line)
        except Exception:
            logger.exception("Plot engine swallowed exception on line: %r", line)

    # ── Internal ─────────────────────────────────────────────────────────

    def _process_one(self, line: str) -> None:
        if self._session_start is None:
            self._session_start = time.perf_counter()

        ts = time.perf_counter() - self._session_start
        self._lines_seen += 1

        # ── Manual mode: direct extract, no buffering or detection ──
        if self._mode == PlotMode.MANUAL:
            assert self._regex_parser is not None
            values = self._regex_parser.extract(line)
            if values:
                self._append(values, ts)
            self._latest_x = ts
            return

        # ── Auto mode: existing detection + extract flow ──
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
                self._init_columns(cols)
                self._detection_complete = True
                self.columns_detected.emit(list(cols))
                # Replay each buffered line at ITS OWN timestamp, so
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

        # Detection complete — extract directly
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
