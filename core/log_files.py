# SPDX-License-Identifier: GPL-3.0-or-later
"""Append-only log files with exact write bookkeeping and splitting.

``SplitLogFile`` is one logical log that is stored as one or more part
files. It is built for three guarantees:

  Nothing is lost silently.
      Every chunk handed to ``append`` is either written, still queued
      for a retry, or counted as lost and reported in the file itself.

  A failed write leaves no gap and no duplicate.
      Writes go straight to the file descriptor, so the number of bytes
      the operating system accepted is always known. After an error the
      unwritten remainder is kept, byte for byte, and written first on
      the next attempt.

  A large log stays usable.
      When a part reaches its size (or row) limit, the next chunk
      starts a new part with its own header. Parts are only ever cut
      between chunks, never inside a line.

The class is not thread-safe: one thread owns a ``SplitLogFile``.

This module does not touch the GUI or serial I/O.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from app.constants import (
    FILENAME_CLASH_MAX_ATTEMPTS,
    FILENAME_CLASH_SUFFIX_FORMAT,
    LOG_PART_SUFFIX_FORMAT,
    LOG_PENDING_MAX_BYTES,
)

#: Builds the header bytes of the part with the given number (1-based).
HeaderFactory = Callable[[int], bytes]

#: Builds the line written in place of chunks that were lost while the
#: file could not be written. Receives the number of lost units.
LossMarkerFactory = Callable[[int], bytes]

_NEW_FILE_FLAGS = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
)
#: A log or an export is for whoever the user's settings allow. A
#: private file is for the user alone.
_NEW_FILE_MODE = 0o644
PRIVATE_FILE_MODE = 0o600


class FileBackend(Protocol):
    """The file-system operations a log file needs."""

    def create(self, path: str, mode: int = _NEW_FILE_MODE) -> int:
        """Create a new file and return its descriptor. Must not replace
        an existing file: raises ``FileExistsError`` instead."""

    def write(self, descriptor: int, data: memoryview) -> int:
        """Write and return how many bytes were accepted."""

    def sync(self, descriptor: int) -> None:
        """Force written bytes to the storage device."""

    def close(self, descriptor: int) -> None:
        """Close the descriptor."""


class OsFileBackend:
    """``FileBackend`` on the real file system."""

    def create(self, path: str, mode: int = _NEW_FILE_MODE) -> int:
        return os.open(path, _NEW_FILE_FLAGS, mode)

    def write(self, descriptor: int, data: memoryview) -> int:
        return os.write(descriptor, data)

    def sync(self, descriptor: int) -> None:
        if sys.platform == "darwin":
            # On macOS fsync only reaches the drive's cache; F_FULLFSYNC
            # asks the drive to commit it. Not every volume supports it.
            import fcntl
            with contextlib.suppress(OSError):
                fcntl.fcntl(descriptor, fcntl.F_FULLFSYNC)
                return
        os.fsync(descriptor)

    def close(self, descriptor: int) -> None:
        os.close(descriptor)


logger = logging.getLogger(__name__)

# Most bytes handed to the operating system in one write.
_WRITE_RUN_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class _Chunk:
    """Queue entry holding bytes to write and the units they represent."""

    data: bytes
    units: int


class _PartBreak:
    """Queue entry marking where the current part ends and the next begins."""


class SplitLogFile:
    """One log stored as a sequence of part files.

    Args:
        base_path: Path of the first part. Later parts insert a part
            suffix before the extension.
        header: Builds the header of each part.
        loss_marker: Builds the line that records lost chunks.
        part_limit_bytes: Size at which a new part starts (0 = never).
        part_limit_units: Unit count at which a new part starts (0 = never).
        eager_header: Write the first part's header when the file is
            opened. If False it is written with the first chunk, for
            logs whose header is not known until data has arrived.
        backend: File-system operations; replaceable for tests.
    """

    def __init__(
        self,
        base_path: str,
        header: HeaderFactory,
        loss_marker: LossMarkerFactory,
        *,
        part_limit_bytes: int = 0,
        part_limit_units: int = 0,
        eager_header: bool = True,
        backend: FileBackend | None = None,
    ) -> None:
        self._base_path = base_path
        self._header = header
        self._loss_marker = loss_marker
        self._part_limit_bytes = part_limit_bytes
        self._part_limit_units = part_limit_units
        self._eager_header = eager_header
        self._backend: FileBackend = backend or OsFileBackend()

        self._descriptor: int | None = None
        self._paths: list[str] = []
        self._part_number = 1

        # Chunks accepted by append() but not yet by the operating system.
        self._queue: deque[_Chunk | _PartBreak] = deque()
        self._queued_bytes = 0
        self._queued_units = 0
        self._head_offset = 0

        # Accounting of the part currently being appended to.
        self._appending_part = 1
        self._part_bytes = 0
        self._part_units = 0
        self._part_has_header = False

        self._failing = False
        self._units_written = 0
        self._units_lost = 0
        self._units_lost_unmarked = 0

    # ── Public API ───────────────────────────────────────────────────────

    @property
    def paths(self) -> list[str]:
        """Paths of every part created so far, in order."""
        return list(self._paths)

    @property
    def units_written(self) -> int:
        """Units handed to the operating system so far."""
        return self._units_written

    @property
    def units_lost(self) -> int:
        """Units that could not be written and were discarded."""
        return self._units_lost

    @property
    def appending_part(self) -> int:
        """Number of the part that new data goes to, starting at 1."""
        return self._appending_part

    @property
    def appending_part_bytes(self) -> int:
        """Bytes given to that part so far, its header included."""
        return self._part_bytes

    @property
    def queued_bytes(self) -> int:
        """Bytes waiting to be written."""
        return self._queued_bytes

    @property
    def is_failing(self) -> bool:
        """True while the most recent write attempt has failed."""
        return self._failing

    def open(self) -> None:
        """Create the first part.

        Raises:
            OSError: The file could not be created or its header written.
        """
        self._descriptor = self._create_part(self._base_path)
        if self._eager_header:
            self._enqueue_header()
            self.flush()

    def append(self, data: bytes, units: int = 1) -> bool:
        """Queue a chunk for writing.

        Returns False if the chunk was discarded: while the file is
        failing, chunks beyond the pending budget are counted as lost
        rather than held in memory without limit.
        """
        if self._failing and self._queued_bytes + len(data) > LOG_PENDING_MAX_BYTES:
            self._units_lost += units
            self._units_lost_unmarked += units
            return False

        if self._units_lost_unmarked:
            marker = self._loss_marker(self._units_lost_unmarked)
            self._units_lost_unmarked = 0
            self._enqueue_chunk(marker, units=0)

        self._enqueue_chunk(data, units)
        return True

    def flush(self) -> None:
        """Hand every queued byte to the operating system.

        Raises:
            OSError: A write or the start of a new part failed. The
                unwritten remainder stays queued for the next call.
        """
        try:
            while self._queue:
                head = self._queue[0]
                if isinstance(head, _PartBreak):
                    self._start_next_part()
                    self._queue.popleft()
                    continue
                self._write_run()
        except OSError:
            self._failing = True
            raise
        self._failing = False

    def sync(self) -> None:
        """Force written bytes to the storage device.

        Raises:
            OSError: The sync failed.
        """
        if self._descriptor is not None:
            self._backend.sync(self._descriptor)

    def close(self) -> None:
        """Write what is queued, sync, and close the file.

        A log that never received a chunk still gets its header, so an
        empty session leaves a well-formed file.

        Raises:
            OSError: The remaining bytes could not be written. They are
                counted as lost and the file is closed regardless.
        """
        try:
            if not self._part_has_header:
                self._enqueue_header()
            if self._units_lost_unmarked:
                # Lines were lost and none followed them, so the marker
                # that records the loss goes at the end of the file.
                marker = self._loss_marker(self._units_lost_unmarked)
                self._units_lost_unmarked = 0
                self._enqueue_chunk(marker, units=0)
            self.flush()
            self.sync()
        except OSError:
            self._units_lost += self._queued_units
            self._queue.clear()
            self._queued_bytes = 0
            self._queued_units = 0
            self._head_offset = 0
            raise
        finally:
            self._close_descriptor()

    # ── Internal ─────────────────────────────────────────────────────────

    def _enqueue_chunk(self, data: bytes, units: int) -> None:
        if self._part_is_full(len(data), units):
            self._queue.append(_PartBreak())
            self._appending_part += 1
            self._part_bytes = 0
            self._part_units = 0
            self._part_has_header = False
        if not self._part_has_header:
            self._enqueue_header()
        self._enqueue(data, units)
        self._part_units += units

    def _part_is_full(self, incoming_bytes: int, incoming_units: int) -> bool:
        if self._part_units == 0:
            # A part always holds at least one chunk, whatever its size.
            return False
        if self._part_limit_bytes and (
            self._part_bytes + incoming_bytes > self._part_limit_bytes
        ):
            return True
        return bool(self._part_limit_units) and (
            self._part_units + incoming_units > self._part_limit_units
        )

    def _enqueue_header(self) -> None:
        pending_breaks = sum(isinstance(item, _PartBreak) for item in self._queue)
        self._enqueue(self._header(self._part_number + pending_breaks), units=0)
        self._part_has_header = True

    def _enqueue(self, data: bytes, units: int) -> None:
        self._queue.append(_Chunk(data, units))
        self._queued_bytes += len(data)
        self._queued_units += units
        self._part_bytes += len(data)

    def _write_run(self) -> None:
        """Write the chunks at the head of the queue, up to the next
        part break, resuming where a previous failed attempt stopped.

        Chunks are joined so a batch of lines costs one system call
        rather than one per line. Each chunk is still accounted for on
        its own, so after a failure the count of what was written and
        what remains is exact.
        """
        if self._descriptor is None:
            raise OSError("log file is not open")
        run: list[bytes] = []
        run_bytes = 0
        for item in self._queue:
            if isinstance(item, _PartBreak) or run_bytes >= _WRITE_RUN_BYTES:
                break
            run.append(item.data)
            run_bytes += len(item.data)
        data = b"".join(run)
        offset = self._head_offset
        with memoryview(data) as view:
            while offset < len(data):
                written = self._backend.write(self._descriptor, view[offset:])
                if written <= 0:
                    raise OSError("the operating system accepted no bytes")
                offset += written
                self._account_written(written)
        self._account_written(0)

    def _account_written(self, written: int) -> None:
        """Remove ``written`` bytes from the head of the queue."""
        self._queued_bytes -= written
        while self._queue:
            head = self._queue[0]
            if isinstance(head, _PartBreak):
                return
            remaining = len(head.data) - self._head_offset
            if remaining > written:
                self._head_offset += written
                return
            written -= remaining
            self._queue.popleft()
            self._head_offset = 0
            self._queued_units -= head.units
            self._units_written += head.units

    def _start_next_part(self) -> None:
        """Close the current part and create the next one."""
        if self._descriptor is not None:
            self._backend.sync(self._descriptor)
            self._close_descriptor()
        root, extension = os.path.splitext(self._base_path)
        suffix = LOG_PART_SUFFIX_FORMAT.format(number=self._part_number + 1)
        self._descriptor = self._create_part(f"{root}{suffix}{extension}")
        self._part_number += 1

    def _close_descriptor(self) -> None:
        """Close the current part.

        The descriptor is given up before the close is attempted, so a
        close that fails is never retried on a number the operating
        system may already have handed to another file. The data was
        synced beforehand, so such a failure loses nothing.
        """
        if self._descriptor is None:
            return
        descriptor, self._descriptor = self._descriptor, None
        try:
            self._backend.close(descriptor)
        except OSError as exc:
            logger.warning("Closing %s reported: %s", self._paths[-1], exc)

    def _create_part(self, path: str) -> int:
        """Create a new file at ``path``, or at a numbered alternative
        if that name is already taken. Never replaces an existing file."""
        root, extension = os.path.splitext(path)
        candidate = path
        for attempt in range(2, FILENAME_CLASH_MAX_ATTEMPTS + 2):
            try:
                descriptor = self._backend.create(candidate)
            except FileExistsError:
                suffix = FILENAME_CLASH_SUFFIX_FORMAT.format(number=attempt)
                candidate = f"{root}{suffix}{extension}"
                continue
            if not self._paths:
                # The first part's final name is the base of every later part.
                self._base_path = candidate
            self._paths.append(candidate)
            return descriptor
        raise FileExistsError(f"no free file name for {path}")
