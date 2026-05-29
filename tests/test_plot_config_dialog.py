# SPDX-License-Identifier: MIT
"""Tests for the Configure Plot dialog's pure logic.

The interactive Qt parts can't be exercised under the headless test
stub, but the deterministic helpers — the numeric tokenizer and the
capture-assistant's pattern generation — are testable. These tests
pin the behaviour the user touches when they "Capture from sample".
"""

from __future__ import annotations

import re

# Stubs are installed via conftest.py auto-fixture
from ui.dialogs.plot_config_dialog import _CaptureAssistantDialog

# ─────────────────────────────────────────────────────────────────────────────
# Tokenizer — splits a sample line into (text, is_numeric) tokens
# ─────────────────────────────────────────────────────────────────────────────

class TestTokenizer:
    def test_simple_kv(self):
        toks = _CaptureAssistantDialog._tokenize("RPM: 1461")
        assert toks == [("RPM: ", False), ("1461", True)]

    def test_multiple_values(self):
        toks = _CaptureAssistantDialog._tokenize(
            "RPM: 1461, Voltage: 12.20",
        )
        # Numeric tokens preserved verbatim; interstitials kept literal
        nums = [t for t, is_num in toks if is_num]
        assert nums == ["1461", "12.20"]

    def test_negative_number(self):
        toks = _CaptureAssistantDialog._tokenize("temp: -3.14")
        nums = [t for t, is_num in toks if is_num]
        assert nums == ["-3.14"]

    def test_scientific_notation(self):
        toks = _CaptureAssistantDialog._tokenize("flux=1.5e-9")
        nums = [t for t, is_num in toks if is_num]
        assert nums == ["1.5e-9"]

    def test_no_numbers(self):
        toks = _CaptureAssistantDialog._tokenize("hello world")
        assert toks == [("hello world", False)]
        assert all(not is_num for _t, is_num in toks)

    def test_empty_string(self):
        toks = _CaptureAssistantDialog._tokenize("")
        assert toks == []

    def test_trailing_text(self):
        toks = _CaptureAssistantDialog._tokenize("count=42 done")
        # "count=" → 42 → " done"
        assert len(toks) == 3
        assert toks[1] == ("42", True)
        assert toks[2] == (" done", False)

    def test_concatenated_numerics(self):
        """'12,34' is two numeric tokens with ',' between."""
        toks = _CaptureAssistantDialog._tokenize("12,34")
        nums = [t for t, is_num in toks if is_num]
        assert nums == ["12", "34"]

    def test_preserves_offsets_for_round_trip(self):
        """Concatenating tokens reproduces the original line —
        critical for pattern generation."""
        line = "[2026-05-10 23:23:25.997] RPM: 1461, V: 12.20"
        toks = _CaptureAssistantDialog._tokenize(line)
        assert "".join(t for t, _ in toks) == line


# ─────────────────────────────────────────────────────────────────────────────
# Generated patterns actually match the source line
# ─────────────────────────────────────────────────────────────────────────────

class TestGeneratedPatternMatches:
    """The capture assistant's whole point is generating a regex that
    matches lines structurally identical to the sample. Verify by
    constructing scenarios manually (no Qt) and asserting via re.
    """

    def _generate(self, line: str, captures: dict[int, str]) -> str:
        """Replicate the dialog's pattern-generation logic in isolation
        (the Qt-bound _refresh_preview can't run under stubs)."""
        toks = _CaptureAssistantDialog._tokenize(line)
        parts: list[str] = []
        for i, (text, is_num) in enumerate(toks):
            if is_num and i in captures:
                name = captures[i]
                parts.append(rf"(?P<{name}>-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)")
            else:
                escaped = re.escape(text)
                escaped = re.sub(r"(?:\\\s)+", r"\\s+", escaped)
                parts.append(escaped)
        return "".join(parts)

    def test_single_capture_matches_self(self):
        line = "RPM: 1461"
        # idx 1 is the number
        pattern = self._generate(line, {1: "RPM"})
        m = re.search(pattern, line)
        assert m is not None
        assert m.group("RPM") == "1461"

    def test_multiple_captures_match(self):
        line = "RPM: 1461, Voltage: 12.20"
        toks = _CaptureAssistantDialog._tokenize(line)
        # Find numeric token indices
        nums = [i for i, (_t, is_num) in enumerate(toks) if is_num]
        captures = {nums[0]: "RPM", nums[1]: "Voltage"}
        pattern = self._generate(line, captures)
        m = re.search(pattern, line)
        assert m is not None
        assert m.group("RPM") == "1461"
        assert m.group("Voltage") == "12.20"

    def test_pattern_tolerates_whitespace_changes(self):
        """Generated pattern uses \\s+ for whitespace runs so minor
        formatting differences don't break the match."""
        line = "RPM: 1461,  Voltage: 12.20"  # double space
        toks = _CaptureAssistantDialog._tokenize(line)
        nums = [i for i, (_t, is_num) in enumerate(toks) if is_num]
        pattern = self._generate(line, {nums[0]: "RPM", nums[1]: "V"})

        # Should match the original
        assert re.search(pattern, line) is not None
        # And a single-space variant
        assert re.search(pattern, "RPM: 1461, Voltage: 12.20") is not None
        # And a tab variant
        assert re.search(pattern, "RPM:\t1461,\tVoltage:\t12.20") is not None

    def test_partial_capture_skips_unwanted_numbers(self):
        """Only captured tokens become named groups; others stay literal."""
        line = "[123] RPM: 1461 V: 12.20"
        toks = _CaptureAssistantDialog._tokenize(line)
        nums = [i for i, (_t, is_num) in enumerate(toks) if is_num]
        # Capture only the second numeric (RPM), not the [123] index
        pattern = self._generate(line, {nums[1]: "RPM"})
        m = re.search(pattern, line)
        assert m is not None
        assert m.group("RPM") == "1461"
        assert "V" not in m.groupdict()

    def test_pattern_matches_other_lines_with_same_shape(self):
        """The whole point — pattern generated from one sample matches
        future lines of the same shape."""
        sample = "RPM: 1461, V: 12.20"
        toks = _CaptureAssistantDialog._tokenize(sample)
        nums = [i for i, (_t, is_num) in enumerate(toks) if is_num]
        pattern = self._generate(sample, {nums[0]: "RPM", nums[1]: "V"})

        for next_line in [
            "RPM: 893, V: 12.05",
            "RPM: 1500, V: 11.98",
            "RPM: 0, V: 0.00",
        ]:
            m = re.search(pattern, next_line)
            assert m is not None, f"pattern failed on {next_line!r}"

    def test_pattern_doesnt_match_unrelated_lines(self):
        """Pattern preserves enough of the original shape to reject
        unrelated formats."""
        sample = "RPM: 1461, V: 12.20"
        toks = _CaptureAssistantDialog._tokenize(sample)
        nums = [i for i, (_t, is_num) in enumerate(toks) if is_num]
        pattern = self._generate(sample, {nums[0]: "RPM", nums[1]: "V"})

        for unrelated in [
            "WARNING: thermal shutdown",
            "INFO: heartbeat",
            "Temp: 25.0, Humidity: 50",  # different keys
        ]:
            assert re.search(pattern, unrelated) is None, \
                f"pattern incorrectly matched {unrelated!r}"
