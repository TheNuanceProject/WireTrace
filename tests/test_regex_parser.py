# SPDX-License-Identifier: MIT
"""Tests for ``core.plot_parsers.RegexParser`` and engine manual mode.

The RegexParser is the user-declared half of the plotter: it lets a
firmware engineer plot any format their device emits, even ones the
auto-detect pipeline can't recognise. These tests pin down its
contract — pattern validation, named-group extraction, partial
matches, mode switching on the engine, and the test() helper that
the Configure Plot dialog uses.
"""

from __future__ import annotations

import pytest

from core.plot_engine import (
    PLOT_RECENT_LINES_BUFFER_SIZE,
    PlotEngine,
    PlotMode,
)
from core.plot_parsers import RegexParser, RegexParserError

# ─────────────────────────────────────────────────────────────────────────────
# RegexParser — construction validation
# ─────────────────────────────────────────────────────────────────────────────

class TestRegexParserConstruction:
    def test_empty_pattern_rejected(self):
        with pytest.raises(RegexParserError, match="empty"):
            RegexParser("")

    def test_whitespace_only_pattern_rejected(self):
        with pytest.raises(RegexParserError, match="empty"):
            RegexParser("   \t  ")

    def test_invalid_regex_rejected(self):
        with pytest.raises(RegexParserError, match="compile"):
            RegexParser(r"(?P<name>[unclosed")

    def test_no_named_groups_rejected(self):
        with pytest.raises(RegexParserError, match="named groups"):
            RegexParser(r"\d+,\d+,\d+")

    def test_only_unnamed_groups_rejected(self):
        with pytest.raises(RegexParserError, match="named groups"):
            RegexParser(r"(\d+)\s+(\d+)")

    def test_single_named_group_accepted(self):
        p = RegexParser(r"RPM:\s*(?P<RPM>\d+)")
        assert p.columns == ["RPM"]
        assert p.pattern == r"RPM:\s*(?P<RPM>\d+)"

    def test_columns_in_declaration_order(self):
        """Column order must follow the regex's named-group order so
        plot trace order matches what the user declared."""
        p = RegexParser(
            r"v=(?P<voltage>[\d.]+)\s+i=(?P<current>[\d.]+)\s+t=(?P<temp>[\d.]+)"
        )
        assert p.columns == ["voltage", "current", "temp"]


# ─────────────────────────────────────────────────────────────────────────────
# RegexParser — extract behaviour
# ─────────────────────────────────────────────────────────────────────────────

class TestRegexParserExtract:
    def test_simple_match(self):
        p = RegexParser(r"RPM:\s*(?P<RPM>\d+)")
        assert p.extract("RPM: 1461") == {"RPM": 1461.0}

    def test_search_not_fullmatch(self):
        """Pattern targets the payload; surrounding noise/timestamps
        must not break it. Engineers shouldn't have to anchor."""
        p = RegexParser(r"RPM:\s*(?P<RPM>\d+)")
        assert p.extract("[2026-05-10 23:23:25.095] RPM: 1461, other stuff") \
            == {"RPM": 1461.0}

    def test_multiple_named_groups(self):
        p = RegexParser(
            r"RPM:\s*(?P<RPM>\d+),\s*Voltage:\s*(?P<Voltage>[\d.]+)"
        )
        out = p.extract("RPM: 1461, Voltage: 12.20")
        assert out == {"RPM": 1461.0, "Voltage": 12.20}

    def test_no_match_returns_none(self):
        p = RegexParser(r"RPM:\s*(?P<RPM>\d+)")
        assert p.extract("WARNING: thermal shutdown") is None

    def test_non_numeric_capture_excluded(self):
        """A named group that captures non-numeric text is dropped
        from the result — keeps plotting alive when one channel
        emits an error string while others stay healthy."""
        p = RegexParser(
            r"RPM:\s*(?P<RPM>\S+),\s*Voltage:\s*(?P<Voltage>[\d.]+)"
        )
        out = p.extract("RPM: ERR, Voltage: 12.20")
        assert out == {"Voltage": 12.20}

    def test_all_non_numeric_returns_none(self):
        p = RegexParser(r"(?P<a>\S+)\s+(?P<b>\S+)")
        assert p.extract("hello world") is None

    def test_optional_group_not_captured(self):
        """An optional group that didn't match returns None; the parser
        skips it gracefully."""
        p = RegexParser(r"a=(?P<a>\d+)(?:\s+b=(?P<b>\d+))?")
        assert p.extract("a=1 b=2") == {"a": 1.0, "b": 2.0}
        assert p.extract("a=1") == {"a": 1.0}

    def test_negative_and_decimal(self):
        p = RegexParser(r"v=(?P<v>-?[\d.]+)")
        assert p.extract("v=-3.14") == {"v": -3.14}
        assert p.extract("v=2.71828") == {"v": 2.71828}

    def test_scientific_notation(self):
        p = RegexParser(r"flux=(?P<flux>[-\d.eE+]+)")
        assert p.extract("flux=1.5e-9") == {"flux": 1.5e-9}


