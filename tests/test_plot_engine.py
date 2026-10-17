# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the live-plot parser pipeline and engine.

These tests cover the pure-data layer only: no Qt, no GUI. The
PlotEngine subscribes to Qt signals at runtime, but its state machine
(detection, ring buffer, schema-change behaviour) is exercised here
by calling ``process()`` directly and reading back via ``snapshot()``.
"""

from __future__ import annotations

import time

import numpy as np

from core.plot_engine import (
    PLOT_AUTODETECT_SAMPLE_SIZE,
    PLOT_DETECTION_GIVE_UP_LINES,
    PLOT_MAX_SERIES,
    PLOT_RING_BUFFER_SIZE,
    PlotEngine,
)
from core.plot_parsers import (
    DelimitedParser,
    JsonParser,
    KvParser,
    ParserPipeline,
)
from tests.clock import TickClock

_tick = TickClock()

# ─────────────────────────────────────────────────────────────────────────────
# JsonParser
# ─────────────────────────────────────────────────────────────────────────────

class TestJsonParser:
    """JSON object lines → numeric column detection."""

    def test_detects_simple_json(self):
        p = JsonParser()
        sample = [f'{{"temp": {25 + i}, "hum": {55 + i}}}' for i in range(10)]
        cols = p.detect(sample)
        assert cols == ["temp", "hum"]

    def test_extract_returns_floats(self):
        p = JsonParser()
        p.detect([f'{{"temp": {25 + i}, "hum": {55 + i}}}' for i in range(5)])
        out = p.extract('{"temp": 26, "hum": 60}')
        assert out == {"temp": 26.0, "hum": 60.0}

    def test_string_numerics_accepted(self):
        """Numeric values encoded as JSON strings should still parse."""
        p = JsonParser()
        sample = [f'{{"temp": "{25 + i}.5"}}' for i in range(5)]
        assert p.detect(sample) == ["temp"]
        assert p.extract('{"temp": "27.5"}') == {"temp": 27.5}

    def test_booleans_excluded(self):
        """JSON true/false are not plottable signals."""
        p = JsonParser()
        sample = [
            '{"temp": 25, "active": true}',
            '{"temp": 26, "active": false}',
            '{"temp": 27, "active": true}',
        ]
        # active should NOT appear in detected columns even though
        # it's int-compatible at the Python layer.
        cols = p.detect(sample)
        assert cols == ["temp"]
        assert "active" not in cols

    def test_intersection_across_lines(self):
        """Only keys present in EVERY parsed line survive."""
        p = JsonParser()
        sample = [
            '{"a": 1, "b": 2, "c": 3}',
            '{"a": 1, "b": 2}',         # missing c
            '{"a": 1, "b": 2, "c": 5}',
        ]
        cols = p.detect(sample)
        assert cols == ["a", "b"]

    def test_returns_none_when_below_threshold(self):
        """If fewer than 50% of lines are JSON, detection fails."""
        p = JsonParser()
        sample = [
            '{"a": 1}',
            "free form text",
            "more free form",
            "even more free form",
        ]
        assert p.detect(sample) is None

    def test_extract_returns_none_for_non_json(self):
        p = JsonParser()
        p.detect([f'{{"a": {i}}}' for i in range(5)])
        assert p.extract("not json at all") is None


# ─────────────────────────────────────────────────────────────────────────────
# KvParser
# ─────────────────────────────────────────────────────────────────────────────

class TestKvParser:
    """Key:value / key=value detection."""

    def test_detects_colon_separator(self):
        p = KvParser()
        sample = [f"Temp: {25 + i}, Hum: {55 + i}" for i in range(10)]
        assert p.detect(sample) == ["Temp", "Hum"]

    def test_detects_equals_separator(self):
        p = KvParser()
        sample = [f"voltage={3.3 + i * 0.1}, current={0.05 + i * 0.01}"
                  for i in range(10)]
        cols = p.detect(sample)
        assert "voltage" in cols
        assert "current" in cols

    def test_extract_numeric_values(self):
        p = KvParser()
        p.detect([f"T: {25 + i}, H: {55 + i}" for i in range(5)])
        out = p.extract("T: 27, H: 58")
        assert out == {"T": 27.0, "H": 58.0}

    def test_intersection_only(self):
        p = KvParser()
        sample = [
            "T: 25, H: 55, P: 1013",
            "T: 26, H: 56",            # missing P
            "T: 27, H: 57, P: 1014",
        ]
        cols = p.detect(sample)
        assert "T" in cols and "H" in cols
        assert "P" not in cols


# ─────────────────────────────────────────────────────────────────────────────
# DelimitedParser
# ─────────────────────────────────────────────────────────────────────────────

class TestDelimitedParser:
    """Positional values with auto-detected delimiter."""

    def test_detects_comma(self):
        p = DelimitedParser()
        sample = [f"{25 + i},{55 + i},{1013 + i}" for i in range(10)]
        cols = p.detect(sample)
        assert cols == ["ch0", "ch1", "ch2"]

    def test_detects_tab(self):
        p = DelimitedParser()
        sample = [f"{25 + i}\t{55 + i}\t{1013 + i}" for i in range(10)]
        cols = p.detect(sample)
        assert cols == ["ch0", "ch1", "ch2"]

    def test_detects_semicolon(self):
        p = DelimitedParser()
        sample = [f"{25 + i};{55 + i};{1013 + i}" for i in range(10)]
        assert p.detect(sample) == ["ch0", "ch1", "ch2"]

    def test_detects_pipe(self):
        p = DelimitedParser()
        sample = [f"{25 + i}|{55 + i}|{1013 + i}" for i in range(10)]
        assert p.detect(sample) == ["ch0", "ch1", "ch2"]

    def test_detects_whitespace(self):
        p = DelimitedParser()
        sample = [f"{25 + i} {55 + i} {1013 + i}" for i in range(10)]
        assert p.detect(sample) == ["ch0", "ch1", "ch2"]

    def test_explicit_delimiter_beats_whitespace_on_ties(self):
        """If a line could split on either comma or whitespace, comma wins."""
        p = DelimitedParser()
        # Lines with explicit comma: whitespace splitting would also work
        sample = [f"{25 + i}, {55 + i}, {1013 + i}" for i in range(10)]
        p.detect(sample)
        assert p._delimiter == ","

    def test_rejects_non_numeric(self):
        p = DelimitedParser()
        sample = ["a,b,c", "d,e,f", "g,h,i"]
        assert p.detect(sample) is None

    def test_set_column_names_renames_when_count_matches(self):
        p = DelimitedParser()
        p.detect([f"{i},{i + 1},{i + 2}" for i in range(10)])
        p.set_column_names(["x", "y", "z"])
        assert p.columns == ["x", "y", "z"]
        out = p.extract("1,2,3")
        assert out == {"x": 1.0, "y": 2.0, "z": 3.0}

    def test_set_column_names_ignored_on_count_mismatch(self):
        p = DelimitedParser()
        p.detect([f"{i},{i + 1},{i + 2}" for i in range(10)])
        p.set_column_names(["x", "y"])  # wrong count
        assert p.columns == ["ch0", "ch1", "ch2"]

    def test_extract_rejects_wrong_field_count(self):
        p = DelimitedParser()
        p.detect([f"{i},{i + 1},{i + 2}" for i in range(10)])
        assert p.extract("1,2") is None  # too few
        assert p.extract("1,2,3,4") is None  # too many

    def test_extract_rejects_empty_token(self):
        """Missing values ``25,,1013`` are rejected (no plot point)."""
        p = DelimitedParser()
        p.detect([f"{i},{i + 1},{i + 2}" for i in range(10)])
        assert p.extract("25,,1013") is None


# ─────────────────────────────────────────────────────────────────────────────
# ParserPipeline: auto mode
# ─────────────────────────────────────────────────────────────────────────────

class TestPipelineAutoMode:
    """No prefix, no plt!: pipeline picks the best parser."""

    def test_picks_json_when_json_present(self):
        p = ParserPipeline()
        sample = [f'{{"a": {i}, "b": {i + 1}}}' for i in range(10)]
        cols = p.detect(sample)
        assert cols == ["a", "b"]
        assert p.active_parser_name == "json"
        assert p.prefix_mode is False

    def test_picks_kv_when_no_json(self):
        p = ParserPipeline()
        sample = [f"T: {i}, H: {i + 1}" for i in range(10)]
        cols = p.detect(sample)
        assert "T" in cols and "H" in cols
        assert p.active_parser_name == "kv"

    def test_picks_delimited_when_no_kv_no_json(self):
        p = ParserPipeline()
        sample = [f"{i},{i + 1},{i + 2}" for i in range(10)]
        cols = p.detect(sample)
        assert cols == ["ch0", "ch1", "ch2"]
        assert p.active_parser_name == "delimited"

    def test_returns_none_for_unstructured_data(self):
        p = ParserPipeline()
        sample = ["hello world", "boot complete", "ready for commands"] * 5
        assert p.detect(sample) is None

    def test_extract_uses_active_parser(self):
        p = ParserPipeline()
        p.detect([f"T: {i}, H: {i + 1}" for i in range(10)])
        assert p.extract("T: 99, H: 88") == {"T": 99.0, "H": 88.0}


# ─────────────────────────────────────────────────────────────────────────────
# ParserPipeline: prefix mode
# ─────────────────────────────────────────────────────────────────────────────

class TestPipelinePrefixMode:
    """Lines prefixed with ``plt:`` are routed; others are ignored."""

    def test_prefix_mode_activates_on_any_prefixed_line(self):
        p = ParserPipeline()
        sample = [
            "INFO: heartbeat",
            "WARNING: noise",
            *[f"plt:{i},{i + 1},{i + 2}" for i in range(10)],
            "more noise",
        ]
        cols = p.detect(sample)
        assert cols == ["ch0", "ch1", "ch2"]
        assert p.prefix_mode is True

    def test_unprefixed_lines_ignored_in_prefix_mode(self):
        p = ParserPipeline()
        p.detect([f"plt:{i},{i + 1}" for i in range(10)])
        # Unprefixed line should not produce data
        assert p.extract("99,88") is None

    def test_prefixed_line_strips_prefix_for_extraction(self):
        p = ParserPipeline()
        p.detect([f"plt:{i},{i + 1}" for i in range(10)])
        out = p.extract("plt:42,43")
        assert out == {"ch0": 42.0, "ch1": 43.0}

    def test_numbered_prefixes_route_to_same_pipeline_in_v1(self):
        """``plt0:`` and ``plt1:`` both feed the same pipeline (v1)."""
        p = ParserPipeline()
        sample = [f"plt0:{i},{i + 1}" for i in range(5)] + \
                 [f"plt1:{i},{i + 1}" for i in range(5)]
        cols = p.detect(sample)
        assert cols == ["ch0", "ch1"]
        out0 = p.extract("plt0:1,2")
        out1 = p.extract("plt1:3,4")
        assert out0 == {"ch0": 1.0, "ch1": 2.0}
        assert out1 == {"ch0": 3.0, "ch1": 4.0}

    def test_plt_config_line_overrides_column_names(self):
        p = ParserPipeline()
        sample = [
            "plt!Voltage,Current,Power",
            *[f"plt:{3.3 + i * 0.1:.2f},{0.5 + i * 0.05:.2f},{1.65 + i * 0.1:.2f}"
              for i in range(10)],
        ]
        cols = p.detect(sample)
        assert cols == ["Voltage", "Current", "Power"]
        out = p.extract("plt:3.50,0.65,2.275")
        assert out == {"Voltage": 3.5, "Current": 0.65, "Power": 2.275}

    def test_plt_config_with_json_payload(self):
        """Prefix protocol works with JSON content too."""
        p = ParserPipeline()
        sample = [f'plt:{{"a": {i}, "b": {i + 1}}}' for i in range(10)]
        cols = p.detect(sample)
        assert cols == ["a", "b"]
        assert p.active_parser_name == "json"
        out = p.extract('plt:{"a": 99, "b": 100}')
        assert out == {"a": 99.0, "b": 100.0}

    def test_config_line_after_detection_returns_none(self):
        p = ParserPipeline()
        p.detect([f"plt:{i},{i + 1}" for i in range(10)])
        assert p.extract("plt!New,Names") is None


# ─────────────────────────────────────────────────────────────────────────────
# PlotEngine: detection state machine
# ─────────────────────────────────────────────────────────────────────────────

class TestPlotEngineDetection:
    """Engine-level detection: sample buffering, hard cap, replay."""

    def test_detection_runs_at_sample_size(self):
        eng = PlotEngine()
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"T: {i}, H: {i + 1}", _tick())
        assert eng.detection_complete
        assert "T" in eng.columns
        assert "H" in eng.columns

    def test_lines_are_chosen_by_content_not_by_wording(self):
        """Readings that contain a severity word are still plotted."""
        eng = PlotEngine()
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"errors={i} temp={20 + i}", _tick())
        assert eng.detection_complete
        assert eng.columns == ["errors", "temp"]

    def test_a_device_naming_hundreds_of_values_gets_only_the_most_the_plot_follows(self):
        eng = PlotEngine()
        line = ", ".join(f"k{n:03d}={n}" for n in range(300))
        for _ in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(line, _tick())
        assert eng.detection_complete
        assert len(eng.columns) == PLOT_MAX_SERIES

    def test_lines_without_values_do_not_detect(self):
        eng = PlotEngine()
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f">>> set mode {'abc'[i % 3]}", _tick())
        assert not eng.detection_complete

    def test_buffered_lines_replayed_after_detection(self):
        """Lines collected during the sample window are reflected in
        the ring buffer after detection completes."""
        eng = PlotEngine()
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"T: {i}", _tick())
        x, y = eng.snapshot("T")
        # Every parseable sample line should have produced a point
        assert len(x) == PLOT_AUTODETECT_SAMPLE_SIZE
        assert len(y) == PLOT_AUTODETECT_SAMPLE_SIZE
        # Values should be 0..49 in order
        np.testing.assert_array_equal(y, np.arange(PLOT_AUTODETECT_SAMPLE_SIZE,
                                                   dtype=np.float64))

    def test_post_detection_lines_appended(self):
        eng = PlotEngine()
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"T: {i}", _tick())
        # 10 more lines after detection
        for i in range(10):
            eng.process(f"T: {100 + i}", _tick())
        _x, y = eng.snapshot("T")
        assert len(y) == PLOT_AUTODETECT_SAMPLE_SIZE + 10
        # Last 10 values are 100..109
        np.testing.assert_array_equal(
            y[-10:], np.arange(100, 110, dtype=np.float64),
        )

    def test_detection_failed_after_hard_cap(self):
        """Unstructured stream gives up cleanly."""
        eng = PlotEngine()
        for i in range(PLOT_DETECTION_GIVE_UP_LINES + 5):
            eng.process(f"hello world line {i}", _tick())
        assert not eng.detection_complete
        assert eng.detection_gave_up

    def test_after_give_up_no_more_processing(self):
        eng = PlotEngine()
        for i in range(PLOT_DETECTION_GIVE_UP_LINES):
            eng.process(f"unstructured {i}", _tick())
        # Now feed valid KV: engine must NOT come back to life mid-session.
        for i in range(20):
            eng.process(f"T: {i}", _tick())
        assert not eng.detection_complete
        assert eng.snapshot("T")[0].size == 0


# ─────────────────────────────────────────────────────────────────────────────
# PlotEngine: ring buffer behaviour
# ─────────────────────────────────────────────────────────────────────────────

class TestPlotEngineRingBuffer:
    """Wraparound, snapshot ordering, buffer size."""

    def test_snapshot_oldest_first_before_wrap(self):
        eng = PlotEngine()
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"T: {i}", _tick())
        _x, y = eng.snapshot("T")
        # Values strictly increasing (oldest first)
        assert np.all(np.diff(y) > 0)

    def test_buffer_wraparound_evicts_oldest(self):
        eng = PlotEngine()
        # Push enough samples to wrap the ring buffer
        total = PLOT_RING_BUFFER_SIZE + 100
        for i in range(total):
            eng.process(f"T: {i}", _tick())
        _x, y = eng.snapshot("T")
        # Buffer holds exactly RING_BUFFER_SIZE most-recent samples
        assert len(y) == PLOT_RING_BUFFER_SIZE
        # Values are the LATEST RING_BUFFER_SIZE values, ordered
        assert y[0] == float(total - PLOT_RING_BUFFER_SIZE)
        assert y[-1] == float(total - 1)

    def test_snapshot_returns_copies(self):
        """Mutating the snapshot must not corrupt engine state."""
        eng = PlotEngine()
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"T: {i}", _tick())
        _x, y = eng.snapshot("T")
        y[0] = 9999.0
        _x2, y2 = eng.snapshot("T")
        assert y2[0] != 9999.0


# ─────────────────────────────────────────────────────────────────────────────
# PlotEngine: robustness
# ─────────────────────────────────────────────────────────────────────────────

class TestPlotEngineRobustness:
    """Schema changes, malformed lines, severity bursts mid-stream."""

    def test_unstructured_lines_mixed_in_do_not_break_detection(self):
        """Occasional lines without values don't stop detection."""
        eng = PlotEngine()
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"T: {i}, H: {i + 1}", _tick())
            if i % 5 == 0:
                eng.process("WARNING: noise", _tick())
        assert eng.detection_complete
        assert eng.columns == ["T", "H"]

    def test_schema_change_unknown_columns_ignored(self):
        """A new column appearing mid-stream is silently ignored."""
        eng = PlotEngine()
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"T: {i}, H: {i + 1}", _tick())
        # Now a line with a new "P" column: should not crash, P ignored
        eng.process("T: 99, H: 88, P: 1013", _tick())
        _x_t, y_t = eng.snapshot("T")
        # Last T value is 99: line was parsed, P just ignored
        assert y_t[-1] == 99.0
        x_p, _y_p = eng.snapshot("P")
        assert x_p.size == 0  # no buffer for P

    def test_missing_columns_partial_extraction(self):
        """Lines with only some detected columns produce partial data."""
        eng = PlotEngine()
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"T: {i}, H: {i + 1}", _tick())
        eng.process("T: 99", _tick())  # missing H: only T appended
        _x_t, y_t = eng.snapshot("T")
        _x_h, y_h = eng.snapshot("H")
        assert y_t[-1] == 99.0
        # H got nothing new; its last value is from the sample replay
        assert len(y_h) == PLOT_AUTODETECT_SAMPLE_SIZE

    def test_non_numeric_value_rejects_line(self):
        """Once detected as numeric KV, a non-numeric value is ignored."""
        eng = PlotEngine()
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"T: {i}", _tick())
        _before_x, before_y = eng.snapshot("T")
        eng.process("T: not_a_number", _tick())
        _after_x, after_y = eng.snapshot("T")
        # No new sample appended
        assert len(after_y) == len(before_y)

    def test_reset_returns_engine_to_clean_state(self):
        eng = PlotEngine()
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"T: {i}", _tick())
        eng.reset()
        assert not eng.detection_complete
        assert eng.columns == []
        assert eng.snapshot("T")[0].size == 0
        # Re-detection works after reset
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"V: {i}", _tick())
        assert eng.detection_complete
        assert "V" in eng.columns

    def test_clear_buffers_keeps_columns(self):
        eng = PlotEngine()
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"T: {i}", _tick())
        cols_before = eng.columns
        eng.clear_buffers()
        assert eng.columns == cols_before  # detection retained
        assert eng.snapshot("T")[0].size == 0  # but buffers empty

    def test_lines_that_do_not_parse_add_no_samples(self):
        eng = PlotEngine()
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"T: {i}", _tick())
        eng.process("ERROR: sensor timeout", _tick())
        eng.process(">>> status", _tick())
        assert eng.snapshot("T")[0].size == PLOT_AUTODETECT_SAMPLE_SIZE


