# SPDX-License-Identifier: MIT
"""WireTrace buffered disk log writer.

Runs in a dedicated QThread. Receives log entries via enqueue(), buffers
them in a collections.deque, and flushes to disk on three triggers:
  1. Buffer reaches LOG_BUFFER_FLUSH_THRESHOLD (5,000 entries)
  2. Timer fires every LOG_FLUSH_INTERVAL_MS (1 second)
  3. stop_logging() is called (guaranteed final flush)

Buffer architecture (spec section 5.3):
  - collections.deque(maxlen=50,000) — automatic O(1) overflow protection
  - Atomic buffer swap: old, self._buffer = self._buffer, deque(maxlen=N)
  - File writes use buffering=65536 (64KB OS buffer)
  - fsync() on stop_logging() AND every PERIODIC_FSYNC_INTERVAL_MS so
    that worst-case data loss on a hard kill (power cut, kernel panic,
    Task Manager force-quit) is bounded to that interval rather than
    the entire session.

CRITICAL RULE: LogEngine receives ALL lines. Filtering only affects
the console display. The disk log is always complete.

This module does NOT touch: GUI or serial I/O.
"""

from __future__ import annotations

import logging
import os
import platform
from collections import deque
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import QMutex, QMutexLocker, QThread, QTimer, Signal

from app.constants import (
    FILE_WRITE_BUFFER_SIZE,
    LOG_BUFFER_FLUSH_THRESHOLD,
    LOG_BUFFER_MAX_ENTRIES,
    LOG_FLUSH_INTERVAL_MS,
    LOG_TIMESTAMP_FORMAT,
)
from version import APP_NAME, APP_VERSION

logger = logging.getLogger(__name__)


#: Interval at which the QThread's run loop forces a full fsync of all
#: open log files. Bounds worst-case data loss on a hard kill to this
#: window. 30 seconds balances data safety against disk activity.
PERIODIC_FSYNC_INTERVAL_MS = 30_000


# ── Log Configuration ────────────────────────────────────────────────────────

@dataclass
class LogConfig:
    """Configuration for a log session.

    Populated from the NewLogDialog and passed to LogEngine.start_logging().
    """
    session_name: str = ""
    port_name: str = ""
    baud_rate: int = 0
    description: str = ""
    buffer_max_entries: int = LOG_BUFFER_MAX_ENTRIES
    flush_threshold: int = LOG_BUFFER_FLUSH_THRESHOLD
    flush_interval_ms: int = LOG_FLUSH_INTERVAL_MS


# ── Log Entry ────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class LogEntry:
    """A single buffered log entry awaiting disk write."""
    timestamp: str
    line: str
    tag: str


# ── Log Engine ───────────────────────────────────────────────────────────────

