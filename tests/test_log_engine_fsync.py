# SPDX-License-Identifier: MIT
"""Tests for LogEngine fsync semantics.

Two regressions are guarded here:

1. **fsync ordering on stop**: ``stop_logging`` must call ``_sync_files``
   AFTER ``csv_engine.finalize``, not before. Earlier code fsynced
   inside the final flush, leaving CSV finalize content exposed to a
   power-loss data-loss window.

2. **Periodic fsync wiring**: ``run()`` must install a fsync timer
   in addition to the existing flush timer, bounding worst-case data
   loss on hard kill to ``PERIODIC_FSYNC_INTERVAL_MS`` rather than
   the entire session.

These tests use mocking against the PySide6 stubs in conftest.py and
do not touch real files or threads.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call

from core.log_engine import PERIODIC_FSYNC_INTERVAL_MS, LogEngine


class TestStopLoggingOrdering:
    """``stop_logging`` must fsync AFTER csv_engine.finalize."""

    def test_sync_called_after_finalize(self):
        engine = LogEngine()

        # Pretend logging is active with both files open.
        engine._is_logging = True
        engine._is_paused = False
        engine._txt_file = MagicMock()
        engine._txt_file.closed = False
        engine._csv_file = MagicMock()
        engine._csv_file.closed = False
        engine._csv_engine = MagicMock()

        # Use a single Mock to record call order across all the steps.
        order = MagicMock()
        order.attach_mock(MagicMock(), "flush")
        order.attach_mock(engine._csv_engine.finalize, "finalize")
        order.attach_mock(MagicMock(), "sync")
        order.attach_mock(MagicMock(), "close")

        engine._flush = order.flush
        engine._csv_engine.finalize = order.finalize
        engine._sync_files = order.sync
        engine._close_files = order.close

        engine.stop_logging()

        # Expected sequence:
        #   _flush  →  csv_engine.finalize  →  _sync_files  →  _close_files
        expected = [
            call.flush(),
            call.finalize(engine._csv_file),
            call.sync(),
            call.close(),
        ]
        assert order.mock_calls == expected, (
            f"Wrong stop_logging ordering. "
            f"Expected {expected!r}, got {order.mock_calls!r}"
        )

    def test_no_csv_engine_still_syncs_before_close(self):
        """When CSV is disabled, sync still happens before close."""
        engine = LogEngine()
        engine._is_logging = True
        engine._txt_file = MagicMock()
        engine._txt_file.closed = False
        engine._csv_file = None
        engine._csv_engine = None

        order = MagicMock()
        order.attach_mock(MagicMock(), "flush")
        order.attach_mock(MagicMock(), "sync")
        order.attach_mock(MagicMock(), "close")

        engine._flush = order.flush
        engine._sync_files = order.sync
        engine._close_files = order.close

        engine.stop_logging()

        assert order.mock_calls == [
            call.flush(), call.sync(), call.close(),
        ]

    def test_finalize_oserror_does_not_skip_sync(self):
        """A failing CSV finalize must not bypass the fsync."""
        engine = LogEngine()
        engine._is_logging = True
        engine._txt_file = MagicMock()
        engine._txt_file.closed = False
        engine._csv_file = MagicMock()
        engine._csv_file.closed = False
        engine._csv_engine = MagicMock()
        engine._csv_engine.finalize.side_effect = OSError("disk full")

        engine._flush = MagicMock()
        engine._sync_files = MagicMock()
        engine._close_files = MagicMock()

        # Should not raise — finalize errors are logged and swallowed.
        engine.stop_logging()

        engine._sync_files.assert_called_once()
        engine._close_files.assert_called_once()


class TestPeriodicFsync:
    """``run()`` must install a fsync timer alongside the flush timer."""

    def test_constant_is_30_seconds(self):
        """Sanity check the interval. 30 s balances data safety
        against disk activity on idle systems."""
        assert PERIODIC_FSYNC_INTERVAL_MS == 30_000

    def test_run_starts_two_timers(self, monkeypatch):
        """``run()`` should construct a flush timer AND an fsync timer,
        each connected to its respective method, and start both."""
        timers_created = []

        class FakeTimer:
            def __init__(self):
                timers_created.append(self)
                self.interval = None
                self.connections = []
                self.start_count = 0
                self.stop_count = 0

            def setInterval(self, ms):
                self.interval = ms

            @property
            def timeout(self):
                outer = self

                class Conn:
                    def connect(self, slot):
                        outer.connections.append(slot)

                return Conn()

            def start(self):
                self.start_count += 1

            def stop(self):
                self.stop_count += 1

        # Patch QTimer used inside log_engine; patch exec()/_running so
        # run() returns immediately after wiring up the timers.
        from core import log_engine
        monkeypatch.setattr(log_engine, "QTimer", FakeTimer)

        engine = LogEngine()
        engine.exec = MagicMock()  # event loop returns immediately

        engine.run()

        assert len(timers_created) == 2, (
            "run() must install exactly two timers (flush + fsync)"
        )

        # Identify which is which by interval
        intervals = sorted(t.interval for t in timers_created)
        assert intervals[0] == engine._config.flush_interval_ms
        assert intervals[1] == PERIODIC_FSYNC_INTERVAL_MS

        # Each timer must have been started, stopped, and have at
        # least one connection wiring it to its periodic action.
        for t in timers_created:
            assert t.start_count == 1, "timer must be started exactly once"
            assert t.stop_count == 1, "timer must be stopped on exit"
            assert len(t.connections) >= 1
