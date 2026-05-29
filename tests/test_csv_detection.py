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
            engine.write_row(
                buf, ts,
                f"Temperature: {20 + i}, Humidity: {50 + i}",
                "DATA",
            )
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
        engine.write_row(buf, "2026-01-01 10:00:00", "a=1, b=2", "DATA")
        engine.write_row(buf, "2026-01-01 10:00:01", "a=3, b=4", "DATA")
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


class TestTagAwareDetection:
    """Tag-aware sampling and writing: severity messages neither
    pollute structure detection nor leak into the structured CSV.

    This is the core fix for the v26.05.10 audit finding where mixed
    streams (boot logs + telemetry, status messages + sensor data)
    collapsed detection to RAW because severity lines polluted the
    column intersection.
    """

    @staticmethod
    def _drive(engine: CSVEngine, buf: io.StringIO,
               rows: list[tuple[str, str, str]]) -> None:
        """Push a list of (timestamp, line, tag) rows through write_row
        and finalize. Mirrors how log_engine feeds the engine."""
        for ts, line, tag in rows:
            engine.write_row(buf, ts, line, tag)
        engine.finalize(buf)

    def test_detects_structure_in_mixed_severity_and_data(self):
        """Boot/severity interleaved with telemetry — detection still
        succeeds because only DATA lines feed the column intersection."""
        engine = CSVEngine()
        buf = io.StringIO()

        rows: list[tuple[str, str, str]] = []
        # 50 lines: every 5th is INFO, every 7th is WARNING, rest DATA.
        # Roughly 36 DATA + 14 severity in a 50-line sample.
        for i in range(50):
            ts = f"2026-01-01 10:00:{i:02d}.000"
            if i % 5 == 0:
                rows.append((ts, f"INFO: heartbeat {i}", "INFO"))
            elif i % 7 == 0:
                rows.append((ts, f"WARNING: condition {i}", "WARNING"))
            else:
                rows.append(
                    (ts, f"Temperature: {20 + i}, Humidity: {50 + i}", "DATA"),
                )

        self._drive(engine, buf, rows)

        # Detection succeeded despite the noise.
        assert engine.mode == CSVMode.AUTO
        assert "Temperature" in engine.columns
        assert "Humidity" in engine.columns

    def test_severity_excluded_from_auto_csv(self):
        """Option C: in AUTO mode, severity rows are not written to CSV
        — neither during the buffered replay nor after detection."""
        engine = CSVEngine()
        buf = io.StringIO()

        rows: list[tuple[str, str, str]] = [
            (f"2026-01-01 10:00:{i:02d}.000",
             f"Temperature: {20 + i}, Humidity: {50 + i}",
             "DATA")
            for i in range(50)
        ]
        # Add severity AFTER detection completes; also add DATA after.
        rows.append(("2026-01-01 10:01:00.000",
                     "ERROR: WiFi disconnected", "ERROR"))
        rows.append(("2026-01-01 10:01:01.000",
                     "Temperature: 99, Humidity: 99", "DATA"))
        rows.append(("2026-01-01 10:01:02.000",
                     "WARNING: low battery", "WARNING"))

        self._drive(engine, buf, rows)
        out = buf.getvalue()

        assert engine.mode == CSVMode.AUTO
        # Severity lines and their timestamps are absent from CSV.
        assert "ERROR: WiFi disconnected" not in out
        assert "WARNING: low battery" not in out
        assert "2026-01-01 10:01:00" not in out
        assert "2026-01-01 10:01:02" not in out
        # The post-detection DATA row IS present.
        assert "2026-01-01 10:01:01" in out

    def test_severity_preserved_in_raw_mode(self):
        """In RAW mode (no structure detected), the CSV is a verbatim
        mirror of the data stream — every row, including severity."""
        engine = CSVEngine()
        buf = io.StringIO()

        # Free-form lines that don't KV/JSON parse → RAW.
        rows: list[tuple[str, str, str]] = [
            (f"2026-01-01 10:00:{i:02d}.000",
             f"free form line {i} with no key value pairs",
             "DATA")
            for i in range(50)
        ]
        rows.append(("2026-01-01 10:01:00.000",
                     "ERROR: something broke", "ERROR"))

        self._drive(engine, buf, rows)
        out = buf.getvalue()

        assert engine.mode == CSVMode.RAW
        # In RAW mode, severity IS written.
        assert "ERROR: something broke" in out
        assert "2026-01-01 10:01:00" in out

    def test_falls_back_to_raw_when_only_severity_in_sample(self):
        """If the entire sample window contains zero DATA lines,
        detection legitimately falls back to RAW. All buffered rows
        (which are all severity) are then written verbatim."""
        engine = CSVEngine()
        buf = io.StringIO()

        rows: list[tuple[str, str, str]] = [
            (f"2026-01-01 10:00:{i:02d}.000",
             f"INFO: status update {i}", "INFO")
            for i in range(50)
        ]

        self._drive(engine, buf, rows)
        out = buf.getvalue()

        assert engine.mode == CSVMode.RAW
        # Severity preserved under RAW.
        assert "INFO: status update 0" in out
        assert "INFO: status update 49" in out

    def test_command_tag_excluded_from_auto_csv(self):
        """User-typed commands (tagged COMMAND, prefixed ">>>") are
        also excluded from the structured CSV under Option C."""
        engine = CSVEngine()
        buf = io.StringIO()

        rows: list[tuple[str, str, str]] = [
            (f"2026-01-01 10:00:{i:02d}.000",
             f"Temperature: {20 + i}, Humidity: {50 + i}",
             "DATA")
            for i in range(50)
        ]
        rows.append(("2026-01-01 10:01:00.000",
                     ">>> reset", "COMMAND"))

        self._drive(engine, buf, rows)
        out = buf.getvalue()

        assert engine.mode == CSVMode.AUTO
        assert ">>> reset" not in out
        assert "2026-01-01 10:01:00" not in out

    def test_auto_csv_is_rectangular(self):
        """Every data row in AUTO mode has exactly the same column count
        as the header. No partial rows, no extra columns, no leakage."""
        engine = CSVEngine()
        buf = io.StringIO()

        rows: list[tuple[str, str, str]] = []
        for i in range(50):
            ts = f"2026-01-01 10:00:{i:02d}.000"
            if i % 4 == 0:
                rows.append((ts, f"WARNING: condition {i}", "WARNING"))
            else:
                rows.append(
                    (ts, f"Temperature: {20 + i}, Humidity: {50 + i}", "DATA"),
                )

        self._drive(engine, buf, rows)

        lines = buf.getvalue().strip().split("\n")
        assert engine.mode == CSVMode.AUTO
        header_cols = lines[0].count(",") + 1
        # Header should be Timestamp,Temperature,Humidity → 3 columns
        assert header_cols == 3
        # Every subsequent row must have the same column count.
        for row in lines[1:]:
            assert row.count(",") + 1 == header_cols, (
                f"non-rectangular row in AUTO CSV: {row!r}"
            )

    def test_unparseable_data_line_skipped_in_auto_mode(self):
        """A DATA-tagged line that doesn't match the detected schema
        (e.g. a boot banner that contains no severity keyword) is
        skipped from the .csv to preserve the rectangular-CSV
        invariant. The .txt log preserves it; only the structured
        export drops it."""
        engine = CSVEngine()
        buf = io.StringIO()

        rows: list[tuple[str, str, str]] = [
            (f"2026-01-01 10:00:{i:02d}.000",
             f"Temperature: {20 + i}, Humidity: {50 + i}",
             "DATA")
            for i in range(50)
        ]
        # Post-detection DATA line that does NOT match the schema
        # (no Temperature/Humidity keys). Must not break rectangularity.
        rows.append(("2026-01-01 10:01:00.000",
                     "Booting firmware v2.3.1", "DATA"))
        # And a normal DATA row after.
        rows.append(("2026-01-01 10:01:01.000",
                     "Temperature: 99, Humidity: 99", "DATA"))

        self._drive(engine, buf, rows)
        out = buf.getvalue()

        assert engine.mode == CSVMode.AUTO
        # The boot banner is absent from CSV.
        assert "Booting firmware" not in out
        assert "2026-01-01 10:01:00" not in out
        # The structured row after it is present.
        assert "2026-01-01 10:01:01" in out
        # All rows in CSV have the same column count as the header.
        lines_out = out.strip().split("\n")
        header_cols = lines_out[0].count(",") + 1
        for ln in lines_out[1:]:
            assert ln.count(",") + 1 == header_cols, (
                f"non-rectangular row: {ln!r}"
            )

    def test_data_timestamps_preserved_when_severity_filtered(self):
        """C2 timestamp invariant: every DATA row's original timestamp
        survives the buffered-replay-with-severity-filtering path.
        The severity rows simply aren't there — the DATA rows are."""
        engine = CSVEngine()
        buf = io.StringIO()

        rows: list[tuple[str, str, str]] = []
        # Alternate DATA / WARNING for 60 rows. 30 DATA, 30 WARNING.
        for i in range(60):
            ts = f"2026-01-01 10:00:{i:02d}.000"
            if i % 2 == 0:
                rows.append(
                    (ts, f"Temperature: {20 + i}, Humidity: {50 + i}", "DATA"),
                )
            else:
                rows.append((ts, f"WARNING: spurious {i}", "WARNING"))

        self._drive(engine, buf, rows)
        out = buf.getvalue()

        assert engine.mode == CSVMode.AUTO
        # All 30 DATA timestamps present; all 30 WARNING timestamps absent.
        for i in range(60):
            ts = f"2026-01-01 10:00:{i:02d}.000"
            if i % 2 == 0:
                assert ts in out, f"missing DATA timestamp: {ts}"
            else:
                assert ts not in out, f"unexpected severity timestamp: {ts}"
