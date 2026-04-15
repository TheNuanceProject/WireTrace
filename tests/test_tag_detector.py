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
