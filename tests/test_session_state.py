# SPDX-License-Identifier: MIT
"""Tests for the per-device session state container.

DeviceSession is a small dataclass but its reset methods are exercised
on every disconnect — a bug there could leak state between sessions.
"""

from __future__ import annotations

from datetime import datetime

from app.constants import (
    DEFAULT_BAUD_RATE,
    DisplayMode,
    ExportFormat,
    TimestampMode,
)
from core.session import DeviceSession


class TestDefaults:
    """New sessions start clean."""

    def test_connection_defaults(self):
        s = DeviceSession()
        assert s.port_name == ""
        assert s.baud_rate == DEFAULT_BAUD_RATE
        assert s.is_connected is False

    def test_logging_defaults(self):
        s = DeviceSession()
        assert s.is_logging is False
        assert s.is_paused is False
        assert s.log_file_path is None
        assert s.csv_file_path is None
        assert s.export_format == ExportFormat.TXT

    def test_display_defaults(self):
        s = DeviceSession()
        assert s.display_mode == DisplayMode.TEXT
        assert s.auto_scroll is True
        assert s.color_mode is True
        assert s.timestamp_mode == TimestampMode.ABSOLUTE
        assert s.filter_text == ""

    def test_metrics_defaults(self):
        s = DeviceSession()
        assert s.data_rate == 0
        assert s.total_lines == 0
        assert s.total_bytes == 0
        assert s.session_start is None


class TestResetMetrics:
    """reset_metrics clears runtime counters without touching config."""

    def test_zeroes_metrics(self):
        s = DeviceSession()
        s.data_rate = 1200
        s.total_lines = 45_000
        s.total_bytes = 2_300_000
        s.session_start = datetime(2026, 1, 1, 10, 0, 0)

        s.reset_metrics()

        assert s.data_rate == 0
        assert s.total_lines == 0
        assert s.total_bytes == 0
        assert s.session_start is None

    def test_preserves_display_preferences(self):
        s = DeviceSession()
        s.color_mode = False
        s.timestamp_mode = TimestampMode.RELATIVE
        s.auto_scroll = False

        s.data_rate = 500
        s.reset_metrics()

        assert s.color_mode is False
        assert s.timestamp_mode == TimestampMode.RELATIVE
        assert s.auto_scroll is False


class TestResetLoggingState:
    """Clears logging fields for a fresh log, leaves connection alone."""

    def test_clears_logging_fields(self):
        s = DeviceSession()
        s.is_logging = True
        s.is_paused = True
        s.log_file_path = "/tmp/log.txt"
        s.csv_file_path = "/tmp/log.csv"
        s.log_name = "MotorTest"
        s.log_comments = "PWM 50%"

        s.reset_logging_state()

        assert s.is_logging is False
        assert s.is_paused is False
        assert s.log_file_path is None
        assert s.csv_file_path is None
        assert s.log_name == ""
        assert s.log_comments == ""

    def test_preserves_connection(self):
        s = DeviceSession()
        s.port_name = "COM3"
        s.baud_rate = 115_200
        s.is_connected = True

        s.reset_logging_state()

        assert s.port_name == "COM3"
        assert s.baud_rate == 115_200
        assert s.is_connected is True


class TestResetConnectionState:
    """Full teardown for disconnect — clears connection, logging, and metrics."""

    def test_clears_connection(self):
        s = DeviceSession()
        s.port_name = "COM3"
        s.is_connected = True

        s.reset_connection_state()

        assert s.port_name == ""
        assert s.is_connected is False

    def test_clears_logging_transitively(self):
        s = DeviceSession()
        s.is_connected = True
        s.is_logging = True
        s.log_file_path = "/tmp/log.txt"

        s.reset_connection_state()

        assert s.is_logging is False
        assert s.log_file_path is None

    def test_clears_metrics_transitively(self):
        s = DeviceSession()
        s.is_connected = True
        s.total_lines = 1000

        s.reset_connection_state()

        assert s.total_lines == 0

    def test_preserves_display_preferences(self):
        s = DeviceSession()
        s.is_connected = True
        s.color_mode = False
        s.timestamp_mode = TimestampMode.RELATIVE

        s.reset_connection_state()

        # Disconnect should NOT reset user's display preferences.
        assert s.color_mode is False
        assert s.timestamp_mode == TimestampMode.RELATIVE