# ─────────────────────────────────────────────────────────────────────────────
# RegexParser — test() helper for the Configure Plot dialog
# ─────────────────────────────────────────────────────────────────────────────

class TestRegexParserTest:
    """The test() helper drives the dialog's 'Test pattern' button.

    Returns match count, total, columns seen in the sample, and a
    preview of the first three matching extractions.
    """

    def test_all_match(self):
        p = RegexParser(r"x=(?P<x>\d+)")
        sample = [f"x={i}" for i in range(10)]
        result = p.test(sample)
        assert result["matched"] == 10
        assert result["total"] == 10
        assert result["columns"] == ["x"]
        assert len(result["preview"]) == 3
        assert result["preview"][0] == ("x=0", {"x": 0.0})

    def test_partial_match(self):
        """Pattern matches some lines, doesn't match others. The
        dialog shows this as 'amber' — valid but firmware sometimes
        doesn't emit the expected format."""
        p = RegexParser(r"x=(?P<x>\d+)")
        sample = ["x=1", "noise", "x=2", "WARNING", "x=3"]
        result = p.test(sample)
        assert result["matched"] == 3
        assert result["total"] == 5
        assert result["columns"] == ["x"]

    def test_zero_match_amber_state(self):
        """No match — but pattern is structurally valid. Apply still
        allowed because firmware may emit the format later."""
        p = RegexParser(r"y=(?P<y>\d+)")
        sample = ["x=1", "x=2", "x=3"]
        result = p.test(sample)
        assert result["matched"] == 0
        assert result["total"] == 3
        assert result["columns"] == []  # no column ever matched
        assert result["preview"] == []

    def test_empty_sample(self):
        """Edge case: dialog opened before any DATA lines arrived."""
        p = RegexParser(r"x=(?P<x>\d+)")
        result = p.test([])
        assert result["matched"] == 0
        assert result["total"] == 0
        assert result["columns"] == []

    def test_columns_filtered_to_actually_seen(self):
        """A column whose group never captured a numeric value is
        excluded from the result's columns list — the dialog should
        warn the user that 'b' didn't actually plot anything."""
        p = RegexParser(r"a=(?P<a>\d+)(?:\s+b=(?P<b>\d+))?")
        sample = ["a=1", "a=2", "a=3"]
        result = p.test(sample)
        assert result["matched"] == 3
        assert result["columns"] == ["a"]  # b never captured


# ─────────────────────────────────────────────────────────────────────────────
# PlotEngine — manual mode integration
# ─────────────────────────────────────────────────────────────────────────────