# ─────────────────────────────────────────────────────────────────────────────
# PlotEngine: prefix protocol end-to-end
# ─────────────────────────────────────────────────────────────────────────────

class TestPlotEnginePrefixProtocol:
    """End-to-end: prefix mode + plt! config + tag filtering."""

    def test_prefix_protocol_with_named_columns(self):
        eng = PlotEngine()
        # 1 config line + 50 prefixed data lines + assorted noise
        eng.process("plt!Voltage,Current,Power", _tick())
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"plt:{3.3 + i * 0.01:.3f},"
                        f"{0.5 + i * 0.005:.3f},"
                        f"{1.65 + i * 0.01:.3f}", _tick())
            if i % 7 == 0:
                eng.process("INFO: heartbeat", _tick())
        assert eng.detection_complete
        assert eng.columns == ["Voltage", "Current", "Power"]
        x, _y = eng.snapshot("Voltage")
        assert x.size > 0

    def test_unprefixed_lines_ignored_in_prefix_mode(self):
        eng = PlotEngine()
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"plt:{i},{i + 1}", _tick())
        # Unprefixed line: must be ignored even though it's structured
        eng.process("99,100", _tick())
        _x, y = eng.snapshot("ch0")
        # No new sample after the 50 from sample replay
        assert len(y) == PLOT_AUTODETECT_SAMPLE_SIZE


