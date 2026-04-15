# SPDX-License-Identifier: MIT
"""WireTrace high-performance serial data reader.

Runs in a dedicated QThread. Receives raw bytes from SerialManager's
data_received signal via a thread-safe queue, assembles complete lines,
classifies severity, and emits processed lines to subscribers.

Threading model:
  SerialManager reads bytes in the main thread (readyRead → readAll, microseconds)
  and emits data_received. SerialReader.enqueue_data() is called in the main thread
  but only touches a thread-safe queue.Queue. The run() loop consumes from the queue
  in its own thread — zero cross-thread QObject access.

Read loop (per spec section 5.2):
  1. Wait for data in queue (event-driven, zero CPU when idle)
  2. Dequeue all available byte chunks
  3. Append to line assembly buffer (bytearray)
  4. Split on \\n (keep incomplete last line in buffer)
  5. For each complete line:
     a. Decode (UTF-8 with replace)
     b. Detect tag via TagDetector (7 severity tags)
     c. Emit line_received(decoded, tag)
  6. Every 1 second: emit rate_updated(count)

Signals are thread-safe via Qt Signal/Slot mechanism. No shared mutable state.

This module does NOT touch: GUI or disk I/O.
"""

from __future__ import annotations

import logging
import queue
import time

from PySide6.QtCore import QThread, Signal

from app.constants import DisplayMode
from core.tag_detector import TagDetector

logger = logging.getLogger(__name__)


