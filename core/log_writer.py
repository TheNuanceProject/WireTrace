# SPDX-License-Identifier: GPL-3.0-or-later
"""The thread that writes a device session to disk.

The reader hands every record of a session to ``SessionWriter.submit``
and returns at once. A dedicated thread then writes them to:

  the session spool   always, so Export can save the whole session
  the ``.txt`` log    while logging is on
  the ``.csv`` log    while logging is on with CSV selected

Because the reader never waits for this thread while data is flowing, a
slow or failing disk cannot delay reading the serial port.

What waits and what does not:
  ``submit``, ``set_paused`` and ``mark_export_start`` return at once.
  ``start_log``, ``stop_log``, ``snapshot``, ``end_session`` and
  ``shutdown`` return a result, so they wait until the writer has
  handled everything queued before them.

Ordering:
  Records and control requests (start a log, stop it, pause, take a
  snapshot) travel through one queue and are handled in the order they
  were made. A log therefore begins exactly at the record that follows
  the request, and stopping a log first writes everything submitted
  before the stop.

Nothing is lost silently:
  - If records arrive faster than the disk accepts them for long enough
    to fill the queue, the excess is counted, reported to the listener,
    and recorded in the log as a notice line.
  - If a write fails, the affected file keeps its unwritten bytes and
    retries (see ``core.log_files``). The listener is told when a
    failure starts and when it ends.
  - An error nobody anticipated is logged with its traceback and
    reported to the listener; the thread carries on with the next item.

Timestamps:
  Every line this module adds to a log carries the timestamp of the
  record it stands beside, never the time it happened to be written,
  so the timestamps in a file never run backwards.

This module contains no Qt code, so it is tested with real threads and
real files.

This module does not touch the GUI or serial I/O.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, TypeVar

from app.constants import (
    CSV_MAX_ROWS_PER_PART,
    LOG_FLUSH_THRESHOLD_BYTES,
    LOG_FSYNC_INTERVAL_MS,
    LOG_PENDING_MAX_CHARACTERS,
)
from core.csv_engine import CSVEngine
from core.log_files import FileBackend, SplitLogFile
from core.log_format import (
    DEFAULT_FILE_LINE_ENDING,
    FileHeaderInfo,
    csv_header,
    encode_csv_line,
    encode_text_line,
    text_header,
)
from core.records import (
    NOTICE_PREFIX,
    LineRecord,
    RecordOrigin,
    format_timestamp,
    notice_record,
)
from core.session_spool import SessionSpool, SpoolSnapshot

logger = logging.getLogger(__name__)

_MS_PER_SECOND = 1000.0

_T = TypeVar("_T")

_LOG_TITLE = "Log Session"
_LOG_KIND = "Session"
_LOG_TIME_LABEL = "Started"
_UNTITLED_SESSION = "Untitled"
_THREAD_NAME = "wiretrace-session-writer"


class LogStartError(Exception):
    """A log could not be started."""


@dataclass(frozen=True, slots=True)
class LogRequest:
    """What the user asked for when turning logging on.

    Attributes:
        text_path: Path of the ``.txt`` log.
        csv_path: Path of the ``.csv`` log, or None for text only.
        session_name: Name the user gave the session.
        port_name: Serial port being logged.
        baud_rate: Baud rate of the connection.
        description: Free-text description, may be empty.
        split_bytes: Size at which each file starts a new part (0 = never).
        line_ending: The characters that end each line of the files.
    """

    text_path: str
    csv_path: str | None
    session_name: str
    port_name: str
    baud_rate: int
    description: str
    split_bytes: int
    line_ending: str = DEFAULT_FILE_LINE_ENDING


@dataclass(frozen=True, slots=True)
class LogStarted:
    """The files a started log is writing to. The names can differ from
    the request when a requested name was already taken."""

    text_path: str
    csv_path: str | None


@dataclass(frozen=True, slots=True)
class LogProgress:
    """Where a running log stands.

    Attributes:
        part_number: Part of the ``.txt`` log being written, from 1.
        part_bytes: Size of that part so far.
        split_bytes: Size at which a new part starts (0 = never).
    """

    part_number: int
    part_bytes: int
    split_bytes: int


@dataclass(frozen=True, slots=True)
class LogSummary:
    """The outcome of a log, reported when it stops.

    Attributes:
        text_paths: Every ``.txt`` part written, in order.
        csv_paths: Every ``.csv`` part written, in order.
        lines_written: Session lines written to the ``.txt`` log.
        lines_lost: Session lines that could not be written.
        csv_rows_lost: Rows that could not be written to the ``.csv`` log.
        error: Why closing the files failed, or empty if it did not.
    """

    text_paths: tuple[str, ...]
    csv_paths: tuple[str, ...]
    lines_written: int
    lines_lost: int
    csv_rows_lost: int
    error: str


class WriterListener(Protocol):
    """Receives the writer's reports. Called on the writer thread."""

    def log_write_failed(self, message: str) -> None:
        """A log file could not be written; the writer keeps retrying."""

    def log_write_recovered(self) -> None:
        """Writing works again after a failure."""

    def lines_dropped(self, count: int) -> None:
        """Lines arrived faster than they could be queued and were dropped."""

    def spool_unavailable(self, message: str) -> None:
        """The session spool cannot be written; Export will be incomplete."""

    def writer_error(self, message: str) -> None:
        """An unexpected error occurred; the writer carries on."""