# ─────────────────────────────────────────────────────────────────────────────
# PlotEngine: realistic mixed streams
# ─────────────────────────────────────────────────────────────────────────────

class TestPlotEngineRealWorld:
    """Realistic streams that mix readings with other lines.

    Boot banners, debug lines and stray colons produce key-value shaped
    tokens that are not in the readings. Detection keeps a column only
    if it appears in most of the matching sample lines, so those tokens
    do not become columns and no real column is lost.
    """

    def test_esp32_boot_then_readings(self):
        """ESP32-style boot banner followed by KV readings must
        detect the columns of the readings and ignore the boot lines."""
        eng = PlotEngine()
        boot = [
            "ets Jul 29 2019 12:21:46",
            "rst:0x10 (RTCWDT_RTC_RESET)",
            "configsip: 0",
            "load:0x3fff0030,len:1184",
        ]
        for line in boot:
            eng.process(line, _tick())
        for i in range(50):
            eng.process(
                f"Temperature: {23.4 + i * 0.05:.2f}, "
                f"Humidity: {51.2 + i * 0.1:.2f}, "
                f"Pressure: {1011.0 + i * 0.02:.2f}",
                _tick(),
            )
        assert eng.detection_complete
        assert eng.columns == ["Temperature", "Humidity", "Pressure"]
        # None of the boot keys should leak into the columns
        for noise_key in ("rst", "configsip", "load", "len"):
            assert noise_key not in eng.columns

    def test_sporadic_dropouts_keep_column(self):
        """A column missing from a few lines of readings (sensor glitch)
        is still detected if it appears in >= 70% of matching lines."""
        eng = PlotEngine()
        # 50 lines, 45 with all 3 keys, 5 with only 2 (P at 90%)
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            if i % 10 == 0:  # 5/50 lines drop P
                eng.process(f"T: {i}, H: {i + 1}", _tick())
            else:
                eng.process(f"T: {i}, H: {i + 1}, P: {1000 + i}", _tick())
        assert eng.detection_complete
        assert "T" in eng.columns
        assert "H" in eng.columns
        assert "P" in eng.columns  # 90% > 70% threshold

    def test_borderline_dropouts_excluded(self):
        """A column appearing in only ~50% of lines is below the 70%
        threshold and excluded: keeps the plot focused on stable
        signals."""
        eng = PlotEngine()
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            if i % 2 == 0:  # P appears in 50% of lines
                eng.process(f"T: {i}, H: {i + 1}, P: {1000 + i}", _tick())
            else:
                eng.process(f"T: {i}, H: {i + 1}", _tick())
        assert eng.detection_complete
        assert "T" in eng.columns
        assert "H" in eng.columns
        assert "P" not in eng.columns  # 50% < 70%


