# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the CSV engine: structure detection and row formatting."""

from __future__ import annotations

import csv
import io
import time

import pytest

from app.constants import (
    CSV_AUTODETECT_SAMPLE_SIZE,
    CSV_DETECTION_GIVE_UP_LINES,
    STRUCTURE_MAX_LINE_CHARACTERS,
    CSVMode,
)
from core.csv_engine import CSVEngine

_TS = "2026-01-01 10:00:00.000"

# Generous for a slow shared runner, and far below what a pattern whose
# cost grows with the square of the line length would take.
_PROMPT_SECONDS = 2.0


def _stamp(index: int) -> str:
    return f"2026-01-01 10:{index // 60:02d}:{index % 60:02d}.000"


def _run(lines: list[str]) -> tuple[CSVEngine, list[str]]:
    """Feed lines through a fresh engine and return it with every row."""
    engine = CSVEngine()
    rows: list[str] = []
    for index, line in enumerate(lines):
        rows.extend(engine.feed(_stamp(index), line))
    rows.extend(engine.finalize())
    return engine, rows


def _parse(header: str, rows: list[str]) -> list[list[str]]:
    return list(csv.reader(io.StringIO("\n".join([header, *rows]))))


class TestDetection:
    def test_detects_key_value_colon(self):
        engine = CSVEngine()
        lines = [f"Temperature: {20 + i}, Humidity: {50 + i}" for i in range(10)]
        assert engine.detect_mode(lines) == CSVMode.AUTO
        assert engine.columns == ["Temperature", "Humidity"]
        assert engine.header == "Timestamp,Temperature,Humidity"

    def test_detects_key_value_equals(self):
        engine = CSVEngine()
        assert engine.detect_mode([f"v={i} i={i}" for i in range(10)]) == CSVMode.AUTO
        assert engine.columns == ["v", "i"]

    def test_detects_json(self):
        engine = CSVEngine()
        lines = [f'{{"temp": {i}, "state": "run"}}' for i in range(10)]
        assert engine.detect_mode(lines) == CSVMode.AUTO
        assert engine.columns == ["temp", "state"]

    def test_falls_back_to_raw_on_unstructured_data(self):
        engine = CSVEngine()
        assert engine.detect_mode([f"hello world {i}" for i in range(10)]) == CSVMode.RAW
        assert engine.columns == []
        assert engine.header == "Timestamp,Data"

    def test_empty_sample_is_raw(self):
        engine = CSVEngine()
        assert engine.detect_mode([]) == CSVMode.RAW
        assert engine.detection_complete

    def test_clock_time_is_not_read_as_a_key(self):
        engine = CSVEngine()
        lines = [f"12:30:{i:02d} temp={20 + i} rpm={1000 + i}" for i in range(10)]
        assert engine.detect_mode(lines) == CSVMode.AUTO
        assert engine.columns == ["temp", "rpm"]

    def test_occasional_odd_line_does_not_collapse_columns(self):
        lines = [f"Temp: {i}, Hum: {i}" for i in range(45)]
        lines[10] = "Temp: 5"
        lines[20] = "status: ok"
        engine = CSVEngine()
        assert engine.detect_mode(lines) == CSVMode.AUTO
        assert engine.columns == ["Temp", "Hum"]