@dataclass(slots=True)
class _Call:
    """A request to run a function on the writer thread.

    ``done`` is set once it has run, for a caller waiting on the
    result. A call nobody waits for has ``awaited`` False.
    """

    function: Callable[[], object]
    done: threading.Event
    awaited: bool = True
    result: object = None
    error: BaseException | None = None


@dataclass(slots=True)
class _Dropped:
    """Queue entry standing in for lines that did not fit in the queue.

    ``timestamp`` is that of the first line dropped.
    """

    count: int
    timestamp: datetime


class _ActiveLog:
    """The open files of one log and the state that goes with them."""

    def __init__(
        self,
        request: LogRequest,
        started: datetime,
        backend: FileBackend | None,
    ) -> None:
        info = FileHeaderInfo(
            title=_LOG_TITLE,
            kind=_LOG_KIND,
            name=request.session_name or _UNTITLED_SESSION,
            time_label=_LOG_TIME_LABEL,
            time=started,
            port_name=request.port_name,
            baud_rate=request.baud_rate,
            description=request.description,
        )
        self.paused = False
        self.split_bytes = request.split_bytes
        self.line_ending = line_ending = request.line_ending
        self.lines_dropped = 0
        self.csv_failed = False
        self.record_error_reported = False
        self.csv_engine: CSVEngine | None = None
        self.csv_file: SplitLogFile | None = None
        # Timestamp of the record most recently handed to the files.
        self.last_timestamp = started

        self.text_file = SplitLogFile(
            request.text_path,
            header=lambda part: text_header(info, part, line_ending),
            loss_marker=lambda count: _text_loss_marker(count, self.last_timestamp, line_ending),
            part_limit_bytes=request.split_bytes,
            backend=backend,
        )
        if request.csv_path is not None:
            engine = CSVEngine()
            self.csv_engine = engine
            self.csv_file = SplitLogFile(
                request.csv_path,
                header=lambda part: csv_header(info, part, engine.header, line_ending),
                loss_marker=lambda count: _csv_loss_marker(count, line_ending),
                part_limit_bytes=request.split_bytes,
                part_limit_units=CSV_MAX_ROWS_PER_PART if request.split_bytes else 0,
                eager_header=False,
                backend=backend,
            )

    @property
    def files(self) -> list[SplitLogFile]:
        return [f for f in (self.text_file, self.csv_file) if f is not None]