class TestPlotEngineManualMode:
    def test_default_mode_is_auto(self):
        eng = PlotEngine()
        assert eng.mode == PlotMode.AUTO
        assert eng.manual_pattern is None

    def test_set_manual_config_switches_mode(self):
        eng = PlotEngine()
        eng.set_manual_config(r"RPM:\s*(?P<RPM>\d+)")
        assert eng.mode == PlotMode.MANUAL
        assert eng.manual_pattern == r"RPM:\s*(?P<RPM>\d+)"
        assert eng.detection_complete  # immediate, no sample buffering
        assert eng.columns == ["RPM"]
        assert eng.active_parser_name == "regex"

    def test_set_manual_config_invalid_pattern_raises(self):
        eng = PlotEngine()
        with pytest.raises(RegexParserError):
            eng.set_manual_config(r"(?P<x>[unclosed")
        # Engine state must NOT have changed
        assert eng.mode == PlotMode.AUTO
        assert eng.manual_pattern is None
        assert not eng.detection_complete

    def test_manual_mode_extracts_immediately(self):
        eng = PlotEngine()
        eng.set_manual_config(r"RPM:\s*(?P<RPM>\d+)")
        # No 50-line warm-up — first DATA line lands in the buffer
        for i in range(10):
            eng.process(f"RPM: {1400 + i}", "DATA")
        _x, y = eng.snapshot("RPM")
        assert y.size == 10
        assert y[0] == 1400.0
        assert y[-1] == 1409.0

    def test_manual_mode_skips_non_matching_lines(self):
        eng = PlotEngine()
        eng.set_manual_config(r"RPM:\s*(?P<RPM>\d+)")
        eng.process("RPM: 1400", "DATA")
        eng.process("WARNING: thermal", "DATA")
        eng.process("RPM: 1401", "DATA")
        eng.process("noise line with no match", "DATA")
        eng.process("RPM: 1402", "DATA")
        _x, y = eng.snapshot("RPM")
        assert y.size == 3  # only the 3 RPM lines

    def test_manual_mode_severity_lines_filtered(self):
        """The TAG_DATA filter still applies in manual mode — severity-
        tagged lines never reach the parser."""
        eng = PlotEngine()
        eng.set_manual_config(r"RPM:\s*(?P<RPM>\d+)")
        eng.process("RPM: 1400", "WARNING")  # filtered by tag
        eng.process("RPM: 1401", "DATA")
        _x, y = eng.snapshot("RPM")
        assert y.size == 1
        assert y[0] == 1401.0

    def test_set_auto_config_switches_back(self):
        eng = PlotEngine()
        eng.set_manual_config(r"RPM:\s*(?P<RPM>\d+)")
        for i in range(10):
            eng.process(f"RPM: {1400 + i}", "DATA")
        assert eng.has_any_data

        eng.set_auto_config()
        assert eng.mode == PlotMode.AUTO
        assert eng.manual_pattern is None
        assert eng.columns == []  # cleared
        assert not eng.detection_complete
        assert not eng.has_any_data

    def test_manual_pattern_preserved_across_reset(self):
        """The disconnect/reconnect cycle must preserve the user's
        manual config — re-entering it every reconnect would be
        hostile UX."""
        eng = PlotEngine()
        eng.set_manual_config(r"RPM:\s*(?P<RPM>\d+)")
        for i in range(10):
            eng.process(f"RPM: {1400 + i}", "DATA")

        # Disconnect: ordered_shutdown calls reset()
        eng.reset()
        assert eng.mode == PlotMode.MANUAL
        assert eng.manual_pattern == r"RPM:\s*(?P<RPM>\d+)"
        assert eng.detection_complete  # still ready
        assert eng.columns == ["RPM"]
        assert not eng.has_any_data  # buffers cleared

        # Reconnect: lines flow, engine extracts using the preserved pattern
        for i in range(5):
            eng.process(f"RPM: {1500 + i}", "DATA")
        _x, y = eng.snapshot("RPM")
        assert y.size == 5
        assert y[0] == 1500.0  # fresh data, no first-session leakage

    def test_columns_detected_fires_on_set_manual_config(self):
        """The view relies on this signal to rebuild traces."""
        eng = PlotEngine()
        emitted: list[list[str]] = []
        eng.columns_detected.connect(lambda cols: emitted.append(list(cols)))

        eng.set_manual_config(r"a=(?P<a>\d+)\s+b=(?P<b>\d+)")
        assert emitted == [["a", "b"]]

    def test_columns_detected_fires_on_manual_reset(self):
        """Reset re-emits so the view rebuilds for the new session."""
        eng = PlotEngine()
        eng.set_manual_config(r"x=(?P<x>\d+)")
        emitted: list[list[str]] = []
        eng.columns_detected.connect(lambda cols: emitted.append(list(cols)))

        eng.reset()
        assert emitted == [["x"]]

    def test_changing_pattern_resets_buffers(self):
        """Switching to a different pattern can't leave old data
        from the previous schema in the buffers."""
        eng = PlotEngine()
        eng.set_manual_config(r"RPM:\s*(?P<RPM>\d+)")
        for i in range(10):
            eng.process(f"RPM: {1400 + i}", "DATA")
        assert eng.has_any_data

        eng.set_manual_config(r"V:\s*(?P<V>[\d.]+)")
        assert eng.columns == ["V"]
        assert not eng.has_any_data