class LogEngine(QThread):
    """Writes log data to disk with buffering and guaranteed flush.

    Each DeviceTab owns one LogEngine instance. The engine manages .txt
    and optionally .csv file output simultaneously.

    Signals:
        flush_completed(int):  Number of lines flushed in the last cycle.
        error_occurred(str):   Error message on write failure.
    """

    # ── Signals ──────────────────────────────────────────────────────────

    flush_completed = Signal(int)
    error_occurred = Signal(str)

    def __init__(self, config: LogConfig | None = None, parent=None) -> None:
        super().__init__(parent)

        self._config = config or LogConfig()
        self._running = False
        self._is_logging = False
        self._is_paused = False

        # Thread-safe buffer
        self._buffer: deque[LogEntry] = deque(maxlen=self._config.buffer_max_entries)
        self._buffer_mutex = QMutex()

        # File handles
        self._txt_file = None
        self._csv_file = None
        self._txt_path: str | None = None
        self._csv_path: str | None = None

        # CSV engine reference (set externally when csv is needed)
        self._csv_engine = None

    # ── Public API ───────────────────────────────────────────────────────

    def enqueue(self, timestamp: str, line: str, tag: str) -> None:
        """Add a log entry to the buffer. Thread-safe.

        This is called from the SerialReader thread for every line.
        The deque's maxlen provides automatic overflow protection.

        Args:
            timestamp: Formatted timestamp string.
            line: The decoded serial line.
            tag: Severity tag from TagDetector.
        """
        if not self._is_logging or self._is_paused:
            return

        entry = LogEntry(timestamp=timestamp, line=line, tag=tag)

        with QMutexLocker(self._buffer_mutex):
            self._buffer.append(entry)

            # Trigger early flush if threshold reached
            if len(self._buffer) >= self._config.flush_threshold:
                QTimer.singleShot(0, self._flush)

    def start_logging(
        self,
        txt_path: str,
        csv_path: str | None = None,
        csv_engine=None,
    ) -> bool:
        """Begin a new logging session.

        Opens file handles and writes headers. Must be called before
        enqueue() will accept entries.

        Args:
            txt_path: Full path for the .txt log file.
            csv_path: Full path for the .csv file (None if txt-only).
            csv_engine: Optional CSVEngine instance for structured CSV output.

        Returns:
            True if files were opened successfully, False on error.
        """
        try:
            # Ensure directories exist
            os.makedirs(os.path.dirname(txt_path), exist_ok=True)

            # Open .txt file with 64KB OS buffer.

            # enqueue() → _flush() calls; closed explicitly in
            # stop_logging() via _close_files(). A context manager
            # would defeat the purpose.
            self._txt_file = open(  # noqa: SIM115
                txt_path, "w", encoding="utf-8",
                buffering=FILE_WRITE_BUFFER_SIZE,
            )
            self._txt_path = txt_path

            # Write .txt header
            self._write_txt_header()

            # Open .csv file if requested
            if csv_path:
                os.makedirs(os.path.dirname(csv_path), exist_ok=True)
                # See SIM115 note above — same reasoning applies here.
                self._csv_file = open(  # noqa: SIM115
                    csv_path, "w", encoding="utf-8", newline="",
                    buffering=FILE_WRITE_BUFFER_SIZE,
                )
                self._csv_path = csv_path
                self._csv_engine = csv_engine

                # Write .csv header
                self._write_csv_header()

            self._is_logging = True
            self._is_paused = False
            logger.info("Logging started: %s%s",
                        txt_path,
                        f" + {csv_path}" if csv_path else "")
            return True

        except OSError as e:
            error_msg = f"Failed to open log file: {e}"
            logger.error(error_msg)
            self.error_occurred.emit(error_msg)
            self._close_files()
            return False

    def stop_logging(self) -> None:
        """Stop logging and perform guaranteed final flush.

        Order matters here. The sequence is:
          1. Mark logging stopped so no new entries enter the buffer.
          2. Flush every buffered LogEntry to the file streams (no
             fsync yet — the CSV engine may still emit rows below).
          3. Finalize the CSV engine, which writes any rows it was
             holding for auto-detection sampling. These rows must be
             produced BEFORE the fsync so they survive a power loss
             on a normal shutdown.
          4. Now fsync everything to physical disk.
          5. Close file handles.

        Previously fsync ran inside step 2, leaving the CSV finalize
        rows in OS buffers and exposing them to a power-loss data
        loss window between steps 3 and 5.
        """
        if not self._is_logging:
            return

        self._is_logging = False
        self._is_paused = False

        # Step 2: Flush every buffered entry. No fsync — the CSV
        # engine may still emit rows in step 3.
        self._flush()

        # Step 3: Finalize the CSV engine. If auto-detection never
        # reached its sample threshold, buffered rows would otherwise
        # be lost.
        if self._csv_engine and self._csv_file and not self._csv_file.closed:
            try:
                self._csv_engine.finalize(self._csv_file)
            except OSError as exc:
                logger.warning("CSV finalize error: %s", exc)

        # Step 4: fsync now — including everything CSV finalize wrote.
        self._sync_files()

        # Step 5: Close files.
        self._close_files()
        logger.info("Logging stopped")

    def pause(self) -> None:
        """Pause log writing. Incoming data is discarded while paused."""
        if self._is_logging and not self._is_paused:
            self._is_paused = True
            logger.info("Logging paused")

    def resume(self) -> None:
        """Resume log writing after a pause."""
        if self._is_logging and self._is_paused:
            self._is_paused = False
            logger.info("Logging resumed")

    @property
    def is_logging(self) -> bool:
        """Return True if actively logging."""
        return self._is_logging

    @property
    def is_paused(self) -> bool:
        """Return True if logging is paused."""
        return self._is_paused

    @property
    def txt_path(self) -> str | None:
        """Return the current .txt log file path, or None."""
        return self._txt_path

    @property
    def csv_path(self) -> str | None:
        """Return the current .csv log file path, or None."""
        return self._csv_path

    # ── QThread Entry Point ──────────────────────────────────────────────

    def run(self) -> None:
        """Thread entry point. Runs periodic flush and fsync timers.

        The flush timer fires every flush_interval_ms (1 s) to drain
        the in-memory deque to OS buffers — this protects against
        process crash. The fsync timer fires every
        PERIODIC_FSYNC_INTERVAL_MS (30 s) to push OS buffers all the
        way to physical disk — this protects against power loss and
        kernel panic. The two together bound worst-case loss on hard
        kill to the fsync interval, instead of the entire session.
        """
        self._running = True

        flush_timer = QTimer()
        flush_timer.setInterval(self._config.flush_interval_ms)
        flush_timer.timeout.connect(self._flush)
        flush_timer.start()

        fsync_timer = QTimer()
        fsync_timer.setInterval(PERIODIC_FSYNC_INTERVAL_MS)
        fsync_timer.timeout.connect(self._sync_files)
        fsync_timer.start()

        # Run event loop
        self.exec()

        # Cleanup
        flush_timer.stop()
        fsync_timer.stop()
        self._running = False

    def stop(self) -> None:
        """Stop the thread's event loop. Call stop_logging() first."""
        self.quit()

    # ── Internal: Flush ──────────────────────────────────────────────────

    def _flush(self) -> None:
        """Flush buffered entries to disk.

        The mutex is held for the buffer swap AND the file-write loop, so
        a flush from the periodic timer (engine thread) and a flush from
        ``stop_logging`` (main thread) cannot interleave bytes in the same
        file (bug B9). The mutex serialises ALL file I/O for this engine —
        see ``_sync_files`` and ``_close_files``, which take the same lock.

        Signal emission is done after the lock is released, so a connected
        slot can never run while the lock is held.

        Does NOT call fsync — fsync is driven separately by the periodic
        fsync timer in ``run()`` and explicitly by ``stop_logging()``.
        """
        count = 0
        error_msg: str | None = None

        with QMutexLocker(self._buffer_mutex):
            if not self._buffer:
                return
            # Atomic swap — take all pending entries, install a fresh deque.
            entries = self._buffer
            self._buffer = deque(maxlen=self._config.buffer_max_entries)

            # Write inside the lock (see docstring): prevents interleaved
            # output from concurrent flushes on the same file handle.
            try:
                for entry in entries:
                    self._write_entry(entry)
                    count += 1
            except OSError as e:
                error_msg = f"Disk write error: {e}"

        if error_msg is not None:
            logger.error(error_msg)
            self.error_occurred.emit(error_msg)
        if count > 0:
            self.flush_completed.emit(count)

    def _write_entry(self, entry: LogEntry) -> None:
        """Write a single log entry to all active file handles."""
        # .txt output: [timestamp] line
        if self._txt_file and not self._txt_file.closed:
            self._txt_file.write(f"[{entry.timestamp}] {entry.line}\n")

        # .csv output: via CSVEngine or raw fallback
        if self._csv_file and not self._csv_file.closed:
            if self._csv_engine:
                self._csv_engine.write_row(self._csv_file, entry.timestamp,
                                           entry.line, entry.tag)
            else:
                # Raw CSV fallback: Timestamp,Data
                # Escape any commas or quotes in the data
                escaped = entry.line.replace('"', '""')
                if "," in escaped or '"' in escaped or "\n" in escaped:
                    escaped = f'"{escaped}"'
                self._csv_file.write(f"{entry.timestamp},{escaped}\n")

    def _sync_files(self) -> None:
        """Flush OS buffers and fsync all open files to physical disk.

        Called periodically by the fsync timer in ``run()`` (engine
        thread) and explicitly by ``stop_logging()`` after CSV finalize.
        Takes the buffer mutex so an fsync can never run concurrently with
        a ``_flush`` write or a ``_close_files`` on the same handle (B9).
        """
        with QMutexLocker(self._buffer_mutex):
            for f in (self._txt_file, self._csv_file):
                if f and not f.closed:
                    try:
                        f.flush()
                        os.fsync(f.fileno())
                    except OSError as e:
                        logger.warning("fsync failed: %s", e)

    def _close_files(self) -> None:
        """Close all file handles safely.

        Takes the buffer mutex so a close can never race a concurrent
        ``_flush`` write or ``_sync_files`` fsync on the same handle (B9).
        """
        with QMutexLocker(self._buffer_mutex):
            for attr in ("_txt_file", "_csv_file"):
                f = getattr(self, attr, None)
                if f and not f.closed:
                    try:
                        f.close()
                    except OSError as e:
                        logger.warning("Error closing file: %s", e)
                setattr(self, attr, None)

            self._txt_path = None
            self._csv_path = None
            self._csv_engine = None

    # ── Internal: Headers ────────────────────────────────────────────────

    def _write_txt_header(self) -> None:
        """Write the .txt log file header per spec section 4.4."""
        if not self._txt_file:
            return

        now = datetime.now().strftime(LOG_TIMESTAMP_FORMAT)
        platform_info = f"{platform.system()} {platform.release()}"

        separator = "=" * 80
        lines = [
            separator,
            f"{APP_NAME} v{APP_VERSION} — Log Session",
            separator,
            f"Session Name  : {self._config.session_name or 'Untitled'}",
            f"Port          : {self._config.port_name}",
            f"Baud Rate     : {self._config.baud_rate}",
            f"Started       : {now}",
            f"Platform      : {platform_info}",
        ]

        if self._config.description:
            lines.append(f"Description   : {self._config.description}")

        lines.append(separator)
        lines.append("")  # Blank line before data

        self._txt_file.write("\n".join(lines) + "\n")

    def _write_csv_header(self) -> None:
        """Write the .csv log file header per spec section 4.4."""
        if not self._csv_file:
            return

        now = datetime.now().strftime(LOG_TIMESTAMP_FORMAT)

        lines = [
            f"# {APP_NAME} v{APP_VERSION} — Log Session",
            f"# Session: {self._config.session_name or 'Untitled'}"
            f" | Port: {self._config.port_name}"
            f" | Baud: {self._config.baud_rate}",
            f"# Started: {now}",
        ]

        self._csv_file.write("\n".join(lines) + "\n")

        # Column header — CSVEngine may override this
        if self._csv_engine:
            self._csv_engine.write_header(self._csv_file)
        else:
            self._csv_file.write("Timestamp,Data\n")
