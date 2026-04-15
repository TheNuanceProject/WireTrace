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
  - fsync() ONLY on stop_logging(), not on every flush

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

        Flushes all remaining buffered entries, finalizes the CSV engine
        (which flushes any rows held for auto-detection sampling), calls
        fsync() on all open files, then closes file handles.
        """
        if not self._is_logging:
            return

        self._is_logging = False
        self._is_paused = False

        # Final flush — guaranteed to write every buffered LogEntry.
        self._flush(final=True)

        # Finalize the CSV engine — if auto-detection never reached its
        # sample threshold, buffered rows would otherwise be lost.
        if self._csv_engine and self._csv_file and not self._csv_file.closed:
            try:
                self._csv_engine.finalize(self._csv_file)
            except OSError as exc:
                logger.warning("CSV finalize error: %s", exc)

        # Close files
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
        """Thread entry point. Runs periodic flush timer.

        The flush timer fires every flush_interval_ms to ensure data
        is written to disk even during low-throughput periods.
        """
        self._running = True

        flush_timer = QTimer()
        flush_timer.setInterval(self._config.flush_interval_ms)
        flush_timer.timeout.connect(self._flush)
        flush_timer.start()

        # Run event loop
        self.exec()

        # Cleanup
        flush_timer.stop()
        self._running = False

    def stop(self) -> None:
        """Stop the thread's event loop. Call stop_logging() first."""
        self.quit()

    # ── Internal: Flush ──────────────────────────────────────────────────

    def _flush(self, final: bool = False) -> None:
        """Flush buffered entries to disk.

        Uses atomic buffer swap to minimize lock hold time:
        the mutex is held only for the swap, not during file I/O.

        Args:
            final: If True, perform fsync() after writing (only on stop).
        """
        # Atomic swap — grab all pending entries, give buffer a fresh deque
        with QMutexLocker(self._buffer_mutex):
            if not self._buffer:
                return
            entries = self._buffer
            self._buffer = deque(maxlen=self._config.buffer_max_entries)

        # Write entries to disk (outside the lock)
        count = 0
        try:
            for entry in entries:
                self._write_entry(entry)
                count += 1
        except OSError as e:
            error_msg = f"Disk write error: {e}"
            logger.error(error_msg)
            self.error_occurred.emit(error_msg)

        # fsync only on final flush (stop_logging)
        if final:
            self._sync_files()

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
                                           entry.line)
            else:
                # Raw CSV fallback: Timestamp,Data
                # Escape any commas or quotes in the data
                escaped = entry.line.replace('"', '""')
                if "," in escaped or '"' in escaped or "\n" in escaped:
                    escaped = f'"{escaped}"'
                self._csv_file.write(f"{entry.timestamp},{escaped}\n")

    def _sync_files(self) -> None:
        """Flush OS buffers and fsync all open files.

        Called ONLY during stop_logging() — never on periodic flushes.
        """
        for f in (self._txt_file, self._csv_file):
            if f and not f.closed:
                try:
                    f.flush()
                    os.fsync(f.fileno())
                except OSError as e:
                    logger.warning("fsync failed: %s", e)

    def _close_files(self) -> None:
        """Close all file handles safely."""
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
