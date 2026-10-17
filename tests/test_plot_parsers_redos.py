# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests that a user-supplied plot pattern cannot hang the parser.

The pattern is compiled with the ``regex`` library and every search runs
with a per-line ``timeout`` (``PLOT_REGEX_TIMEOUT_SECONDS``). A search
that exceeds it raises ``TimeoutError``; ``extract`` turns that into a
skipped line, and ``test`` counts the timeouts so the Configure plot
dialog can refuse the pattern.

The pathological pattern used here is ``(?P<x>(a|a)+)+$``. The
``regex`` engine answers the classic ``(?P<x>a+)+$`` almost at once, so
that one would not reach the timeout.
"""

from __future__ import annotations

import time

import pytest

from app.constants import (
    PLOT_MAX_LINE_CHARACTERS,
    PLOT_REGEX_MAX_CONSECUTIVE_TIMEOUTS,
    PLOT_REGEX_TIMEOUT_SECONDS,
)
from core.plot_engine import PlotEngine
from core.plot_parsers import JsonParser, KvParser, RegexParser, RegexParserError
from tests.clock import TickClock

_tick = TickClock()

# A pattern that genuinely triggers catastrophic backtracking under the
# regex library, paired with a non-matching adversarial line.
PATHOLOGICAL_PATTERN = r"(?P<x>(a|a)+)+$"
ADVERSARIAL_LINE = "a" * 40 + "b"

# Generous for a slow shared runner, and far below what a pattern whose
# cost grows with the square of the line length would take.
_PROMPT_SECONDS = 2.0


class TestCompilation:
    def test_valid_named_pattern_compiles(self):
        parser = RegexParser(r"RPM:\s*(?P<rpm>\d+)")
        assert parser.columns == ["rpm"]

    def test_invalid_pattern_raises_parser_error(self):
        with pytest.raises(RegexParserError):
            RegexParser(r"(?P<x>")  # unbalanced group

    def test_pattern_without_named_groups_raises(self):
        with pytest.raises(RegexParserError):
            RegexParser(r"\d+")

    def test_empty_pattern_raises(self):
        with pytest.raises(RegexParserError):
            RegexParser("   ")


class TestExtractNormal:
    def test_extracts_numeric_named_groups(self):
        parser = RegexParser(r"RPM:\s*(?P<rpm>\d+),\s*I:\s*(?P<cur>[\d.]+)")
        assert parser.extract("RPM: 1450, I: 3.2") == {"rpm": 1450.0, "cur": 3.2}

    def test_non_matching_line_returns_none(self):
        parser = RegexParser(r"RPM:\s*(?P<rpm>\d+)")
        assert parser.extract("no numbers here") is None

    def test_partial_numeric_capture(self):
        # One group numeric, one not → partial dict (keeps plotting alive).
        parser = RegexParser(r"A:\s*(?P<a>\w+),\s*B:\s*(?P<b>\w+)")
        assert parser.extract("A: 12, B: ERR") == {"a": 12.0}


class TestRedosTimeout:
    def test_pathological_extract_returns_none_quickly(self):
        parser = RegexParser(PATHOLOGICAL_PATTERN)
        t0 = time.time()
        result = parser.extract(ADVERSARIAL_LINE)
        elapsed = time.time() - t0

        # Timed out → treated as a non-match (skipped line).
        assert result is None
        # Bounded by the per-line budget plus a generous margin; the
        # unfixed code would run for many seconds on this input.
        assert elapsed < PLOT_REGEX_TIMEOUT_SECONDS + 0.5

    def test_timed_search_raises_timeout_error(self):
        # The internal search surfaces the timeout; extract is what
        # swallows it. The guard against a hanging pattern rests on this.
        parser = RegexParser(PATHOLOGICAL_PATTERN)
        with pytest.raises(TimeoutError):
            parser._timed_search(ADVERSARIAL_LINE)

    def test_extract_does_not_freeze_on_repeated_bad_lines(self):
        parser = RegexParser(PATHOLOGICAL_PATTERN)
        t0 = time.time()
        for _ in range(5):
            assert parser.extract(ADVERSARIAL_LINE) is None
        elapsed = time.time() - t0
        # Five lines, each bounded by the budget: nowhere near a freeze.
        assert elapsed < (PLOT_REGEX_TIMEOUT_SECONDS * 5) + 1.0


class TestTestMethodReportsTimeout:
    def test_timed_out_count_surfaced(self):
        parser = RegexParser(PATHOLOGICAL_PATTERN)
        sample = [ADVERSARIAL_LINE, ADVERSARIAL_LINE, "a" * 30 + "b"]
        result = parser.test(sample)

        assert result["timed_out"] == 3
        assert result["matched"] == 0
        assert result["total"] == 3

    def test_clean_pattern_reports_zero_timeouts(self):
        parser = RegexParser(r"(?P<rpm>\d+)")
        result = parser.test(["RPM: 1450", "RPM: 1460", "no match"])
        assert result["timed_out"] == 0
        assert result["matched"] == 2
        assert result["columns"] == ["rpm"]

    def test_result_always_has_timed_out_key(self):
        parser = RegexParser(r"(?P<rpm>\d+)")
        assert "timed_out" in parser.test([])


class TestAutoParsersOnHostileLines:
    """The automatic parsers run on the GUI thread, on whatever a device
    sends. The time spent on a line must grow with its length, not with
    its square, and no line may raise."""

    @pytest.mark.parametrize(
        "line",
        [
            # Named, because the test's name otherwise contains the
            # whole line, which is longer than Windows allows in the
            # environment variable pytest keeps the name in.
            pytest.param("A" * 65_536, id="one-long-word"),
            pytest.param("ab " * 21_000, id="many-short-words"),
            pytest.param("a_" * 32_000, id="underscores"),
            pytest.param("k" + " " * 60_000 + ": 5", id="key-then-long-gap"),
        ],
    )
    def test_key_value_parser_handles_a_long_line_promptly(self, line):
        parser = KvParser()
        parser.detect(["temp: 1, rpm: 2"] * 10)
        started = time.perf_counter()
        parser.extract(line)
        assert time.perf_counter() - started < _PROMPT_SECONDS

    def test_key_value_pairs_are_still_found(self):
        parser = KvParser()
        assert parser.detect(["Motor Temp: 21.5, rpm=-40"] * 10) == ["Motor Temp", "rpm"]
        assert parser.extract("Motor Temp: 22, rpm=-41") == {
            "Motor Temp": 22.0, "rpm": -41.0,
        }

    def test_deeply_nested_json_is_not_data_and_does_not_raise(self):
        parser = JsonParser()
        nested = '{"a":' + "[" * 60_000
        assert not parser.detect([nested] * 10)
        parser.detect(['{"temp": 1}'] * 10)
        assert parser.extract(nested) is None


class TestPatternThatKeepsRunningOutOfTime:
    def test_timeouts_in_a_row_are_counted_and_a_match_starts_the_count_again(self):
        parser = RegexParser(PATHOLOGICAL_PATTERN)

        parser.extract(ADVERSARIAL_LINE)
        parser.extract(ADVERSARIAL_LINE)
        assert parser.consecutive_timeouts == 2

        parser.extract("aaa")
        assert parser.consecutive_timeouts == 0

    def test_the_engine_stops_plotting_and_says_so_once(self):
        engine = PlotEngine()
        stalls: list[bool] = []
        engine.pattern_stalled.connect(lambda: stalls.append(True))
        engine.set_manual_config(PATHOLOGICAL_PATTERN)

        for _ in range(PLOT_REGEX_MAX_CONSECUTIVE_TIMEOUTS):
            engine.process(ADVERSARIAL_LINE, _tick())
        assert engine.stalled is True
        assert stalls == [True]

        started = time.time()
        for _ in range(1_000):
            engine.process(ADVERSARIAL_LINE, _tick())
        assert time.time() - started < _PROMPT_SECONDS
        assert stalls == [True]

    def test_a_new_pattern_starts_plotting_again(self):
        engine = PlotEngine()
        engine.set_manual_config(PATHOLOGICAL_PATTERN)
        for _ in range(PLOT_REGEX_MAX_CONSECUTIVE_TIMEOUTS):
            engine.process(ADVERSARIAL_LINE, _tick())

        engine.set_manual_config(r"rpm=(?P<rpm>\d+)")
        engine.process("rpm=1200", _tick())

        assert engine.stalled is False
        assert engine.snapshot("rpm")[1].tolist() == [1200.0]

    def test_a_few_timeouts_between_matches_do_not_stop_plotting(self):
        engine = PlotEngine()
        engine.set_manual_config(PATHOLOGICAL_PATTERN)

        for _ in range(PLOT_REGEX_MAX_CONSECUTIVE_TIMEOUTS - 1):
            engine.process(ADVERSARIAL_LINE, _tick())
        engine.process("aaa", _tick())
        for _ in range(PLOT_REGEX_MAX_CONSECUTIVE_TIMEOUTS - 1):
            engine.process(ADVERSARIAL_LINE, _tick())

        assert engine.stalled is False


class TestPatternsAndValuesBeyondWhatCanBeHandled:
    def test_a_pattern_too_deep_to_compile_is_refused_with_a_reason(self):
        with pytest.raises(RegexParserError):
            RegexParser("(" * 5_000 + r"(?P<x>\d+)" + ")" * 5_000)

    def test_an_integer_too_large_for_a_reading_is_left_out(self):
        parser = JsonParser()
        parser.detect([f'{{"a": {n}, "b": {n}}}' for n in range(5)])

        assert parser.extract('{"a": 1' + "0" * 400 + ', "b": 2}') == {"b": 2.0}

    def test_a_very_long_line_is_plotted_from_its_start(self):
        engine = PlotEngine()
        engine.set_manual_config(r"rpm=(?P<rpm>\d+)")

        engine.process("rpm=1200 " + "x" * (PLOT_MAX_LINE_CHARACTERS * 4), _tick())
        engine.process("x" * PLOT_MAX_LINE_CHARACTERS + " rpm=9", _tick())

        assert engine.snapshot("rpm")[1].tolist() == [1200.0]
