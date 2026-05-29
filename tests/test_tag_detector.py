# SPDX-License-Identifier: MIT
"""Tests for the severity-based tag detector.

The detector runs on every serial line, so correctness and priority
ordering matter. These tests lock in the 7-tag taxonomy.
"""

from __future__ import annotations

import pytest

from core.tag_detector import TagDetector


class TestBasicDetection:
    """Each severity keyword produces its expected tag."""

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            # CRITICAL keywords
            ("FATAL: kernel panic", "CRITICAL"),
            ("critical failure in subsystem", "CRITICAL"),
            ("system panic", "CRITICAL"),
            ("unexpected crash", "CRITICAL"),
            # ERROR keywords
            ("ERROR: timeout", "ERROR"),
            ("operation failed", "ERROR"),
            ("unhandled exception", "ERROR"),
            ("fault detected on bus", "ERROR"),
            # WARNING keywords
            ("warning: low memory", "WARNING"),
            ("warn: retry in 5s", "WARNING"),
            ("caution: high temperature", "WARNING"),
            # INFO keywords
            ("info: startup complete", "INFO"),
            ("device connected", "INFO"),
            ("ready for commands", "INFO"),
            ("system started", "INFO"),
            ("operation success", "INFO"),
            ("HTTP 200 ok", "INFO"),
            ("sensor initialized", "INFO"),
            ("task complete", "INFO"),
            # DEBUG keywords
            ("DEBUG: state=idle", "DEBUG"),
            ("trace enabled", "DEBUG"),
            ("verbose: all modules loaded", "DEBUG"),
        ],
    )
    def test_keyword_matching(self, message, expected):
        assert TagDetector.detect(message) == expected


class TestPriorityOrder:
    """When a line contains multiple severity keywords, the most severe wins."""

    def test_critical_beats_error(self):
        assert TagDetector.detect("critical error in module") == "CRITICAL"

    def test_error_beats_warning(self):
        assert TagDetector.detect("error: warning ignored") == "ERROR"

    def test_warning_beats_info(self):
        assert TagDetector.detect("not ready: warning raised") == "WARNING"

    def test_info_beats_debug(self):
        assert TagDetector.detect("debug: device connected") == "INFO"


class TestCaseInsensitive:
    """Keyword matching ignores case."""

    @pytest.mark.parametrize("message", ["ERROR", "Error", "error", "eRrOr"])
    def test_case_variants_all_match(self, message):
        assert TagDetector.detect(message) == "ERROR"


class TestFallback:
    """Lines with no severity keyword fall back to DATA (or COMMAND)."""

    def test_plain_data_line(self):
        assert TagDetector.detect("Temperature: 25.3C") == "DATA"

    def test_empty_string(self):
        assert TagDetector.detect("") == "DATA"

    def test_explicit_command_type(self):
        # data_type="COMMAND" returns immediately — keywords irrelevant.
        assert (
            TagDetector.detect("ERROR: this is actually a command", "COMMAND")
            == "COMMAND"
        )


class TestWordBoundaryFalsePositives:
    """Substring-inside-word fix: keywords embedded mid-word do NOT match.

    The earlier ``kw in message`` matching incorrectly classified data
    lines whose words happened to contain a severity keyword as an
    embedded substring (e.g. "fault" inside "default", "ok" inside
    "token"). The word-boundary fix anchors keyword matching to a left
    word boundary, eliminating this class of false positive.

    Note: keywords appearing as a *prefix* of a longer word (e.g. "info"
    in "InfoLog", "fault" in "fault-tolerant") still match — this is
    intentional and matches the spirit of the original design where
    "fail" must continue to match "failed"/"failing".
    """

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            # The headline case from the audit: "fault" inside "Default"
            ("Default: 100, Override: 50", "DATA"),
            ("Default value: 42", "DATA"),
            # "ok" embedded mid-word in common English words
            ("Token verified", "DATA"),
            ("Broken pipe", "DATA"),
            ("Lookup table loaded", "DATA"),
            ("Bookmark saved", "DATA"),
            ("Cookies enabled: yes", "DATA"),
        ],
    )
    def test_substring_in_middle_does_not_match(self, message, expected):
        assert TagDetector.detect(message) == expected


class TestMorphologicalVariants:
    """Stem keywords still match common verb/noun variants.

    Left-boundary matching (``\\bkw``, not ``\\bkw\\b``) preserves the
    original detector's prefix-style coverage so callers don't have to
    enumerate every form (failed, failing, failure, warning, warned).
    """

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            # "fail" stem
            ("operation failing intermittently", "ERROR"),
            ("failure mode active", "ERROR"),
            # "warn" stem
            ("warned about deprecation", "WARNING"),
            ("warnings present", "WARNING"),
            # "info" stem (rare prefix usage; kept consistent)
            ("InfoLog ready", "INFO"),
            # The "informational" case was an explicit Help-Guide
            # vs code mismatch flagged in the v1.1.0 claim audit.
            # The detector intentionally classifies this as INFO
            # because the same left-boundary rule lets "failed" and
            # "warning" match correctly. The Help Guide was rewritten
            # to document this accurately; this test pins the
            # behaviour so future detector changes don't regress.
            ("informational note", "INFO"),
            ("information available", "INFO"),
            # "connected" keyword (note: full word "connected", not
            # stem "connect", so "connection" does not match — this
            # is intentional)
            ("connected to WiFi", "INFO"),
            ("connected successfully", "INFO"),
        ],
    )
    def test_stem_matches_morphological_variants(self, message, expected):
        assert TagDetector.detect(message) == expected


class TestPunctuationAdjacent:
    """Word boundaries are correctly handled adjacent to punctuation.

    Punctuation characters are non-word characters, so they form valid
    word boundaries on either side of a keyword.
    """

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("ok!", "INFO"),
            ("ok.", "INFO"),
            ("[ERROR] disk full", "ERROR"),
            ("(warning) check fuses", "WARNING"),
            ("FATAL.", "CRITICAL"),
        ],
    )
    def test_punctuation_does_not_block_match(self, message, expected):
        assert TagDetector.detect(message) == expected


class TestRemainingAmbiguities:
    """Standalone-word ambiguities that keyword classification cannot resolve.

    Some severity keywords legitimately appear as standalone data labels
    in structured serial data: "Fail" as a counter label, "ok" as a
    status value, "Trace" as an analytical-chemistry data label,
    "connected" / "complete" as status fields. Tagging these as severity
    is a cosmetic miscolouring in the console — the data is still
    written to disk completely, and the CSV detection layer's tag-aware
    sampling tolerates them.

    These tests pin the *current* behaviour. They exist so that any
    future change to the classifier (e.g. context-aware tagging) is a
    deliberate decision, not an accidental regression.
    """

    @pytest.mark.parametrize(
        ("message", "tag_assigned"),
        [
            ("Pass: 98, Fail: 2, Total: 100", "ERROR"),
            ("Smoke: 0.05, CO: ok, Temp: 25",  "INFO"),
            ("Trace element: 0.003",            "DEBUG"),
            ("Nodes connected: 5",              "INFO"),
            ("Cycle complete: 45ms",            "INFO"),
        ],
    )
    def test_standalone_word_ambiguity_still_present(
        self, message, tag_assigned,
    ):
        assert TagDetector.detect(message) == tag_assigned