class TestVeryLongLines:
    def test_pairs_are_looked_for_only_at_the_start_of_a_line(self):
        engine = CSVEngine()
        padding = "x " * (STRUCTURE_MAX_LINE_CHARACTERS // 2)
        lines = [f"T: {i}, H: {i}, {padding} late: {i}" for i in range(10)]

        assert engine.detect_mode(lines) == CSVMode.AUTO
        assert engine.columns == ["T", "H"]

    def test_a_long_line_of_near_keys_is_read_promptly(self):
        engine = CSVEngine()
        engine.detect_mode([f"T: {i}, H: {i}" for i in range(10)])
        line = "a " * 32_768
        started = time.perf_counter()
        # Unbounded, each such line takes about 0.1 s; bounded, under 0.01 s.
        for _ in range(50):
            engine.feed(_TS, line)
        assert time.perf_counter() - started < _PROMPT_SECONDS


class TestHoldUntilDetected:
    def test_nothing_is_returned_before_the_first_sample_completes(self):
        engine = CSVEngine()
        for index in range(CSV_AUTODETECT_SAMPLE_SIZE - 1):
            assert engine.feed(_stamp(index), f"a={index}, b={index}") == []
        assert not engine.detection_complete

    def test_held_rows_are_released_with_their_own_timestamps(self):
        engine = CSVEngine()
        rows: list[str] = []
        for index in range(CSV_AUTODETECT_SAMPLE_SIZE):
            rows.extend(engine.feed(_stamp(index), f"a={index}, b={index}"))
        assert engine.mode == CSVMode.AUTO
        assert len(rows) == CSV_AUTODETECT_SAMPLE_SIZE
        for index, row in enumerate(rows):
            assert row == f"{_stamp(index)},{index},{index}"

    def test_rows_after_detection_are_returned_immediately(self):
        engine = CSVEngine()
        for index in range(CSV_AUTODETECT_SAMPLE_SIZE):
            engine.feed(_stamp(index), f"a={index}, b={index}")
        assert engine.feed("T", "a=7, b=8") == ["T,7,8"]

    def test_short_session_is_detected_on_finalize(self):
        engine, rows = _run(["a=1, b=2", "a=3, b=4"])
        assert engine.mode == CSVMode.AUTO
        assert rows == [f"{_stamp(0)},1,2", f"{_stamp(1)},3,4"]

    def test_empty_session_finalizes_to_raw_with_no_rows(self):
        engine, rows = _run([])
        assert engine.mode == CSVMode.RAW
        assert rows == []
        assert engine.header == "Timestamp,Data"

    def test_finalize_after_detection_returns_nothing(self):
        engine = CSVEngine()
        engine.detect_mode(["a=1"])
        assert engine.finalize() == []

    def test_boot_log_before_readings_is_still_detected(self):
        boot = [f"booting stage {i}" for i in range(60)]
        readings = [f"Temp: {i}, Hum: {i}" for i in range(140)]
        engine, rows = _run(boot + readings)
        assert engine.mode == CSVMode.AUTO
        assert engine.columns == ["Temp", "Hum"]
        assert len(rows) == len(readings)

    def test_gives_up_to_raw_and_keeps_every_line(self):
        lines = [f"free text {i}" for i in range(CSV_DETECTION_GIVE_UP_LINES + 5)]
        engine, rows = _run(lines)
        assert engine.mode == CSVMode.RAW
        assert rows == [f"{_stamp(i)},{line}" for i, line in enumerate(lines)]


class TestRowsAreChosenByContent:
    """A line is exported if it fits the columns, whatever words it contains."""

    def test_readings_containing_a_severity_word_are_exported(self):
        lines = [f"errors={i} temp=22.{i % 10} rpm=1200" for i in range(60)]
        engine, rows = _run(lines)
        assert engine.mode == CSVMode.AUTO
        assert engine.columns == ["errors", "temp", "rpm"]
        assert len(rows) == 60

    def test_json_with_a_status_word_is_exported(self):
        lines = [f'{{"temp": {i}, "status": "ok"}}' for i in range(60)]
        engine, rows = _run(lines)
        assert engine.columns == ["temp", "status"]
        assert rows[0] == f"{_stamp(0)},0,ok"

    def test_lines_that_do_not_fit_the_columns_are_left_out(self):
        lines = [f"Temp: {i}, Hum: {i}, Pres: {i}" for i in range(60)]
        lines[30] = "ERROR: sensor timeout"
        lines[40] = "WARNING: Temp: 99 exceeds limit"
        engine, rows = _run(lines)
        assert engine.columns == ["Temp", "Hum", "Pres"]
        assert len(rows) == 58
        assert all("ERROR" not in row and "WARNING" not in row for row in rows)

    def test_row_missing_a_minority_of_columns_is_kept_with_blanks(self):
        lines = [f"Temp: {i}, Hum: {i}, Pres: {i}" for i in range(60)]
        lines[55] = "Temp: 7, Hum: 8"
        engine, rows = _run(lines)
        parsed = _parse(engine.header, rows)
        assert parsed[56] == [_stamp(55), "7", "8", ""]

    def test_auto_output_is_rectangular(self):
        lines = [f"Temp: {i}, Hum: {i}" for i in range(60)]
        lines[5] = "Temp: 1"
        engine, rows = _run(lines)
        parsed = _parse(engine.header, rows)
        assert {len(row) for row in parsed} == {3}


class TestRawMode:
    def test_every_line_becomes_a_row(self):
        engine = CSVEngine()
        engine.detect_mode([])
        assert engine.feed("T", "anything at all") == ["T,anything at all"]

    def test_blank_line_is_an_empty_cell(self):
        engine = CSVEngine()
        engine.detect_mode([])
        assert engine.feed("T", "") == ["T,"]


class TestCsvEscaping:
    def test_comma_triggers_quotes(self):
        assert CSVEngine._csv_escape("a,b") == '"a,b"'

    def test_quote_is_doubled(self):
        assert CSVEngine._csv_escape('say "hi"') == '"say ""hi"""'

    def test_plain_value_unchanged(self):
        assert CSVEngine._csv_escape("22.4") == "22.4"

    def test_newline_triggers_quotes(self):
        assert CSVEngine._csv_escape("a\nb") == '"a\nb"'

    def test_empty_value(self):
        assert CSVEngine._csv_escape("") == ""


class TestHeader:
    def test_column_names_are_escaped_like_values(self):
        engine = CSVEngine()
        lines = ['{"a,b": 1, "=cmd": 2}'] * 10
        assert engine.detect_mode(lines) == CSVMode.AUTO
        assert engine.header == 'Timestamp,"a,b",\'=cmd'

    def test_key_repeated_on_a_line_is_one_column(self):
        engine = CSVEngine()
        assert engine.detect_mode(["t=1 t=2 u=3"] * 10) == CSVMode.AUTO
        assert engine.columns == ["t", "u"]
        assert engine.header == "Timestamp,t,u"


class TestHostileLines:
    """A device can send anything. The time spent on a line must grow
    with its length, not with its square, and no line may raise."""

    @pytest.mark.parametrize(
        "line",
        [
            # Named, because the test's name otherwise contains the
            # whole line, which is longer than Windows allows in the
            # environment variable pytest keeps the name in.
            pytest.param("A" * 65_536, id="one-long-word"),
            pytest.param("ab " * 21_000, id="many-short-words"),
            pytest.param("a_" * 32_000, id="underscores"),
            pytest.param(" " * 65_536, id="only-spaces"),
            pytest.param("k" + " " * 60_000 + ": 5", id="key-then-long-gap"),
        ],
    )
    def test_long_line_without_pairs_is_handled_promptly(self, line):
        engine = CSVEngine()
        engine.detect_mode(["temp: 1, rpm: 2"] * 10)
        started = time.perf_counter()
        engine.feed(_TS, line)
        assert time.perf_counter() - started < _PROMPT_SECONDS

    def test_deeply_nested_json_is_not_a_row_and_does_not_raise(self):
        engine = CSVEngine()
        engine.detect_mode(['{"temp": 1, "rpm": 2}'] * 10)
        assert engine.feed(_TS, '{"a":' + "[" * 60_000) == []