class SessionWriter:
    """Writes the records of a device session on a thread of its own.

    Args:
        listener: Receives failure and recovery reports.
        spool_root: Directory that holds session spools.
        max_pending_lines: Lines the queue may hold before new ones
            are dropped and reported.
        flush_interval_ms: Longest time a written line stays in memory
            before it is handed to the operating system.
        backend: File-system operations; replaceable for tests.
    """

    def __init__(
        self,
        listener: WriterListener,
        spool_root: str,
        max_pending_lines: int,
        flush_interval_ms: int,
        backend: FileBackend | None = None,
    ) -> None:
        self._listener = listener
        self._spool_root = spool_root
        self._max_pending_lines = max_pending_lines
        self._flush_interval = flush_interval_ms / _MS_PER_SECOND
        self._sync_interval = LOG_FSYNC_INTERVAL_MS / _MS_PER_SECOND
        self._backend = backend

        self._condition = threading.Condition()
        self._items: deque[list[LineRecord] | _Call | _Dropped] = deque()
        self._pending_lines = 0
        self._pending_characters = 0
        self._stopping = False
        self._thread: threading.Thread | None = None

        # Owned by the writer thread.
        self._session_active = False
        self._spool: SessionSpool | None = None
        self._log: _ActiveLog | None = None
        self._failure_reported = False
        # Written by the writer thread, read by any thread.
        self._progress: LogProgress | None = None

    # ── Public API (any thread) ──────────────────────────────────────────

    def configure(self, max_pending_lines: int, flush_interval_ms: int) -> None:
        """Apply changed preferences."""
        with self._condition:
            self._max_pending_lines = max_pending_lines
            self._flush_interval = flush_interval_ms / _MS_PER_SECOND
            self._condition.notify()

    def begin_session(self) -> None:
        """Start recording a new session, discarding the previous spool."""
        self._ensure_thread()
        self._call(self._begin_session)

    def submit(self, records: list[LineRecord]) -> None:
        """Queue records for writing. Never blocks.

        Records beyond the queue limit, counted in lines and in
        characters, are dropped. The drop keeps its place in the queue,
        so it is reported and recorded exactly where the missing lines
        would have been. An empty queue accepts a
        batch of any size: the limit exists to bound a backlog, and
        there is none.
        """
        if not records:
            return
        with self._condition:
            characters = sum(len(record.text) for record in records)
            fits = (
                self._pending_lines + len(records) <= self._max_pending_lines
                and self._pending_characters + characters <= LOG_PENDING_MAX_CHARACTERS
            )
            if fits or self._pending_lines == 0:
                self._items.append(records)
                self._pending_lines += len(records)
                self._pending_characters += characters
            elif self._items and isinstance(self._items[-1], _Dropped):
                self._items[-1].count += len(records)
            else:
                self._items.append(_Dropped(len(records), records[0].timestamp))
            self._condition.notify()

    def start_log(self, request: LogRequest) -> LogStarted:
        """Open the log files. Records submitted after this are logged.

        Raises:
            LogStartError: No session is being recorded, a log is
                already running, or a file could not be created.
        """
        return self._call(lambda: self._start_log(request))

    def stop_log(self) -> LogSummary | None:
        """Write everything submitted so far, then close the log files.

        Returns None if no log was running.
        """
        return self._call(self._stop_log)

    def log_progress(self) -> LogProgress | None:
        """Where the running log stands, or None when no log is running.

        Never waits for the writer thread: it returns the figures the
        writer last published.
        """
        return self._progress

    def set_paused(self, paused: bool, moment: datetime) -> None:
        """Pause or resume the log at ``moment``. The session spool
        keeps recording. Does not wait."""
        self._post(lambda: self._set_paused(paused, moment))

    def mark_export_start(self) -> None:
        """Make later exports start from this point of the session.
        Does not wait."""
        self._post(self._mark_export_start)

    def snapshot(self) -> SpoolSnapshot | None:
        """Capture the session recorded so far, for an export.

        Returns None if no session is being recorded.
        """
        return self._call(self._snapshot)

    def end_session(self) -> LogSummary | None:
        """Stop the log, if any, and stop recording. The spool stays
        readable until the next session begins or the writer shuts down."""
        return self._call(self._end_session)

    def shutdown(self) -> LogSummary | None:
        """End the session, delete the spool, and stop the thread."""
        if self._thread is None:
            return None
        summary = self._call(self._shutdown_session)
        with self._condition:
            self._stopping = True
            self._condition.notify()
        self._thread.join()
        self._thread = None
        return summary

    # ── Thread ───────────────────────────────────────────────────────────

    def _ensure_thread(self) -> None:
        if self._thread is None:
            self._stopping = False
            self._thread = threading.Thread(
                target=self._run, name=_THREAD_NAME, daemon=True,
            )
            self._thread.start()

    def _call(self, function: Callable[[], _T]) -> _T:
        """Run ``function`` on the writer thread, after everything
        already queued, and return its result."""
        if self._thread is None:
            return function()
        call = _Call(function, threading.Event())
        with self._condition:
            self._items.append(call)
            self._condition.notify()
        call.done.wait()
        if call.error is not None:
            raise call.error
        return call.result  # type: ignore[return-value]

    def _post(self, function: Callable[[], object]) -> None:
        """Run ``function`` on the writer thread, after everything
        already queued, without waiting for it."""
        if self._thread is None:
            function()
            return
        with self._condition:
            self._items.append(_Call(function, threading.Event(), awaited=False))
            self._condition.notify()

    def _run(self) -> None:
        next_flush = time.monotonic() + self._flush_interval
        next_sync = time.monotonic() + self._sync_interval
        while True:
            with self._condition:
                while not self._items and not self._stopping:
                    # With no log open there is nothing to flush on a
                    # schedule, so the thread sleeps until it is needed.
                    timeout = None
                    if self._log is not None:
                        timeout = max(0.0, next_flush - time.monotonic())
                        if timeout == 0.0:
                            break
                    self._condition.wait(timeout)
                items = self._items
                self._items = deque()
                self._pending_lines = 0
                self._pending_characters = 0
                stopping = self._stopping

            for item in items:
                self._handle(item)

            # The next due time is set before the attempt, so a step
            # that fails is tried again on schedule, not in a tight loop.
            now = time.monotonic()
            try:
                if (
                    now >= next_flush
                    or self._queued_log_bytes() >= LOG_FLUSH_THRESHOLD_BYTES
                ):
                    next_flush = now + self._flush_interval
                    self._flush_log()
                if now >= next_sync:
                    next_sync = now + self._sync_interval
                    self._sync_log()
            except Exception as exc:
                self._report_unexpected("writing the log", exc)

            if stopping and not items:
                return

    def _handle(self, item: list[LineRecord] | _Call | _Dropped) -> None:
        """Process one queue item. Nothing raised here may end the
        thread: everything still queued, and every caller waiting on a
        result, depends on it."""
        if isinstance(item, _Call):
            self._run_call(item)
            return
        try:
            if isinstance(item, _Dropped):
                self._record_dropped(item)
            else:
                self._write_records(item)
        except Exception as exc:
            self._report_unexpected("recording received lines", exc)

    def _run_call(self, call: _Call) -> None:
        try:
            call.result = call.function()
        except BaseException as exc:
            if call.awaited:
                call.error = exc
            elif isinstance(exc, Exception):
                self._report_unexpected("handling a request", exc)
            else:
                raise
        finally:
            call.done.set()

    def _report_unexpected(self, activity: str, error: Exception) -> None:
        logger.exception("Unexpected error while %s", activity)
        self._listener.writer_error(
            f"Unexpected error while {activity}: {type(error).__name__}: {error}",
        )

    # ── Writer-thread operations ─────────────────────────────────────────

    def _begin_session(self) -> None:
        self._end_session()
        self._delete_spool()
        self._session_active = True
        spool = SessionSpool(self._spool_root, backend=self._backend)
        try:
            spool.create()
        except OSError as exc:
            spool.delete()
            self._report_spool_unavailable(exc)
            return
        self._spool = spool

    def _write_records(self, records: list[LineRecord]) -> None:
        if self._spool is not None:
            try:
                self._spool.append(records)
            except OSError as exc:
                self._delete_spool()
                self._report_spool_unavailable(exc)

        log = self._log
        if log is None or log.paused:
            return
        self._log_records(log, records)
        self._publish_progress()

    def _log_records(self, log: _ActiveLog, records: list[LineRecord]) -> None:
        for record in records:
            try:
                self._log_record(log, record)
            except Exception as exc:
                # One line that cannot be logged costs that line, which
                # is counted as lost, and never the lines around it.
                log.lines_dropped += 1
                if not log.record_error_reported:
                    log.record_error_reported = True
                    self._report_unexpected("writing a line to the log", exc)

    def _log_record(self, log: _ActiveLog, record: LineRecord) -> None:
        log.last_timestamp = record.timestamp
        counted = record.origin is not RecordOrigin.NOTICE
        log.text_file.append(encode_text_line(record, log.line_ending), units=int(counted))
        if record.origin is RecordOrigin.DEVICE and not log.csv_failed:
            self._feed_csv(log, record)

    def _feed_csv(self, log: _ActiveLog, record: LineRecord) -> None:
        """Give one line to the CSV engine and queue the rows it returns.

        The ``.txt`` log is the complete record. An error in building
        the ``.csv`` must not cost it a line, so the CSV is closed off
        with what it has and the text log carries on.
        """
        if log.csv_engine is None:
            return
        try:
            rows = log.csv_engine.feed(format_timestamp(record.timestamp), record.text)
        except Exception as exc:
            log.csv_failed = True
            self._report_unexpected("building the CSV log", exc)
            return
        self._append_csv_rows(log, rows)

    @staticmethod
    def _append_csv_rows(log: _ActiveLog, rows: list[str]) -> None:
        if log.csv_file is not None:
            for row in rows:
                log.csv_file.append(encode_csv_line(row, log.line_ending))

    def _record_dropped(self, dropped: _Dropped) -> None:
        """Report dropped lines and leave a trace of them in the session."""
        self._listener.lines_dropped(dropped.count)
        if self._log is not None and not self._log.paused:
            self._log.lines_dropped += dropped.count
        self._write_records([
            notice_record(
                f"{dropped.count:,} lines were not recorded: the disk could not keep up",
                dropped.timestamp,
            ),
        ])

    def _start_log(self, request: LogRequest) -> LogStarted:
        if not self._session_active:
            raise LogStartError("The device is not connected.")
        if self._log is not None:
            raise LogStartError("A log is already running.")
        log = _ActiveLog(request, datetime.now(), self._backend)
        try:
            for log_file, path in (
                (log.text_file, request.text_path),
                (log.csv_file, request.csv_path),
            ):
                if log_file is not None:
                    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                    log_file.open()
        except OSError as exc:
            self._discard(log)
            raise LogStartError(str(exc)) from exc
        self._log = log
        self._failure_reported = False
        self._publish_progress()
        return LogStarted(
            text_path=log.text_file.paths[0],
            csv_path=log.csv_file.paths[0] if log.csv_file is not None else None,
        )

    @staticmethod
    def _discard(log: _ActiveLog) -> None:
        """Close and remove the files of a log that failed to start."""
        for log_file in log.files:
            with contextlib.suppress(OSError):
                log_file.close()
            for path in log_file.paths:
                with contextlib.suppress(OSError):
                    os.remove(path)

    def _stop_log(self) -> LogSummary | None:
        log, self._log = self._log, None
        self._publish_progress()
        if log is None:
            return None
        if log.csv_engine is not None and not log.csv_failed:
            self._append_csv_rows(log, log.csv_engine.finalize())

        errors: list[str] = []
        for log_file in log.files:
            try:
                log_file.close()
            except OSError as exc:
                errors.append(str(exc))
        self._failure_reported = False
        return LogSummary(
            text_paths=tuple(log.text_file.paths),
            csv_paths=tuple(log.csv_file.paths) if log.csv_file is not None else (),
            lines_written=log.text_file.units_written,
            lines_lost=log.text_file.units_lost + log.lines_dropped,
            csv_rows_lost=log.csv_file.units_lost if log.csv_file is not None else 0,
            error="; ".join(errors),
        )

    def _publish_progress(self) -> None:
        """Replace the progress figures other threads read.

        One assignment of a complete object, so a reader never sees a
        part number and a size that do not belong together.
        """
        log = self._log
        self._progress = None if log is None else LogProgress(
            part_number=log.text_file.appending_part,
            part_bytes=log.text_file.appending_part_bytes,
            split_bytes=log.split_bytes,
        )

    def _set_paused(self, paused: bool, moment: datetime) -> None:
        log = self._log
        if log is None or log.paused == paused:
            return
        log.paused = paused
        log.last_timestamp = moment
        message = "Logging paused" if paused else "Logging resumed"
        log.text_file.append(
            encode_text_line(notice_record(message, moment), log.line_ending), units=0,
        )

    def _mark_export_start(self) -> None:
        if self._spool is not None:
            self._spool.mark()

    def _snapshot(self) -> SpoolSnapshot | None:
        return self._spool.snapshot() if self._spool is not None else None

    def _end_session(self) -> LogSummary | None:
        self._session_active = False
        summary = self._stop_log()
        if self._spool is not None:
            self._spool.close()
        return summary

    def _shutdown_session(self) -> LogSummary | None:
        summary = self._end_session()
        self._delete_spool()
        return summary

    def _delete_spool(self) -> None:
        if self._spool is not None:
            spool, self._spool = self._spool, None
            spool.delete()

    def _report_spool_unavailable(self, error: OSError) -> None:
        logger.warning("Session spool unavailable: %s", error)
        self._listener.spool_unavailable(str(error))

    # ── Flushing ─────────────────────────────────────────────────────────

    def _queued_log_bytes(self) -> int:
        if self._log is None:
            return 0
        return sum(log_file.queued_bytes for log_file in self._log.files)

    def _flush_log(self) -> None:
        if self._log is None:
            return
        failure: OSError | None = None
        for log_file in self._log.files:
            try:
                log_file.flush()
            except OSError as exc:
                failure = exc
        self._report_write_state(failure)

    def _sync_log(self) -> None:
        if self._log is None:
            return
        for log_file in self._log.files:
            if log_file.is_failing:
                continue
            try:
                log_file.sync()
            except OSError as exc:
                logger.warning("Log sync failed: %s", exc)

    def _report_write_state(self, failure: OSError | None) -> None:
        """Tell the listener when a failure begins and when it ends."""
        if failure is not None and not self._failure_reported:
            self._failure_reported = True
            logger.error("Log write failed: %s", failure)
            self._listener.log_write_failed(str(failure))
        elif failure is None and self._failure_reported:
            self._failure_reported = False
            logger.info("Log writing recovered")
            self._listener.log_write_recovered()


# ── Loss Markers ─────────────────────────────────────────────────────────

def _text_loss_marker(count: int, timestamp: datetime, line_ending: str) -> bytes:
    return encode_text_line(
        notice_record(f"{count:,} lines were not written: the disk was unavailable", timestamp),
        line_ending,
    )


def _csv_loss_marker(count: int, line_ending: str) -> bytes:
    return encode_csv_line(
        f"# {NOTICE_PREFIX} {count:,} rows were not written: the disk was unavailable",
        line_ending,
    )
