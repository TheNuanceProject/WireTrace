# SPDX-License-Identifier: GPL-3.0-or-later
"""Temporary on-disk record of a session, for Export.

The console keeps a bounded number of lines so memory stays flat over
any session length. The spool is what lets Export still save the whole
session: every record is appended to segment files in a private
directory under the cache location, and read back on request.

Bounded like everything else:
  The spool is cut into segments. When the total passes its cap the
  oldest segment is discarded, and an export says that earlier data
  is no longer available.

Ownership:
  Each spool directory holds a lock file that stays locked for as long
  as its session is alive. A directory whose lock can be taken belongs
  to a session that ended without cleaning up (a crash, a power cut),
  and ``remove_stale`` deletes it. A second running WireTrace never
  removes the spool of the first.

Threading:
  One thread appends. ``SpoolSnapshot.records`` only reads files and
  may run on any thread; a snapshot keeps its segments from being
  discarded until it is released.

This module does not touch the GUI or serial I/O.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from app.constants import SPOOL_MAX_BYTES, SPOOL_SEGMENT_BYTES, SPOOL_STALE_MIN_AGE_S
from core.log_files import PRIVATE_FILE_MODE, FileBackend, OsFileBackend
from core.records import LineRecord, RecordOrigin

logger = logging.getLogger(__name__)

_LOCK_FILE_NAME = "lock"
_PRIVATE_DIRECTORY_MODE = 0o700
_SEGMENT_NAME_FORMAT = "segment-{number:06d}.spool"
_FIELD_SEPARATOR = "\t"
_FIELD_COUNT = 5
# What the record is: a row of the HEX display or a line of text.
_KINDS = {True: "hex", False: "line"}
_KIND_IS_HEX = {kind: hex_row for hex_row, kind in _KINDS.items()}
_ENCODING = "utf-8"
_READ_BLOCK_BYTES = 1024 * 1024


@dataclass(slots=True)
class _Segment:
    path: str
    size: int = 0


@dataclass(frozen=True, slots=True)
class _Slice:
    """A byte range of one segment file."""

    path: str
    start: int
    end: int


class SpoolSnapshot:
    """A fixed view of the spool at the moment it was taken.

    Attributes:
        truncated: True if older data had already been discarded, so
            the snapshot does not reach back to the start of the session.
    """

    def __init__(self, slices: list[_Slice], truncated: bool, release) -> None:
        self._slices = slices
        self.truncated = truncated
        self._release = release
        self._released = False

    @property
    def is_empty(self) -> bool:
        """True if the snapshot holds no records."""
        return not self._slices

    def records(self) -> Iterator[LineRecord]:
        """Yield every record in the snapshot, oldest first."""
        for piece in self._slices:
            yield from _read_slice(piece)

    def release(self) -> None:
        """Allow the spool to discard the segments this snapshot covers."""
        if not self._released:
            self._released = True
            self._release()


class SessionSpool:
    """Appends the records of one session to segment files.

    Args:
        root: Directory that holds the spool directories of all sessions.
        backend: File-system operations; replaceable for tests.
        segment_bytes: Size at which a new segment starts.
        max_bytes: Total size beyond which the oldest segment is discarded.
    """

    def __init__(
        self,
        root: str,
        *,
        backend: FileBackend | None = None,
        segment_bytes: int = SPOOL_SEGMENT_BYTES,
        max_bytes: int = SPOOL_MAX_BYTES,
    ) -> None:
        self._directory = os.path.join(root, uuid.uuid4().hex)
        self._backend: FileBackend = backend or OsFileBackend()
        self._segment_bytes = segment_bytes
        self._max_bytes = max_bytes

        self._lock_file = None
        self._descriptor: int | None = None
        self._segments: list[_Segment] = []
        self._next_segment_number = 1
        self._total_bytes = 0
        self._truncated = False

        # Where an export starts: (segment path, offset). None = the
        # start of the oldest segment still held.
        self._mark: tuple[str, int] | None = None

        # Snapshots being read. While there is one, segments are not
        # discarded and a requested delete waits.
        self._holds = 0
        self._delete_pending = False
        self._holds_lock = threading.Lock()

    # ── Public API ───────────────────────────────────────────────────────

    @property
    def directory(self) -> str:
        return self._directory

    def create(self) -> None:
        """Create the spool directory and its first segment.

        Raises:
            OSError: The directory or segment could not be created.
        """
        # A recording holds everything the device sent, so its folder and
        # files are for the user alone. The mode has no effect on Windows,
        # where the folder is inside the user's profile.
        os.makedirs(self._directory, mode=_PRIVATE_DIRECTORY_MODE, exist_ok=True)
        os.chmod(self._directory, _PRIVATE_DIRECTORY_MODE)
        self._lock_file = _lock(os.path.join(self._directory, _LOCK_FILE_NAME))
        self._open_segment()

    def append(self, records: list[LineRecord]) -> None:
        """Append records to the spool.

        Raises:
            OSError: The write failed. The spool should not be used again.
        """
        if self._descriptor is None:
            raise OSError("session spool is not open")
        current = self._segments[-1]
        if current.size >= self._segment_bytes:
            self._open_segment()
            self._discard_oldest_segments()
            current = self._segments[-1]

        data = b"".join(_encode(record) for record in records)
        view = memoryview(data)
        offset = 0
        while offset < len(data):
            written = self._backend.write(self._descriptor, view[offset:])
            if written <= 0:
                raise OSError("the operating system accepted no bytes")
            offset += written
            current.size += written
            self._total_bytes += written

    def mark(self) -> None:
        """Make the current end of the spool the start of later exports."""
        if self._segments:
            current = self._segments[-1]
            self._mark = (current.path, current.size)

    def snapshot(self) -> SpoolSnapshot:
        """Capture everything from the mark to the current end."""
        slices: list[_Slice] = []
        started = self._mark is None
        for segment in self._segments:
            start = 0
            if not started:
                if segment.path != self._mark[0]:
                    continue
                started = True
                start = self._mark[1]
            if segment.size > start:
                slices.append(_Slice(segment.path, start, segment.size))

        with self._holds_lock:
            self._holds += 1
        truncated = self._truncated and self._mark is None
        return SpoolSnapshot(slices, truncated, self._release_hold)

    def close(self) -> None:
        """Stop appending. The files stay readable until ``delete``."""
        if self._descriptor is not None:
            descriptor, self._descriptor = self._descriptor, None
            with contextlib.suppress(OSError):
                self._backend.close(descriptor)

    def delete(self) -> None:
        """Close the spool and remove its directory.

        If a snapshot is still being read (an export in progress), the
        files are removed when that snapshot is released, so the export
        finishes with everything it was promised.
        """
        self.close()
        with self._holds_lock:
            if self._holds:
                self._delete_pending = True
                return
        self._remove_directory()

    def _remove_directory(self) -> None:
        if self._lock_file is not None:
            lock_file, self._lock_file = self._lock_file, None
            lock_file.close()
        shutil.rmtree(self._directory, ignore_errors=True)

    @staticmethod
    def remove_stale(root: str) -> None:
        """Delete spool directories left behind by sessions that are gone."""
        try:
            names = os.listdir(root)
        except OSError:
            return
        newest_removable = time.time() - SPOOL_STALE_MIN_AGE_S
        for name in names:
            directory = os.path.join(root, name)
            lock_path = os.path.join(directory, _LOCK_FILE_NAME)
            try:
                if os.path.getmtime(directory) > newest_removable:
                    # Possibly another WireTrace starting right now,
                    # between creating its directory and locking it.
                    continue
            except OSError:
                continue
            if not os.path.isfile(lock_path):
                continue  # not a spool
            try:
                lock_file = _lock(lock_path)
            except OSError:
                continue  # locked: its session is alive
            lock_file.close()
            shutil.rmtree(directory, ignore_errors=True)
            logger.info("Removed stale session spool %s", directory)

    # ── Internal ─────────────────────────────────────────────────────────

    def _open_segment(self) -> None:
        if self._descriptor is not None:
            descriptor, self._descriptor = self._descriptor, None
            self._backend.close(descriptor)
        name = _SEGMENT_NAME_FORMAT.format(number=self._next_segment_number)
        path = os.path.join(self._directory, name)
        self._descriptor = self._backend.create(path, PRIVATE_FILE_MODE)
        self._next_segment_number += 1
        self._segments.append(_Segment(path))

    def _discard_oldest_segments(self) -> None:
        """Drop the oldest segments while the spool is over its cap.

        Skipped while a snapshot is being read; the next segment change
        catches up.
        """
        with self._holds_lock:
            if self._holds:
                return
        while self._total_bytes > self._max_bytes and len(self._segments) > 1:
            oldest = self._segments.pop(0)
            try:
                os.remove(oldest.path)
            except OSError as exc:
                logger.warning("Could not discard spool segment: %s", exc)
                self._segments.insert(0, oldest)
                return
            self._total_bytes -= oldest.size
            self._truncated = True
            if self._mark is not None and self._mark[0] == oldest.path:
                self._mark = None

    def _release_hold(self) -> None:
        with self._holds_lock:
            self._holds -= 1
            remove = self._delete_pending and self._holds == 0
            if remove:
                self._delete_pending = False
        if remove:
            self._remove_directory()


# ── Record Encoding ──────────────────────────────────────────────────────────

def _encode(record: LineRecord) -> bytes:
    line = _FIELD_SEPARATOR.join((
        record.timestamp.isoformat(),
        record.origin.value,
        record.tag,
        _KINDS[record.hex_row],
        record.text,
    ))
    return f"{line}\n".encode(_ENCODING, errors="backslashreplace")


def _decode(line: str) -> LineRecord | None:
    fields = line.split(_FIELD_SEPARATOR, _FIELD_COUNT - 1)
    if len(fields) != _FIELD_COUNT:
        return None
    try:
        return LineRecord(
            timestamp=datetime.fromisoformat(fields[0]),
            origin=RecordOrigin(fields[1]),
            tag=fields[2],
            hex_row=_KIND_IS_HEX[fields[3]],
            text=fields[4],
        )
    except (ValueError, KeyError):
        return None


def _read_slice(piece: _Slice) -> Iterator[LineRecord]:
    """Yield the records stored in one byte range of a segment."""
    remaining = piece.end - piece.start
    tail = b""
    with open(piece.path, "rb") as segment:
        segment.seek(piece.start)
        while remaining > 0:
            block = segment.read(min(_READ_BLOCK_BYTES, remaining))
            if not block:
                break
            remaining -= len(block)
            lines = (tail + block).split(b"\n")
            tail = lines.pop()
            for raw in lines:
                record = _decode(raw.decode(_ENCODING))
                if record is not None:
                    yield record


# ── Directory Lock ───────────────────────────────────────────────────────────

def _lock(path: str):
    """Open ``path`` and take an exclusive lock on it without waiting.

    Returns the open file; the lock lasts until it is closed (or the
    process ends). Raises ``OSError`` if another process holds the lock.
    """
    lock_file = open(path, "a+b")  # noqa: SIM115 (held open on purpose)
    try:
        if sys.platform == "win32":
            import msvcrt
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        raise
    return lock_file