# ─────────────────────────────────────────────────────────────────────────────
# PlotEngine: latest_x semantics
# ─────────────────────────────────────────────────────────────────────────────

class TestPlotEngineLatestX:
    """latest_x must follow the data, not the wall clock.

    ``latest_x`` is the X of the most recently appended sample; it
    only moves when data moves, so the view does not drift into empty
    space when a stream pauses or the device disconnects.
    """

    def test_latest_x_starts_at_zero(self):
        eng = PlotEngine()
        assert eng.latest_x == 0.0

    def test_latest_x_advances_with_data(self):
        eng = PlotEngine()
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"T: {i}", _tick())
        x_after_initial = eng.latest_x
        assert x_after_initial > 0.0

        # Push more data; latest_x should advance further
        for i in range(20):
            eng.process(f"T: {100 + i}", _tick())
        assert eng.latest_x >= x_after_initial

    def test_latest_x_does_not_drift_when_idle(self):
        """No new ``process`` calls means no advance in latest_x:
        even if real wall-clock time passes."""
        eng = PlotEngine()
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"T: {i}", _tick())
        snapshot_latest = eng.latest_x

        # Sleep briefly: latest_x must not budge.
        time.sleep(0.05)
        assert eng.latest_x == snapshot_latest

    def test_replay_preserves_individual_timestamps(self):
        """Buffered samples must replay at their ORIGINAL timestamps,
        not all clustered at the detection moment, which would make
        the plot autorange to a microscopic X cluster."""
        eng = PlotEngine()
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"T: {i}", _tick())
        x, _ = eng.snapshot("T")
        # X values must be strictly increasing: proves each buffered
        # sample got its OWN timestamp, not all the same replay value.
        assert np.all(np.diff(x) > 0), \
            "buffered-sample replay collapsed timestamps"

    def test_clear_buffers_resets_latest_x(self):
        eng = PlotEngine()
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"T: {i}", _tick())
        assert eng.latest_x > 0.0
        eng.clear_buffers()
        assert eng.latest_x == 0.0

    def test_has_any_data_flag(self):
        eng = PlotEngine()
        assert not eng.has_any_data
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"T: {i}", _tick())
        assert eng.has_any_data
        eng.clear_buffers()
        assert not eng.has_any_data

    def test_x_is_seconds_since_the_first_plotted_line(self):
        from datetime import datetime, timedelta

        eng = PlotEngine()
        eng.set_manual_config(r"T:\s*(?P<T>\d+)")
        start = datetime(2026, 3, 4, 5, 6, 7)
        for i in range(4):
            eng.process(f"T: {i}", start + timedelta(milliseconds=250 * i))
        x, y = eng.snapshot("T")
        assert list(x) == [0.0, 0.25, 0.5, 0.75]
        assert list(y) == [0.0, 1.0, 2.0, 3.0]
        assert eng.latest_x == 0.75