class SerialReader(QThread):
    """Reads serial data in a dedicated thread, emits decoded lines.

    Data arrives via enqueue_data() (called from main thread). All processing
    happens in the reader thread (run loop).

    Signals:
        line_received(str, str):  (decoded_line, tag) for each complete line.
        raw_received(bytes):      Raw bytes for HEX display mode.
        rate_updated(int):        Lines-per-second metric, emitted every ~1s.
        error_occurred(str):      Error message on processing failure.
    """

    # ── Signals ──────────────────────────────────────────────────────────

    line_received = Signal(str, str)     # (decoded_line, tag)
    raw_received = Signal(bytes)         # raw bytes for HEX mode
    rate_updated = Signal(int)           # lines/sec
    error_occurred = Signal(str)         # error_message

    def __init__(
        self,
        display_mode: DisplayMode = DisplayMode.TEXT,
        parent=None,
    ) -> None:
        """Initialize the serial reader.

        Args:
            display_mode: TEXT or HEX — controls whether raw_received is emitted.
            parent: Optional QObject parent.
        """
        super().__init__(parent)

        self._display_mode = display_mode
        self._running = False

        # Thread-safe data queue (producer-consumer pattern)
        # Main thread puts bytes, reader thread gets them
        self._data_queue: queue.Queue[bytes] = queue.Queue()

        # Line assembly buffer — holds incomplete line data between reads
        self._line_buffer = bytearray()

        # Rate tracking
        self._line_count = 0
        self._total_lines = 0
        self._total_bytes = 0

        # Tag detector (stateless, no initialization needed)
        self._tag_detector = TagDetector()

    # ── Public API ───────────────────────────────────────────────────────

    @property
    def display_mode(self) -> DisplayMode:
        """Current display mode (TEXT or HEX)."""
        return self._display_mode

    @display_mode.setter
    def display_mode(self, mode: DisplayMode) -> None:
        """Change display mode at runtime (thread-safe via atomic assignment)."""
        self._display_mode = mode

    @property
    def total_lines(self) -> int:
        """Total lines processed since start."""
        return self._total_lines

    @property
    def total_bytes(self) -> int:
        """Total bytes received since start."""
        return self._total_bytes

    def enqueue_data(self, raw_bytes: bytes) -> None:
        """Enqueue raw bytes for processing.

        Called from the main thread by SerialManager.data_received signal.
        Thread-safe: queue.Queue handles synchronization internally.

        Args:
            raw_bytes: Raw bytes received from the serial port.
        """
        if self._running:
            self._data_queue.put_nowait(raw_bytes)

    def run(self) -> None:
        """Thread entry point. Consumer loop for the data queue.

        Blocks on queue.get() with 100ms timeout. This provides:
          - Zero CPU when idle (blocks in kernel)
          - ~100ms response to stop() requests
          - Batched processing for high throughput
        """
        self._running = True
        self._line_buffer.clear()
        self._line_count = 0
        self._total_lines = 0
        self._total_bytes = 0

        logger.info("SerialReader started (producer-consumer mode)")

        last_rate_time = time.monotonic()

        while self._running:
            try:
                # Block until data arrives or timeout (100ms)
                chunk = self._data_queue.get(timeout=0.1)
            except queue.Empty:
                # No data — check rate reporting
                now = time.monotonic()
                if now - last_rate_time >= 1.0:
                    self.rate_updated.emit(self._line_count)
                    self._line_count = 0
                    last_rate_time = now
                continue

            # Drain any additional queued chunks for batch processing
            accumulated = bytearray(chunk)
            while True:
                try:
                    more = self._data_queue.get_nowait()
                    accumulated.extend(more)
                except queue.Empty:
                    break

            data_len = len(accumulated)
            self._total_bytes += data_len

            # HEX mode: emit raw bytes
            if self._display_mode == DisplayMode.HEX:
                self.raw_received.emit(bytes(accumulated))

            # Append to line assembly buffer and process
            self._line_buffer.extend(accumulated)
            self._process_lines()

            # Rate reporting (every ~1 second)
            now = time.monotonic()
            if now - last_rate_time >= 1.0:
                self.rate_updated.emit(self._line_count)
                self._line_count = 0
                last_rate_time = now

        # Flush remaining buffer on exit
        self._flush_remaining()

        # Final rate emission
        self.rate_updated.emit(0)

        logger.info("SerialReader stopped (total: %d lines, %d bytes)",
                     self._total_lines, self._total_bytes)

    def stop(self) -> None:
        """Request the reader thread to stop.

        Thread-safe. Can be called from any thread.
        The thread will finish processing current data before exiting.
        """
        self._running = False

    # ── Internal Data Processing ─────────────────────────────────────────

    def _process_lines(self) -> None:
        """Extract and emit complete lines from the assembly buffer.

        Lines are split on \\n. An incomplete trailing fragment is kept
        in the buffer for the next read cycle. Handles \\r\\n gracefully.
        """
        while b"\n" in self._line_buffer:
            # Find the first newline
            idx = self._line_buffer.index(b"\n")

            # Extract the complete line (excluding the \\n)
            raw_line = bytes(self._line_buffer[:idx])
            del self._line_buffer[:idx + 1]

            # Strip trailing \\r if present (\\r\\n → clean line)
            if raw_line.endswith(b"\r"):
                raw_line = raw_line[:-1]

            # Decode with replacement for invalid bytes
            decoded = raw_line.decode("utf-8", errors="replace")

            # Skip empty lines (pure whitespace)
            stripped = decoded.strip()
            if not stripped:
                continue

            # Classify severity
            tag = self._tag_detector.detect(decoded)

            # Track line count
            self._total_lines += 1
            self._line_count += 1

            # Emit to all subscribers (ConsoleView, LogEngine, future Plotter)
            self.line_received.emit(decoded, tag)

    def _flush_remaining(self) -> None:
        """Process any remaining data in the buffer on shutdown.

        If there's a partial line (no trailing newline), emit it as-is.
        This ensures no data is lost during ordered shutdown.
        """
        if not self._line_buffer:
            return

        raw_line = bytes(self._line_buffer)
        self._line_buffer.clear()

        if raw_line.endswith(b"\r"):
            raw_line = raw_line[:-1]

        if not raw_line:
            return

        decoded = raw_line.decode("utf-8", errors="replace").strip()
        if decoded:
            tag = self._tag_detector.detect(decoded)
            self._total_lines += 1
            self.line_received.emit(decoded, tag)
