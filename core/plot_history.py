# SPDX-License-Identifier: GPL-3.0-or-later
"""Older plot values, read back from the session recording.

The plot engine keeps the newest samples of each value in memory. Older
values come from the session recording (``core/session_spool.py``),
which holds every line of the session up to its size limit. A
``HistoryReader`` reads it on a thread of its own, in two ways:

  Following: it reads the recording from the start of the session and
  keeps up with it as it grows, finds the values in each device line
  with a parser of its own (the same way the engine does), and hands
  over the samples in batches. The plot keeps them as an overview: the
  least and greatest sample of each slice of time (``core/plot_overview``).
  While reading it notes the time each block of the recording covers.

  Detail: for a range of older time held still on screen it reads just
  the blocks that cover the range and returns every sample in it, or
  the least and greatest per pixel when there are more than the chart
  can show, together with the exact count, least, greatest, sum and
  last value of the range.

A detail range stops where the samples in memory begin. Samples received
at the same moment as the oldest one in memory are split exactly: the
range takes as many of them as are no longer in memory.

The reader never touches the engine. Everything it hands over carries
the engine's generation, so values found under an older configuration
are recognised and dropped.

This module does not touch the GUI or the serial port.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from app.constants import (
    MS_PER_SECOND,
    PLOT_HISTORY_DETAIL_MAX_LINES,
    PLOT_HISTORY_HANDOVER_LINES,
    PLOT_HISTORY_HANDOVER_MS,
    PLOT_HISTORY_POLL_MS,
    PLOT_HISTORY_SAMPLES_PER_PIXEL,
    PLOT_MAX_LINE_CHARACTERS,
    PLOT_REGEX_MAX_CONSECUTIVE_TIMEOUTS,
)
from app.diagnostic_log import log_data_failure
from core.plot_overview import Overview, OverviewFigures
from core.records import LineRecord, RecordOrigin
from core.session_spool import SpoolPosition, SpoolRange, SpoolSnapshot

logger = logging.getLogger(__name__)

_THREAD_NAME = "wiretrace-plot-history"
#: Segments taken at a time while following, so that a reader far
#: behind lets the recording discard its oldest segments one by one.
_SEGMENTS_PER_READ = 1

Recording = Callable[[SpoolPosition | None, int | None], SpoolSnapshot | None]
Samples = dict[str, tuple[np.ndarray, np.ndarray]]


@dataclass(frozen=True)
class HistorySource:
    """What to read: the engine's start of time, values and parser.

    ``parser`` belongs to the reader from now on; it has an
    ``extract(line)`` method giving each value found in a line.
    """

    generation: int
    origin: datetime
    columns: tuple[str, ...]
    parser: object


@dataclass(frozen=True)
class HistoryBatch:
    """Samples read while following, in the order they were recorded.

    Attributes:
        reached: The time of the latest device line read so far, or None
            before any.
        start_lost: The start of the session was no longer held when
            reading began, or a stretch was discarded before it could
            be read.
        stalled: The pattern took too long on the lines; reading stopped.
    """

    generation: int
    samples: Samples
    reached: float | None = None
    start_lost: bool = False
    stalled: bool = False


@dataclass(frozen=True)
class DetailRequest:
    """A range of older time to read in full.

    ``held`` gives, for each value with samples in memory, the time of
    its oldest one and how many samples in memory share that time; the
    range stops there for that value.
    """

    generation: int
    low: float
    high: float
    pixels: int
    held: dict[str, tuple[float, int]] = field(default_factory=dict)


@dataclass(frozen=True)
class RangeFigures:
    """Exact figures of one value over a range read in full."""

    count: int
    minimum: float
    maximum: float
    total: float
    last_x: float
    last_y: float


@dataclass(frozen=True)
class DetailResult:
    """What a detail read found.

    Attributes:
        every_sample: Each value's samples are all there, not only the
            least and greatest per pixel.
        complete: Every line of the range was still in the recording,
            and the range was not too long to read in full.
    """

    request: DetailRequest
    samples: Samples
    figures: dict[str, RangeFigures]
    every_sample: bool
    complete: bool


@dataclass(frozen=True, slots=True)
class _Indexed:
    """A block of the recording and the span of time its records cover."""

    where: SpoolRange
    earliest: float
    latest: float


class _Collector:
    """Gathers the samples of a range for one value: every one while
    they fit, and always the overview the figures come from."""

    def __init__(self, low: float, high: float, pixels: int) -> None:
        self._low = low
        slices = max(2, pixels + pixels % 2)
        self.overview = Overview(slices, max((high - low) / slices, np.finfo(float).tiny))
        self._limit = pixels * PLOT_HISTORY_SAMPLES_PER_PIXEL
        self._x: list[float] = []
        self._y: list[float] = []
        self.every_sample = True
        self.last: tuple[float, float] | None = None

    def add(self, x: list[float], y: list[float]) -> None:
        if not x:
            return
        self.overview.add(np.asarray(x) - self._low, np.asarray(y))
        self.last = x[-1], y[-1]
        if self.every_sample:
            self._x.extend(x)
            self._y.extend(y)
            if len(self._x) > self._limit:
                self.every_sample = False
                self._x, self._y = [], []

    def samples(self) -> tuple[np.ndarray, np.ndarray]:
        if self.every_sample:
            return np.asarray(self._x, dtype=float), np.asarray(self._y, dtype=float)
        x, y = self.overview.samples(0.0, np.inf)
        return x + self._low, y

    def figures(self) -> RangeFigures | None:
        found: OverviewFigures | None = self.overview.figures(0.0, np.inf)
        if found is None or self.last is None:
            return None
        return RangeFigures(
            found.count, found.minimum, found.maximum, found.total, *self.last,
        )


class HistoryReader:
    """Reads older plot values from the recording on a thread of its own.

    Args:
        recording: Gives what the session recorded after a position, at
            most a number of segments (``DevicePipeline.recording``).
        deliver: Called on the reader's thread with each ``HistoryBatch``
            and ``DetailResult``.
    """

    def __init__(self, recording: Recording, deliver: Callable[[object], None]) -> None:
        self._recording = recording
        self._deliver = deliver
        self._condition = threading.Condition()
        self._source: HistorySource | None = None
        self._detail: DetailRequest | None = None
        self._reading = False
        self._stopping = False
        self._thread: threading.Thread | None = None
        # Reader-thread state.
        self._following: HistorySource | None = None
        self._position: SpoolPosition | None = None
        self._index: list[_Indexed] = []
        # The time on the plot up to which the recording was discarded
        # after it was indexed.
        self._gone_until = -np.inf
        self._stalled = False

    # ── Control (any thread) ─────────────────────────────────────────────

    def follow(self, source: HistorySource | None) -> None:
        """Read ``source`` from the start of the session; None stops following."""
        with self._condition:
            self._source = source
            self._detail = None
            self._condition.notify()
        self._ensure_thread()

    def set_reading(self, reading: bool) -> None:
        """Read only while the plot is shown; a detail request is answered either way."""
        with self._condition:
            self._reading = reading
            self._condition.notify()

    def request_detail(self, request: DetailRequest | None) -> None:
        """Read ``request`` in full, replacing any earlier request not yet answered."""
        with self._condition:
            self._detail = request
            self._condition.notify()
        self._ensure_thread()

    def stop(self) -> None:
        """Stop reading and wait for the thread to end."""
        with self._condition:
            self._stopping = True
            self._source = None
            self._detail = None
            self._condition.notify()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    # ── Thread ───────────────────────────────────────────────────────────

    def _ensure_thread(self) -> None:
        if self._thread is None and not self._stopping:
            self._thread = threading.Thread(target=self._run, name=_THREAD_NAME, daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            with self._condition:
                if self._stopping:
                    return
                source, detail, reading = self._source, self._detail, self._reading
                self._detail = None
            try:
                if source is not self._following:
                    self._start_following(source)
                if detail is not None:
                    if source is not None and detail.generation == source.generation:
                        self._read_detail(source, detail)
                    continue
                if source is not None and reading and not self._stalled and self._follow_once(source):
                    continue
            except Exception:
                # A fault in reading must not take the plot with it: older
                # values stop, and the log says why.
                log_data_failure(logger, "Reading older plot values failed")
                with self._condition:
                    if self._source is source:
                        self._source = None
                continue
            with self._condition:
                if not self._stopping and self._detail is None and self._source is source:
                    # Following looks for new records now and then;
                    # otherwise there is nothing to do until asked.
                    idle = source is None or not self._reading or self._stalled
                    self._condition.wait(None if idle else PLOT_HISTORY_POLL_MS / MS_PER_SECOND)

    def _changed(self, source: HistorySource) -> bool:
        """True when reading ``source`` should give way."""
        with self._condition:
            return self._stopping or self._source is not source or self._detail is not None

    # ── Following ────────────────────────────────────────────────────────

    def _start_following(self, source: HistorySource | None) -> None:
        self._following = source
        self._position = None
        self._index = []
        self._gone_until = -np.inf
        self._stalled = False

    def _follow_once(self, source: HistorySource) -> bool:
        """Read what was recorded since the last call. True if anything was."""
        snapshot = self._recording(self._position, _SEGMENTS_PER_READ)
        if snapshot is None:
            return False
        try:
            if snapshot.is_empty:
                return False
            lost = snapshot.truncated
            self._forget_discarded(snapshot.oldest_segment)
            pending = _Pending(source.columns)
            handed_over = time.monotonic()
            for block in snapshot.blocks():
                self._index_block(block.where, block.records, source.origin)
                pending.add_records(block.records, source)
                self._position = SpoolPosition(block.where.segment, block.where.end)
                if pattern_stalled(source.parser):
                    self._stalled = True
                    self._deliver(HistoryBatch(
                        source.generation, pending.take(), pending.reached, lost, stalled=True,
                    ))
                    return True
                due = time.monotonic() - handed_over >= PLOT_HISTORY_HANDOVER_MS / MS_PER_SECOND
                if pending.lines >= PLOT_HISTORY_HANDOVER_LINES or due:
                    self._deliver(HistoryBatch(source.generation, pending.take(), pending.reached, lost))
                    lost = False
                    handed_over = time.monotonic()
                if self._changed(source):
                    break
            if pending.lines or lost:
                self._deliver(HistoryBatch(source.generation, pending.take(), pending.reached, lost))
            return True
        finally:
            snapshot.release()

    def _forget_discarded(self, oldest_segment: int | None) -> None:
        """Drop the index of blocks the recording no longer holds.

        The index then never outgrows the recording, however long the session.
        """
        if oldest_segment is None:
            return
        kept = 0
        while kept < len(self._index) and self._index[kept].where.segment < oldest_segment:
            kept += 1
        if kept:
            self._gone_until = max(
                self._gone_until, *(entry.latest for entry in self._index[:kept]),
            )
            del self._index[:kept]

    def _index_block(self, where: SpoolRange, records: list[LineRecord], origin: datetime) -> None:
        if records:
            times = [(record.timestamp - origin).total_seconds() for record in records]
            self._index.append(_Indexed(where, min(times), max(times)))

    # ── Detail ───────────────────────────────────────────────────────────

    def _read_detail(self, source: HistorySource, request: DetailRequest) -> None:
        wanted = [
            entry for entry in self._index
            if entry.latest >= request.low and entry.earliest <= request.high
        ]
        snapshot = self._recording(None, None)
        if snapshot is None:
            return
        try:
            collectors = {
                name: _Collector(request.low, request.high, request.pixels)
                for name in source.columns
            }
            # Samples at the time of the oldest one in memory, kept apart
            # until the range has been read and they can be split.
            boundary: dict[str, tuple[list[float], list[float]]] = {
                name: ([], []) for name in request.held
            }
            # Complete only if every block of the range is still held and
            # following has read as far as the range reaches.
            reach = min([request.high, *(at for at, _ in request.held.values())])
            read_up_to = max((entry.latest for entry in self._index), default=-np.inf)
            complete = (
                read_up_to >= reach
                and request.low > self._gone_until
                and all(snapshot.holds(entry.where) for entry in wanted)
            )
            lines = 0
            for block in snapshot.read(entry.where for entry in wanted):
                if self._changed(source) or pattern_stalled(source.parser):
                    return
                lines += len(block.records)
                if lines > PLOT_HISTORY_DETAIL_MAX_LINES:
                    complete = False
                    break
                found = _Pending(source.columns)
                found.add_records(block.records, source)
                for name, (x, y) in found.lists.items():
                    self._collect(name, x, y, request, collectors[name], boundary)
            for name, (x, y) in boundary.items():
                take = len(x) - request.held[name][1]
                if take > 0:
                    collectors[name].add(x[:take], y[:take])
        finally:
            snapshot.release()
        samples = {name: collector.samples() for name, collector in collectors.items()}
        figures = {
            name: found for name, collector in collectors.items()
            if (found := collector.figures()) is not None
        }
        self._deliver(DetailResult(
            request, samples, figures,
            every_sample=all(collector.every_sample for collector in collectors.values()),
            complete=complete,
        ))

    @staticmethod
    def _collect(
        name: str,
        x: list[float],
        y: list[float],
        request: DetailRequest,
        collector: _Collector,
        boundary: dict[str, tuple[list[float], list[float]]],
    ) -> None:
        held = request.held.get(name)
        keep_x: list[float] = []
        keep_y: list[float] = []
        for at, value in zip(x, y, strict=True):
            if at < request.low or at > request.high:
                continue
            if held is not None:
                if at > held[0]:
                    continue
                if at == held[0]:
                    boundary[name][0].append(at)
                    boundary[name][1].append(value)
                    continue
            keep_x.append(at)
            keep_y.append(value)
        collector.add(keep_x, keep_y)


class _Pending:
    """Samples found in device lines, value by value."""

    def __init__(self, columns: tuple[str, ...]) -> None:
        self.lists: dict[str, tuple[list[float], list[float]]] = {
            name: ([], []) for name in columns
        }
        self.lines = 0
        self.reached: float | None = None

    def add_records(self, records: list[LineRecord], source: HistorySource) -> None:
        extract = source.parser.extract
        origin = source.origin
        for record in records:
            if record.origin is not RecordOrigin.DEVICE:
                continue
            self.lines += 1
            at = (record.timestamp - origin).total_seconds()
            if self.reached is None or at > self.reached:
                self.reached = at
            values = extract(record.text[:PLOT_MAX_LINE_CHARACTERS])
            if not values or at < 0:
                continue
            for name, value in values.items():
                found = self.lists.get(name)
                if found is not None:
                    found[0].append(at)
                    found[1].append(value)

    def take(self) -> Samples:
        taken = {
            name: (np.asarray(x, dtype=float), np.asarray(y, dtype=float))
            for name, (x, y) in self.lists.items() if x
        }
        for x, y in self.lists.values():
            x.clear()
            y.clear()
        self.lines = 0
        return taken


def pattern_stalled(parser: object) -> bool:
    """True when a pattern has run out of time on too many lines in a row."""
    return getattr(parser, "consecutive_timeouts", 0) >= PLOT_REGEX_MAX_CONSECUTIVE_TIMEOUTS