# ─────────────────────────────────────────────────────────────────────────────
# PlotEngine: late-bind capability
# ─────────────────────────────────────────────────────────────────────────────

class TestPlotEngineLateBind:
    """The view is constructed lazily on first toggle. By then the
    engine may already have detected columns and accumulated data.
    These tests verify the engine exposes everything the view needs
    to rebuild itself from existing state.
    """

    def test_columns_query_after_detection(self):
        eng = PlotEngine()
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"T: {i}, H: {i + 1}", _tick())
        # View constructed AFTER this point: it queries:
        assert eng.detection_complete
        assert eng.columns == ["T", "H"]
        # And can pull existing buffer state
        x, y = eng.snapshot("T")
        assert x.size > 0
        assert y.size > 0

    def test_gave_up_query_after_failure(self):
        eng = PlotEngine()
        for i in range(PLOT_DETECTION_GIVE_UP_LINES + 5):
            eng.process(f"unstructured noise {i}", _tick())
        assert eng.detection_gave_up
        assert eng.columns == []


# ─────────────────────────────────────────────────────────────────────────────
# PlotEngine: connect / disconnect / reconnect cycle
# ─────────────────────────────────────────────────────────────────────────────

class TestPlotEngineReconnectCycle:
    """Disconnecting a tab and connecting again on the same tab must not
    leave the plot empty: after a reset the engine detects afresh."""

    def test_engine_reset_then_redetects_cleanly(self):
        eng = PlotEngine()
        # First session
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"RPM: {1400 + i}, Voltage: {12.0 + i * 0.01:.2f}", _tick())
        assert eng.detection_complete
        assert eng.columns == ["RPM", "Voltage"]

        # Disconnect path: engine.reset() called by ordered_shutdown
        eng.reset()
        assert not eng.detection_complete
        assert eng.columns == []
        assert eng.latest_x == 0.0

        # Reconnect: same format
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(
                f"RPM: {1500 + i}, Voltage: {12.5 + i * 0.01:.2f}", _tick(),
            )
        assert eng.detection_complete
        assert eng.columns == ["RPM", "Voltage"]
        # Data from second session is fresh, not bleeding from first
        _x, y = eng.snapshot("RPM")
        assert y.min() >= 1500.0, \
            "second-session buffer leaked first-session data"

    def test_engine_reconnect_with_different_format(self):
        """An engineer might reconnect with a different firmware loaded.
        The engine must adapt without manual intervention."""
        eng = PlotEngine()
        # Session 1: motor format
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"RPM: {1400 + i}", _tick())
        assert eng.columns == ["RPM"]

        eng.reset()

        # Session 2: weather format
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"Temp: {25.0 + i * 0.1:.1f}, Humidity: {50 + i}", _tick())
        assert eng.detection_complete
        assert eng.columns == ["Temp", "Humidity"]

    def test_columns_detected_signal_fires_on_each_redetection(self):
        """The view relies on the columns_detected signal to rebuild
        traces. After engine.reset(), the next detection must re-emit.
        """
        eng = PlotEngine()
        emit_count = [0]
        last_columns: list[list[str]] = []

        def on_columns(cols):
            emit_count[0] += 1
            last_columns.append(list(cols))

        eng.columns_detected.connect(on_columns)

        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"A: {i}, B: {i * 2}", _tick())
        assert emit_count[0] == 1
        assert last_columns[-1] == ["A", "B"]

        eng.reset()
        # Reset must NOT re-emit
        assert emit_count[0] == 1

        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"A: {i}, B: {i * 2}", _tick())
        assert emit_count[0] == 2, \
            "columns_detected did not re-fire after reset+redetect"
        assert last_columns[-1] == ["A", "B"]

    def test_three_cycles(self):
        """Connect → disconnect → connect → disconnect → connect.
        Ensures the cycle fix isn't a one-shot."""
        eng = PlotEngine()
        for cycle in range(3):
            for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
                eng.process(f"V: {3.0 + cycle + i * 0.01:.3f}", _tick())
            assert eng.detection_complete, f"cycle {cycle} did not detect"
            assert eng.columns == ["V"]
            _x, y = eng.snapshot("V")
            assert y.size > 0
            # Each cycle's data starts from its own baseline
            assert y.min() >= 3.0 + cycle - 0.01
            eng.reset()


