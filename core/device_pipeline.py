# SPDX-License-Identifier: GPL-3.0-or-later
"""The data pipeline of one device tab.

Three independent parts carry a session from the port to the disk and
to the screen:

    port --> reader thread --+--> session writer thread --> files on disk
                             +--> display buffer --> GUI, on its own timer

  Reader thread
      Owns the serial port. Cuts the byte stream into lines, stamps
      each line once at arrival, classifies it, and hands it to the
      writer and to the display buffer. It never waits for either.

  Session writer (``core.log_writer``)
      Writes the session spool and the log files on a thread of its own.

  Display buffer
      A bounded queue the GUI empties at its own pace. If the GUI falls
      behind, the oldest undisplayed lines are skipped in the view and
      counted. The log is unaffected.

``DevicePipeline`` is the only object a device tab talks to. It lives
on the GUI thread; every call either returns at once or is carried to
the reader thread by a queued signal.

Shutdown order (disconnect, tab close, application exit):
  1. Stop reading and close the port.
  2. Emit the final partial line, if any.
  3. The writer writes everything it was given and closes the log.
  4. Only then is the disconnect reported, with the log's real outcome.

This module does not touch GUI widgets.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from app.constants import (
    DEFAULT_LINE_ENDING,
    DISPLAY_MAX_CHARACTERS,
    HEX_IDLE_FLUSH_MS,
    SERIAL_ARRIVAL_SPREAD_MAX_MS,
    STATS_INTERVAL_MS,
    TAG_DATA,
    DisplayMode,
    LineEnding,
)
from core.export_job import ExportJob, ExportRequest, ExportResult
from core.framing import HexFramer, LineFramer, decode_line, escape_control_characters
from core.log_writer import LogProgress, LogRequest, LogStarted, LogSummary, SessionWriter
from core.records import LineRecord, command_record, notice_record
from core.tag_detector import TagDetector
from core.transport import Transport, open_serial_transport

logger = logging.getLogger(__name__)

#: Opens a transport on the calling thread. Returns ``(transport, "")``
#: or ``(None, reason)``.
TransportFactory = Callable[[str, int], tuple[Transport | None, str]]

_COMMAND_ENCODING = "utf-8"
_MS_PER_SECOND = 1000
_ARRIVAL_SPREAD_MAX = timedelta(milliseconds=SERIAL_ARRIVAL_SPREAD_MAX_MS)
_ONE_MICROSECOND = timedelta(microseconds=1)


def arrival_times(count: int, previous: datetime | None, now: datetime) -> list[datetime]:
    """Estimate when each of ``count`` lines of one read arrived.

    A read returns everything the port collected since the previous
    read, so its lines did not all arrive at the same instant. They are
    stamped evenly across the interval since the previous read, ending
    at ``now``. After a silence longer than the spread limit the lines
    belong to a fresh burst and all carry ``now``.

    The result never decreases and its last entry is always ``now``.
    """
    if count <= 0:
        return []
    if previous is None or count == 1 or not (
        timedelta(0) < now - previous <= _ARRIVAL_SPREAD_MAX
    ):
        return [now] * count
    # Whole microseconds, so rounding can never place a line after ``now``.
    gap = (now - previous) // _ONE_MICROSECOND
    return [
        previous + timedelta(microseconds=gap * index // count)
        for index in range(1, count + 1)
    ]


class DisplayBuffer:
    """Bounded hand-over of records from the reader to the GUI.

    Thread-safe. When the buffer is full the oldest records are
    discarded and counted, so memory stays bounded however far the GUI
    falls behind. The bound is on the number of records and on the
    characters they hold, whichever is reached first.
    """

    def __init__(self, capacity: int, max_characters: int = DISPLAY_MAX_CHARACTERS) -> None:
        self._capacity = capacity
        self._max_characters = max_characters
        self._records: deque[LineRecord] = deque()
        self._characters = 0
        self._skipped = 0
        self._lock = threading.Lock()

    def set_capacity(self, capacity: int) -> None:
        with self._lock:
            self._capacity = capacity
            self._trim()

    def put(self, records: list[LineRecord]) -> None:
        with self._lock:
            self._records.extend(records)
            self._characters += sum(len(record.text) for record in records)
            self._trim()

    def take(self, max_count: int) -> tuple[list[LineRecord], int]:
        """Remove and return up to ``max_count`` records, oldest first,
        together with how many were skipped since the previous call."""
        with self._lock:
            count = min(max_count, len(self._records))
            records = [self._records.popleft() for _ in range(count)]
            self._characters -= sum(len(record.text) for record in records)
            skipped, self._skipped = self._skipped, 0
        return records, skipped

    def keep_newest(self, count: int) -> None:
        """Discard all but the newest ``count`` records. The discarded
        ones are counted as skipped."""
        with self._lock:
            self._discard_oldest(len(self._records) - count)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self._characters = 0
            self._skipped = 0

    def _trim(self) -> None:
        """Discard the oldest records beyond the capacity, then as many
        more as it takes to come back under the character limit. One
        record always stays, however long it is."""
        self._discard_oldest(len(self._records) - self._capacity)
        while self._characters > self._max_characters and len(self._records) > 1:
            self._discard_oldest(1)

    def _discard_oldest(self, count: int) -> None:
        for _ in range(count):
            self._characters -= len(self._records.popleft().text)
        if count > 0:
            self._skipped += count


class _WriterSignals(QObject):
    """Turns the writer thread's reports into Qt signals."""

    log_write_failed = Signal(str)
    log_write_recovered = Signal()
    lines_dropped = Signal(int)
    spool_unavailable = Signal(str)
    writer_error = Signal(str)


