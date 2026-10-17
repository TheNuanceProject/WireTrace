# SPDX-License-Identifier: GPL-3.0-or-later
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


    @pytest.mark.parametrize(
        "message",
        ["link ok, then a fatal error", "fatal error after link ok"],
    )
    def test_most_severe_keyword_wins_wherever_it_stands(self, message):
        assert TagDetector.detect(message) == "CRITICAL"


class TestCaseInsensitive:
    """Keyword matching ignores case."""

    @pytest.mark.parametrize("message", ["ERROR", "Error", "error", "eRrOr"])
    def test_case_variants_all_match(self, message):
        assert TagDetector.detect(message) == "ERROR"


class TestFallback:
    """Lines with no severity keyword fall back to DATA."""

    def test_plain_data_line(self):
        assert TagDetector.detect("Temperature: 25.3C") == "DATA"

    def test_empty_string(self):
        assert TagDetector.detect("") == "DATA"


class TestWordBoundaryFalsePositives:
    """Keywords embedded in the middle of a word do not match.

    Matching is anchored to a left word boundary, so "fault" inside
    "default" and "ok" inside "token" do not classify a line.

    A keyword at the start of a longer word (for example "info" in
    "InfoLog", "fault" in "fault-tolerant") still matches. That is
    intended: "fail" must go on matching "failed" and "failing".
    """

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            # "fault" inside "Default"
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
            # "informational" is INFO by design: the left-boundary rule
            # that lets "failed" and "warning" match applies here too.
            ("informational note", "INFO"),
            ("information available", "INFO"),
            # "connected" keyword (note: full word "connected", not
            # stem "connect", so "connection" does not match: this
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
    only colours the line in the console: the data is still written to
    disk completely, and CSV rows are chosen by content, never by tag.

    These tests hold the behaviour as it is, so that a change to the
    classifier is a deliberate decision.
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


class TestFirmwareLevelMarkers:
    """An explicit level marker printed by the firmware decides the tag."""

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("E (1234) wifi: connect to the AP fail", "ERROR"),
            ("W (300) spi_flash: detected chip size mismatch", "WARNING"),
            ("I (5012) main: no error found", "INFO"),
            ("D (77) nvs: nvs_open_from_partition", "DEBUG"),
            ("V (78) intr_alloc: failed lookup", "DEBUG"),
            ("[00:00:01.234,000] <err> net_core: packet dropped", "ERROR"),
            ("[00:00:02.000,000] <wrn> bt_hci: fault recovered", "WARNING"),
            ("[00:00:03.100,000] <inf> app: fatal flag cleared", "INFO"),
            ("[00:00:04.200,000] <dbg> sensor: warning threshold 3", "DEBUG"),
            ("[ERROR] sensor timeout", "ERROR"),
            ("[warn] retrying", "WARNING"),
            ("2026-01-02 10:00:00 [INFO] error count: 0", "INFO"),
            ("[FATAL] stack overflow", "CRITICAL"),
            ("[TRACE] entering loop", "DEBUG"),
        ],
    )
    def test_marker_decides_the_tag(self, message, expected):
        assert TagDetector.detect(message) == expected

    @pytest.mark.parametrize(
        "message",
        [
            "E 1234 not an esp-idf line",
            "I(12) missing space",
            "values <error> in angle brackets",
            "[ERRORS] plural is not a level",
        ],
    )
    def test_near_misses_fall_through_to_keywords_or_data(self, message):
        marker = TagDetector._tag_from_level_marker(message)
        assert marker is None

    def test_line_without_marker_or_keyword_is_data(self):
        assert TagDetector.detect("RPM: 1465, Current: 2.45") == "DATA"



class TestLettersOutsideAscii:
    """Case-insensitive matching treats a few non-ASCII letters as their
    ASCII counterparts (dotted and dotless i, long s). Such a line must
    be classified like any other, never rejected."""

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("SENSOR FA\u0130L", "ERROR"),
            ("sensor fa\u0131l", "ERROR"),
            ("cra\u017fh in task", "CRITICAL"),
            ("\u0130NFO boot", "INFO"),
            ("d\u00e9marrage termin\u00e9", "DATA"),
            ("\u6e29\u5ea6: 21.5", "DATA"),
        ],
    )
    def test_line_is_classified(self, message, expected):
        assert TagDetector.detect(message) == expected