# ─────────────────────────────────────────────────────────────────────────────
# PlotEngine — recent_lines accessor
# ─────────────────────────────────────────────────────────────────────────────

class TestPlotEngineRecentLines:
    """The Configure Plot dialog displays recent DATA lines as
    reference and uses them as the corpus for pattern testing.
    """

    def test_starts_empty(self):
        eng = PlotEngine()
        assert eng.recent_lines() == []

    def test_collects_data_lines(self):
        eng = PlotEngine()
        for i in range(5):
            eng.process(f"line {i}", "DATA")
        assert eng.recent_lines() == [f"line {i}" for i in range(5)]

    def test_excludes_non_data_tags(self):
        """Recent-lines is for plotting reference. Severity and
        command tags are excluded so the dialog only shows plottable
        candidates."""
        eng = PlotEngine()
        eng.process("data 1", "DATA")
        eng.process("WARNING: thermal", "WARNING")
        eng.process("INFO: heartbeat", "INFO")
        eng.process("data 2", "DATA")
        assert eng.recent_lines() == ["data 1", "data 2"]

    def test_capped_at_buffer_size(self):
        eng = PlotEngine()
        n = PLOT_RECENT_LINES_BUFFER_SIZE + 50
        for i in range(n):
            eng.process(f"line {i}", "DATA")
        recent = eng.recent_lines()
        assert len(recent) == PLOT_RECENT_LINES_BUFFER_SIZE
        # Window holds the most recent lines
        assert recent[-1] == f"line {n - 1}"
        assert recent[0] == f"line {n - PLOT_RECENT_LINES_BUFFER_SIZE}"

    def test_returns_copy(self):
        """Mutating the returned list must not affect the engine."""
        eng = PlotEngine()
        eng.process("a", "DATA")
        recent = eng.recent_lines()
        recent.clear()
        assert eng.recent_lines() == ["a"]

    def test_cleared_on_reset(self):
        eng = PlotEngine()
        for i in range(10):
            eng.process(f"line {i}", "DATA")
        eng.reset()
        assert eng.recent_lines() == []

    def test_independent_of_detection_state(self):
        """Even after auto-detect gives up, recent lines keep flowing
        so the user can switch to manual mode and see them."""
        eng = PlotEngine()
        # Push enough noise to make auto-detect give up
        from core.plot_engine import PLOT_DETECTION_GIVE_UP_LINES
        for i in range(PLOT_DETECTION_GIVE_UP_LINES + 5):
            eng.process(f"random unstructured line {i}", "DATA")
        assert eng.detection_gave_up

        # And new lines should still feed recent_lines
        eng.process("a structured: 42 line", "DATA")
        assert "a structured: 42 line" in eng.recent_lines()
