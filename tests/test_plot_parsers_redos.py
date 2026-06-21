# SPDX-License-Identifier: MIT
"""Regression tests for B3 — ReDoS via user-supplied plot regex.

The bug: the manual-mode plot parser compiled the user's pattern with the
stdlib ``re`` module and called ``search`` with no time bound. A
catastrophic-backtracking pattern against an adversarial line ran for
exponential time, freezing the parsing thread and the UI.

The fix compiles the pattern with the ``regex`` library and runs every
search with a per-line ``timeout`` (``PLOT_REGEX_TIMEOUT_SECONDS``). A
search that exceeds the budget raises ``TimeoutError``; ``extract``
converts that into a skipped line (returns ``None``), so the live data
thread keeps flowing, and ``test`` counts the timeouts so the Configure
Plot dialog can show a red result and disable Apply.

Note on the pathological pattern used here: the ``regex`` library's
engine resists the classic ``(?P<x>a+)+$`` (it returns no-match almost
instantly), so that pattern does NOT exercise the timeout path. A pattern
that genuinely forces backtracking under ``regex`` is
``(?P<x>(a|a)+)+$``; these tests use it so the timeout branch is really
covered.
"""

from __future__ import annotations

import time

import pytest

from app.constants import PLOT_REGEX_TIMEOUT_SECONDS
from core.plot_parsers import RegexParser, RegexParserError

# A pattern that genuinely triggers catastrophic backtracking under the
# regex library, paired with a non-matching adversarial line.
PATHOLOGICAL_PATTERN = r"(?P<x>(a|a)+)+$"
ADVERSARIAL_LINE = "a" * 40 + "b"


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
        # swallows it. This pins the mechanism the fix relies on.
        parser = RegexParser(PATHOLOGICAL_PATTERN)
        with pytest.raises(TimeoutError):
            parser._timed_search(ADVERSARIAL_LINE)

    def test_extract_does_not_freeze_on_repeated_bad_lines(self):
        parser = RegexParser(PATHOLOGICAL_PATTERN)
        t0 = time.time()
        for _ in range(5):
            assert parser.extract(ADVERSARIAL_LINE) is None
        elapsed = time.time() - t0
        # Five lines, each bounded by the budget — nowhere near a freeze.
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