class _WriterListener:
    """``WriterListener`` that forwards to ``_WriterSignals``."""

    def __init__(self, signals: _WriterSignals) -> None:
        self._signals = signals

    def log_write_failed(self, message: str) -> None:
        self._signals.log_write_failed.emit(message)

    def log_write_recovered(self) -> None:
        self._signals.log_write_recovered.emit()

    def lines_dropped(self, count: int) -> None:
        self._signals.lines_dropped.emit(count)

    def spool_unavailable(self, message: str) -> None:
        self._signals.spool_unavailable.emit(message)

    def writer_error(self, message: str) -> None:
        self._signals.writer_error.emit(message)


class _ReaderWorker(QObject):
    """Runs on the reader thread: port, framing, classification, hand-over."""

    opened = Signal(str, int)            # port, baud
    open_failed = Signal(str, str)       # port, reason
    closed = Signal(str, bool, object)   # port, unexpected, LogSummary | None
    transport_error = Signal(str)
    command_failed = Signal(str)
    stats = Signal(int, int)             # lines in the last second, total lines

    def __init__(
        self,
        writer: SessionWriter,
        display: DisplayBuffer,
        transport_factory: TransportFactory,
    ) -> None:
        super().__init__()
        self._writer = writer
        self._display = display
        self._transport_factory = transport_factory

        self._transport: Transport | None = None
        self._port_name = ""
        self._display_mode = DisplayMode.TEXT
        self._line_framer = LineFramer()
        self._hex_framer = HexFramer()
        self._lines_since_report = 0
        self._total_lines = 0
        self._last_read_time: datetime | None = None

        # Timers are created on the reader thread, in ``start``.
        self._stats_timer: QTimer | None = None
        self._hex_idle_timer: QTimer | None = None

        # Set by ``shutdown`` for the thread that is waiting on it.
        self.finished_shutdown = threading.Event()
        self.shutdown_summary: LogSummary | None = None

    # ── Slots (reader thread) ────────────────────────────────────────────

    @Slot()
    def start(self) -> None:
        """Create the objects that must live on the reader thread."""
        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(STATS_INTERVAL_MS)
        self._stats_timer.timeout.connect(self._report_stats)

        self._hex_idle_timer = QTimer(self)
        self._hex_idle_timer.setSingleShot(True)
        self._hex_idle_timer.setInterval(HEX_IDLE_FLUSH_MS)
        self._hex_idle_timer.timeout.connect(self._flush_hex_row)

    @Slot(str, int)
    def open(self, port: str, baud: int) -> None:
        if self._transport is not None:
            self._finish(unexpected=False)

        transport, reason = self._transport_factory(port, baud)
        if transport is None:
            # The recording of the previous session is left as it was,
            # so it can still be exported.
            self.open_failed.emit(port, reason)
            return

        # The new session is being recorded before the port's first
        # bytes are delivered: they reach this thread through its event
        # loop, which does not run until this method returns.
        self._writer.begin_session()
        self._display.clear()
        self._line_framer = LineFramer()
        self._hex_framer = HexFramer()
        self._lines_since_report = 0
        self._total_lines = 0
        self._last_read_time = None

        transport.data_received.connect(self._on_bytes)
        transport.link_lost.connect(self._on_link_lost)
        transport.error_occurred.connect(self.transport_error)
        self._transport = transport
        self._port_name = port
        self._stats_timer.start()
        self.opened.emit(port, baud)

    @Slot()
    def close(self) -> None:
        if self._transport is not None:
            self._finish(unexpected=False)

    @Slot()
    def shutdown(self) -> None:
        """Close the connection, if any, then confirm from this thread."""
        if self._transport is not None:
            self.shutdown_summary = self._finish(unexpected=False)
        self.finished_shutdown.set()

    @Slot(str, str)
    def send(self, command: str, line_ending: str) -> None:
        if self._transport is None:
            self.command_failed.emit("The device is not connected")
            return
        data = (command + line_ending).encode(_COMMAND_ENCODING)
        reason = self._transport.write(data)
        if reason:
            self.command_failed.emit(reason)
            return
        echo = command_record(escape_control_characters(command), self._stamp_now())
        self._deliver([echo], count_as_received=False)

    @Slot(bool)
    def set_log_paused(self, paused: bool) -> None:
        self._writer.set_paused(paused, self._stamp_now())

    def _stamp_now(self) -> datetime:
        """Return the current time for a line this thread adds itself.

        The time is remembered as the latest stamp issued, so the lines
        of the next read are not spread back across it and timestamps
        never run backwards.
        """
        now = datetime.now()
        self._last_read_time = now
        return now

    @Slot(object)
    def set_display_mode(self, mode: DisplayMode) -> None:
        if mode is self._display_mode:
            return
        # What the outgoing mode still holds is shown before the switch,
        # so no received byte is dropped by changing mode.
        self._deliver(self._flush_framer(datetime.now()))
        self._display_mode = mode

    # ── Data Path ────────────────────────────────────────────────────────

    @Slot(bytes)
    def _on_bytes(self, data: bytes) -> None:
        now = datetime.now()
        try:
            if self._display_mode is DisplayMode.HEX:
                texts = self._hex_framer.feed(data)
                self._hex_idle_timer.start()
            else:
                texts = self._decode(self._line_framer.feed(data))
            records = self._records(texts, now)
        except Exception:
            # Last resort. These bytes cannot be turned into lines, but
            # the session says so where they would have been.
            logger.exception("Could not process %d received bytes", len(data))
            records = [notice_record(
                f"{len(data):,} received bytes could not be processed: internal error",
                self._stamp_now(),
            )]
        self._deliver(records)

    @staticmethod
    def _decode(lines: list[bytes]) -> list[str]:
        return [text for raw in lines if (text := decode_line(raw)) is not None]

    def _records(self, texts: list[str], now: datetime) -> list[LineRecord]:
        """Stamp and classify the texts of one read."""
        times = arrival_times(len(texts), self._last_read_time, now)
        self._last_read_time = now
        if self._display_mode is DisplayMode.HEX:
            return [
                LineRecord(when, text, TAG_DATA, hex_row=True)
                for when, text in zip(times, texts, strict=True)
            ]
        return [
            LineRecord(when, text, TagDetector.detect(text))
            for when, text in zip(times, texts, strict=True)
        ]

    def _flush_framer(self, now: datetime) -> list[LineRecord]:
        """Return what the active framer holds as final records."""
        if self._display_mode is DisplayMode.HEX:
            self._hex_idle_timer.stop()
            texts = self._hex_framer.flush()
        else:
            texts = self._decode(self._line_framer.flush())
        return self._records(texts, now)

    @Slot()
    def _flush_hex_row(self) -> None:
        """Show an incomplete hex row once the stream has gone quiet."""
        self._deliver(self._flush_framer(datetime.now()))

    def _deliver(self, records: list[LineRecord], count_as_received: bool = True) -> None:
        if not records:
            return
        self._writer.submit(records)
        self._display.put(records)
        if count_as_received:
            self._lines_since_report += len(records)
            self._total_lines += len(records)

    @Slot()
    def _report_stats(self) -> None:
        rate = self._lines_since_report * _MS_PER_SECOND // STATS_INTERVAL_MS
        self._lines_since_report = 0
        self.stats.emit(rate, self._total_lines)

    # ── Ending a Connection ──────────────────────────────────────────────

    @Slot(str)
    def _on_link_lost(self, message: str) -> None:
        logger.warning("Connection to %s lost: %s", self._port_name, message)
        if self._transport is not None:
            self._finish(unexpected=True)

    def _finish(self, unexpected: bool) -> LogSummary | None:
        """Run the shutdown order for the current connection.

        Returns the summary of the log that was running, if any.
        """
        transport, self._transport = self._transport, None
        port_name, self._port_name = self._port_name, ""
        self._stats_timer.stop()

        # Closing delivers any bytes the transport has already read, so
        # it is done while the data connection is still in place.
        transport.close()
        transport.data_received.disconnect(self._on_bytes)
        transport.link_lost.disconnect(self._on_link_lost)
        transport.error_occurred.disconnect(self.transport_error)
        transport.deleteLater()

        self._deliver(self._flush_framer(datetime.now()))
        summary = self._writer.end_session()
        self.stats.emit(0, self._total_lines)
        self.closed.emit(port_name, unexpected, summary)
        return summary


