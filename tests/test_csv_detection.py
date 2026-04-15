# SPDX-License-Identifier: MIT
"""Tests for the CSV export engine.

Includes the regression guard for audit finding C2 (timestamps must be
preserved across the detection sample boundary, not replaced with a
single "current" timestamp).
"""

from __future__ import annotations

import io

from app.constants import CSVMode
from core.csv_engine import CSVEngine


class TestDetection:
    """detect_mode identifies structure in common data patterns."""

    def test_detects_key_value_colon(self):
        engine = CSVEngine()
        lines = [
            "Temperature: 25.3, Humidity: 60",
            "Temperature: 25.5, Humidity: 61",
            "Temperature: 25.6, Humidity: 62",
        ]
        mode = engine.detect_mode(lines)
        assert mode == CSVMode.AUTO
        assert "Temperature" in engine.columns
        assert "Humidity" in engine.columns

    def test_detects_key_value_equals(self):
        engine = CSVEngine()
        lines = [
            "voltage=3.3, current=0.05",
            "voltage=3.4, current=0.06",
        ]
        assert engine.detect_mode(lines) == CSVMode.AUTO

    def test_detects_json(self):
        engine = CSVEngine()
        lines = [
            '{"temp": 25, "hum": 60}',
            '{"temp": 26, "hum": 61}',
            '{"temp": 27, "hum": 62}',
        ]
        assert engine.detect_mode(lines) == CSVMode.AUTO
        assert "temp" in engine.columns
        assert "hum" in engine.columns

    def test_falls_back_to_raw_on_unstructured_data(self):
        engine = CSVEngine()
        lines = [
            "System starting up...",
            "ERROR: something broke",
            "Retrying connection",
            "OK",
        ]
        assert engine.detect_mode(lines) == CSVMode.RAW

    def test_empty_sample_returns_raw(self):
        assert CSVEngine().detect_mode([]) == CSVMode.RAW


class TestTimestampPreservation:
    """Regression guard for audit finding C2.

    Before the fix, every row written during the auto-detection sample
    window had its original timestamp replaced by whichever "current"
    timestamp was passed when the 50th row arrived. These tests lock in
    the correct behavior: every row carries its own original timestamp
    after detection completes.
    """

    def _build_auto_detect_log(self, row_count: int) -> str:
        engine = CSVEngine()
        buf = io.StringIO()
        engine.write_header(buf)  # no-op per the deferred-header design

        timestamps = [
            f"2026-01-01 10:00:{i:02d}.000" for i in range(row_count)
        ]
        for i, ts in enumerate(timestamps):
            engine.write_row(buf, ts, f"Temperature: {20 + i}, Humidity: {50 + i}")
        engine.finalize(buf)

        return buf.getvalue()

    def test_auto_mode_preserves_every_distinct_timestamp(self):
        out = self._build_auto_detect_log(row_count=60)
        for i in range(60):
            expected = f"2026-01-01 10:00:{i:02d}.000"
            assert expected in out, f"missing timestamp: {expected}"

    def test_auto_mode_row_count_matches_input(self):
        out = self._build_auto_detect_log(row_count=60)
        rows = out.strip().split("\n")
        # 1 header + 60 data rows
        assert len(rows) == 61

    def test_auto_mode_header_has_detected_columns(self):
        out = self._build_auto_detect_log(row_count=60)
        header = out.split("\n")[0]
        assert "Timestamp" in header
        assert "Temperature" in header
        assert "Humidity" in header
        # The broken code produced "Timestamp,Data" as header even in AUTO mode.
        assert header != "Timestamp,Data"

    def test_short_log_detected_on_finalize(self):
        """Fewer than sample_size rows: detection runs on stop, not never."""
        engine = CSVEngine()
        buf = io.StringIO()
        engine.write_header(buf)
        engine.write_row(buf, "2026-01-01 10:00:00", "a=1, b=2")
        engine.write_row(buf, "2026-01-01 10:00:01", "a=3, b=4")
        engine.finalize(buf)

        rows = buf.getvalue().strip().split("\n")
        assert len(rows) == 3  # header + 2 data rows
        assert "2026-01-01 10:00:00" in rows[1]
        assert "2026-01-01 10:00:01" in rows[2]

    def test_empty_log_produces_header_only(self):
        engine = CSVEngine()
        buf = io.StringIO()
        engine.write_header(buf)
        engine.finalize(buf)
        # Header should be the RAW default since no samples were seen.
        assert buf.getvalue() == "Timestamp,Data\n"


class TestCsvEscaping:
    """Values containing commas, quotes, or newlines are RFC 4180 escaped."""

    def test_comma_triggers_quotes(self):
        assert CSVEngine._csv_escape("a,b") == '"a,b"'

    def test_quote_is_doubled(self):
        assert CSVEngine._csv_escape('a"b') == '"a""b"'

    def test_plain_value_unchanged(self):
        assert CSVEngine._csv_escape("plain") == "plain"

    def test_newline_triggers_quotes(self):
        assert CSVEngine._csv_escape("a\nb") == '"a\nb"'

    def test_empty_value(self):
        assert CSVEngine._csv_escape("") == ""