# ─────────────────────────────────────────────────────────────────────────────
# PlotEngine: activation
# ─────────────────────────────────────────────────────────────────────────────

class TestPlotEngineActivation:
    """An inactive engine does no parsing; activating it catches up."""

    def test_engine_parses_by_default(self):
        eng = PlotEngine()
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"temp: {i}", _tick())
        assert eng.columns == ["temp"]

    def test_inactive_engine_parses_nothing_but_remembers_recent_lines(self):
        eng = PlotEngine()
        eng.set_active(False)
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE * 2):
            eng.process(f"T: {i}, H: {i + 1}", _tick())
        assert not eng.detection_complete
        assert not eng.has_any_data
        assert len(eng.recent_lines()) == PLOT_AUTODETECT_SAMPLE_SIZE * 2

    def test_activation_detects_and_plots_from_the_recent_lines(self):
        eng = PlotEngine()
        eng.set_active(False)
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE * 2):
            eng.process(f"T: {i}, H: {i + 1}", _tick())
        eng.set_active(True)
        assert eng.detection_complete
        assert eng.columns == ["T", "H"]
        x, y = eng.snapshot("T")
        assert y.size == PLOT_AUTODETECT_SAMPLE_SIZE * 2
        assert np.all(np.diff(x) > 0)

    def test_activation_with_few_recent_lines_keeps_collecting(self):
        eng = PlotEngine()
        eng.set_active(False)
        for i in range(10):
            eng.process(f"T: {i}", _tick())
        eng.set_active(True)
        assert not eng.detection_complete
        for i in range(10, PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"T: {i}", _tick())
        assert eng.detection_complete
        assert eng.snapshot("T")[1].size == PLOT_AUTODETECT_SAMPLE_SIZE

    def test_deactivation_keeps_what_was_plotted(self):
        eng = PlotEngine()
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"T: {i}", _tick())
        eng.set_active(False)
        assert eng.detection_complete
        assert eng.snapshot("T")[1].size == PLOT_AUTODETECT_SAMPLE_SIZE

    def test_reactivation_does_not_duplicate_samples(self):
        eng = PlotEngine()
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"T: {i}", _tick())
        eng.set_active(False)
        eng.set_active(True)
        assert eng.snapshot("T")[1].size == PLOT_AUTODETECT_SAMPLE_SIZE

    def test_setting_the_same_state_changes_nothing(self):
        eng = PlotEngine()
        for i in range(PLOT_AUTODETECT_SAMPLE_SIZE):
            eng.process(f"T: {i}", _tick())
        eng.set_active(True)
        assert eng.snapshot("T")[1].size == PLOT_AUTODETECT_SAMPLE_SIZE

    def test_manual_mode_catches_up_on_activation(self):
        eng = PlotEngine()
        eng.set_manual_config(r"RPM:\s*(?P<RPM>\d+)")
        eng.set_active(False)
        for i in range(5):
            eng.process(f"RPM: {1400 + i}", _tick())
        assert not eng.has_any_data
        eng.set_active(True)
        assert list(eng.snapshot("RPM")[1]) == [1400.0, 1401.0, 1402.0, 1403.0, 1404.0]