class DevicePipeline(QObject):
    """The data pipeline of one device tab. Lives on the GUI thread.

    Signals:
        connected(str, int):            Port opened: port, baud.
        connect_failed(str, str):       Port could not be opened: port, reason.
        disconnected(str, bool, object): Connection ended and everything
                                        is on disk: port, whether it was
                                        unexpected, ``LogSummary`` or None.
        transport_error(str):           A serial error that did not end
                                        the connection.
        command_failed(str):            A command could not be sent.
        stats_updated(int, int):        Lines per second, total lines.
        log_write_failed(str):          A log file cannot be written.
        log_write_recovered():          Log writing works again.
        lines_dropped(int):             Lines the disk could not keep up with.
        spool_unavailable(str):         The session cannot be recorded
                                        for Export.
        writer_error(str):              An unexpected error while
                                        recording; recording carries on.
        export_finished(object):        ``ExportResult`` of an export.

    Args:
        spool_root: Directory that holds session spools.
        display_capacity: Lines the display buffer may hold.
        max_pending_lines: Lines the writer's queue may hold.
        flush_interval_ms: The writer's flush interval.
        transport_factory: Opens the port; replaceable for tests.
    """

    connected = Signal(str, int)
    connect_failed = Signal(str, str)
    disconnected = Signal(str, bool, object)
    transport_error = Signal(str)
    command_failed = Signal(str)
    stats_updated = Signal(int, int)
    log_write_failed = Signal(str)
    log_write_recovered = Signal()
    lines_dropped = Signal(int)
    spool_unavailable = Signal(str)
    writer_error = Signal(str)
    export_finished = Signal(object)

    # Requests carried to the reader thread.
    _start_requested = Signal()
    _open_requested = Signal(str, int)
    _close_requested = Signal()
    _shutdown_requested = Signal()
    _send_requested = Signal(str, str)
    _display_mode_requested = Signal(object)
    _log_pause_requested = Signal(bool)

    # Raised on the export thread; handled on the GUI thread.
    _export_done = Signal(object)

    def __init__(
        self,
        spool_root: str,
        display_capacity: int,
        max_pending_lines: int,
        flush_interval_ms: int,
        transport_factory: TransportFactory = open_serial_transport,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._display = DisplayBuffer(display_capacity)

        self._writer_signals = _WriterSignals(self)
        self._writer_signals.log_write_failed.connect(self.log_write_failed)
        self._writer_signals.log_write_recovered.connect(self.log_write_recovered)
        self._writer_signals.lines_dropped.connect(self.lines_dropped)
        self._writer_signals.spool_unavailable.connect(self.spool_unavailable)
        self._writer_signals.writer_error.connect(self.writer_error)
        self._writer = SessionWriter(
            _WriterListener(self._writer_signals),
            spool_root=spool_root,
            max_pending_lines=max_pending_lines,
            flush_interval_ms=flush_interval_ms,
        )

        self._worker = _ReaderWorker(self._writer, self._display, transport_factory)
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)

        self._start_requested.connect(self._worker.start)
        self._open_requested.connect(self._worker.open)
        self._close_requested.connect(self._worker.close)
        self._shutdown_requested.connect(self._worker.shutdown)
        self._send_requested.connect(self._worker.send)
        self._display_mode_requested.connect(self._worker.set_display_mode)
        self._log_pause_requested.connect(self._worker.set_log_paused)

        self._worker.opened.connect(self.connected)
        self._worker.open_failed.connect(self.connect_failed)
        self._worker.closed.connect(self.disconnected)
        self._worker.transport_error.connect(self.transport_error)
        self._worker.command_failed.connect(self.command_failed)
        self._worker.stats.connect(self.stats_updated)
        self._export_done.connect(self._on_export_done)

        self._export: ExportJob | None = None

    # ── Connection ───────────────────────────────────────────────────────

    def open(self, port: str, baud: int) -> None:
        """Open a port. Answered by ``connected`` or ``connect_failed``."""
        if not self._thread.isRunning():
            self._thread.start()
            self._start_requested.emit()
        self._open_requested.emit(port, baud)

    def close(self) -> None:
        """End the connection. Answered by ``disconnected`` once the
        session is on disk."""
        self._close_requested.emit()

    def send_command(
        self, command: str, line_ending: LineEnding = DEFAULT_LINE_ENDING,
    ) -> None:
        """Send a command to the device, followed by ``line_ending``, and
        echo it into the session."""
        self._send_requested.emit(command, line_ending.value)

    def set_display_mode(self, mode: DisplayMode) -> None:
        self._display_mode_requested.emit(mode)

    # ── Display ──────────────────────────────────────────────────────────

    def take_display(self, max_count: int) -> tuple[list[LineRecord], int]:
        """Collect received lines for the console (see ``DisplayBuffer.take``)."""
        return self._display.take(max_count)

    def limit_display_backlog(self, count: int) -> None:
        """Skip the console ahead to the newest ``count`` waiting lines."""
        self._display.keep_newest(count)

    def discard_display(self) -> None:
        """Forget the lines still waiting for the console."""
        self._display.clear()

    # ── Logging ──────────────────────────────────────────────────────────

    def start_log(self, request: LogRequest) -> LogStarted:
        """Start logging. Raises ``LogStartError`` if the files cannot be created."""
        return self._writer.start_log(request)

    def stop_log(self) -> LogSummary | None:
        """Write what has been received so far, then close the log."""
        return self._writer.stop_log()

    def set_log_paused(self, paused: bool) -> None:
        """Pause or resume the log. The reader stamps the moment, like
        every other line of the session."""
        self._log_pause_requested.emit(paused)

    def log_progress(self) -> LogProgress | None:
        """Which part the log is writing and how large it is, or None
        when no log is running."""
        return self._writer.log_progress()

    # ── Export ───────────────────────────────────────────────────────────

    @property
    def is_exporting(self) -> bool:
        return self._export is not None

    def mark_export_start(self) -> None:
        """Make later exports begin with the lines received from now on."""
        self._writer.mark_export_start()

    def export(self, request: ExportRequest) -> bool:
        """Save the recorded session in the background.

        Answered by ``export_finished``. Returns False if there is
        nothing recorded to export or an export is already running.
        """
        if self._export is not None:
            return False
        snapshot = self._writer.snapshot()
        if snapshot is None:
            return False
        if snapshot.is_empty:
            snapshot.release()
            return False
        self._export = ExportJob(snapshot, request, self._report_export_done)
        self._export.start()
        return True

    def _report_export_done(self, result: ExportResult) -> None:
        """Called on the export thread when it has finished."""
        self._export_done.emit(result)

    @Slot(object)
    def _on_export_done(self, result: ExportResult) -> None:
        if self._export is not None:
            self._export.join()
            self._export = None
        self.export_finished.emit(result)

    # ── Settings and Lifetime ────────────────────────────────────────────

    def configure(
        self, display_capacity: int, max_pending_lines: int, flush_interval_ms: int,
    ) -> None:
        """Apply changed preferences."""
        self._display.set_capacity(display_capacity)
        self._writer.configure(max_pending_lines, flush_interval_ms)

    def shutdown(self) -> LogSummary | None:
        """Close everything and wait until the session is on disk.

        Used when the tab or the application closes. Returns the
        summary of the log that was running, if any. No signal is
        emitted afterwards.
        """
        if self._export is not None:
            self._export.cancel()
            self._export.join()
            self._export = None
        if self._thread.isRunning():
            self._shutdown_requested.emit()
            self._worker.finished_shutdown.wait()
            self._thread.quit()
            self._thread.wait()
        # Reports the reader queued before it stopped are still to be
        # delivered to this thread; they must not reach a tab that is
        # going away.
        self.blockSignals(True)
        summary = self._worker.shutdown_summary
        self._worker.shutdown_summary = None
        return self._writer.shutdown() or summary
