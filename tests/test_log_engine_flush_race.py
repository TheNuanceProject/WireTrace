# SPDX-License-Identifier: MIT
"""Regression tests for B9 — concurrent _flush race in LogEngine.

The bug: ``_flush`` held the buffer mutex only for the atomic buffer
swap, then wrote the entries to disk OUTSIDE the lock. ``_flush`` runs
both from the periodic flush timer (engine thread) and synchronously from
``stop_logging`` (main thread), so two flushes could write to the same
file handle concurrently and interleave bytes — corrupting the log the
user trusts as the source of truth. ``_sync_files`` (fsync timer) and
``_close_files`` were likewise unsynchronised against writes.

The fix makes the mutex the single serialisation point for all file I/O:
the ``_flush`` write loop, ``_sync_files``, and ``_close_files`` all run
under the lock, so no two of them can touch a handle at once.

The PySide6 stubs make ``QMutexLocker`` a no-op that isn't even a context
manager, so these tests install instrumented real lock objects (a
``threading.Lock`` plus an occupancy counter) and assert that each file
operation runs while the lock is held — the exact invariant the fix adds.
A two-thread contention test confirms the critical section is never
entered by more than one thread at a time.
"""

from __future__ import annotations

import io
import threading
from unittest.mock import MagicMock

import pytest

import core.log_engine as log_engine
from core.log_engine import LogEngine, LogEntry


class _InstrumentedMutex:
    """Real mutex with an occupancy counter for assertions."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.inside = 0       # holders currently in the critical section
        self.max_inside = 0   # peak observed occupancy (must stay 1)

    def acquire(self) -> None:
        self._lock.acquire()
        self.inside += 1
        self.max_inside = max(self.max_inside, self.inside)

    def release(self) -> None:
        self.inside -= 1
        self._lock.release()


class _InstrumentedLocker:
    """Context-manager stand-in for QMutexLocker over _InstrumentedMutex."""

    def __init__(self, mutex: _InstrumentedMutex) -> None:
        self._mutex = mutex

    def __enter__(self) -> _InstrumentedLocker:
        self._mutex.acquire()
        return self

    def __exit__(self, *_exc: object) -> bool:
        self._mutex.release()
        return False


@pytest.fixture()
def patched_locks(monkeypatch):
    monkeypatch.setattr(log_engine, "QMutex", _InstrumentedMutex)
    monkeypatch.setattr(log_engine, "QMutexLocker", _InstrumentedLocker)
    # fsync would choke on a mock file descriptor; neutralise it.
    monkeypatch.setattr(log_engine.os, "fsync", lambda _fd: None)


def _entry(i: int) -> LogEntry:
    return LogEntry(timestamp=f"t{i}", line=f"line{i}", tag="DATA")


class TestFlushWritesUnderLock:
    def test_every_write_runs_while_lock_held(self, patched_locks):
        engine = LogEngine()
        engine._is_logging = True
        engine._txt_file = io.StringIO()

        for i in range(5):
            engine._buffer.append(_entry(i))

        held: list[int] = []
        original = engine._write_entry

        def spy(entry):
            held.append(engine._buffer_mutex.inside)
            original(entry)

        engine._write_entry = spy
        engine._flush()

        # The lock was held (occupancy 1) during every single write.
        assert held == [1, 1, 1, 1, 1]

    def test_all_entries_written_and_buffer_drained(self, patched_locks):
        engine = LogEngine()
        engine._is_logging = True
        buf = io.StringIO()
        engine._txt_file = buf

        for i in range(3):
            engine._buffer.append(_entry(i))
        engine._flush()

        out = buf.getvalue()
        assert "line0" in out and "line1" in out and "line2" in out
        assert len(engine._buffer) == 0

    def test_empty_buffer_is_noop(self, patched_locks):
        engine = LogEngine()
        engine._is_logging = True
        engine._txt_file = io.StringIO()

        engine._flush()  # buffer empty

        assert engine._txt_file.getvalue() == ""


class TestSyncAndCloseUnderLock:
    def test_sync_files_runs_under_lock(self, patched_locks):
        engine = LogEngine()
        seen: list[int] = []
        f = MagicMock()
        f.closed = False
        f.flush.side_effect = lambda: seen.append(engine._buffer_mutex.inside)
        engine._txt_file = f
        engine._csv_file = None

        engine._sync_files()

        assert seen == [1]

    def test_close_files_runs_under_lock(self, patched_locks):
        engine = LogEngine()
        seen: list[int] = []
        f = MagicMock()
        f.closed = False
        f.close.side_effect = lambda: seen.append(engine._buffer_mutex.inside)
        engine._txt_file = f
        engine._csv_file = None

        engine._close_files()

        assert seen == [1]
        assert engine._txt_file is None


class TestConcurrentFlushesSerialised:
    def test_two_threads_never_overlap_in_critical_section(self, patched_locks):
        # Sanity check that the lock genuinely serialises the critical
        # section under real contention. (The per-write assertion above is
        # the regression pin; this confirms the locking primitive holds.)
        engine = LogEngine()
        engine._is_logging = True
        engine._txt_file = io.StringIO()
        engine._write_entry = lambda _entry: None  # fast, no I/O

        # Bounded feed: add a fixed number of entries, then stop.
        for i in range(200):
            engine._buffer.append(_entry(i))

        def flusher():
            for _ in range(100):
                engine._flush()

        threads = [threading.Thread(target=flusher) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # The lock guarantees the swap+write critical section is never
        # entered by two threads at once.
        assert engine._buffer_mutex.max_inside == 1
