# SPDX-License-Identifier: MIT
"""WireTrace severity-based message classifier.

Classifies serial messages into exactly 7 tags:
  CRITICAL — fatal, critical, panic, crash
  ERROR    — error, fail, exception, fault
  WARNING  — warn, warning, caution
  INFO     — info, connected, ready, started, success, ok, initialized, complete
  DEBUG    — debug, trace, verbose
  COMMAND  — user-sent commands (assigned externally, not by keyword)
  DATA     — everything else (default)

Matching rule:
  Keywords are matched at a left word boundary, so they cannot match as
  a substring embedded inside an unrelated word. For example, "fault"
  matches in "fault detected" but NOT inside "default"; "ok" matches
  in "HTTP 200 ok" but NOT inside "token". The right side is
  intentionally unbounded so morphological suffixes still hit
  ("fail" matches "failed"/"failing", "warn" matches "warning"/"warned").

  Standalone words that are simultaneously legitimate data labels and
  severity keywords (e.g. "trace" in "Trace element: 0.003",
  "connected" in "Nodes connected: 5") remain ambiguous — keyword
  classification cannot resolve those without sentence-level
  understanding. The miscolouring is cosmetic; no data is lost.

Design principles:
  - Priority order: most severe tag wins
  - Case-insensitive matching
  - Zero guessing: no protocol detection, no heuristics
  - Predictable: same input always produces same output
  - Patterns are precompiled once at import; detection is stateless

This module does NOT touch: GUI, disk, serial, or any I/O.
"""

import re
from typing import ClassVar

from app.constants import SEVERITY_KEYWORDS, TAG_COMMAND, TAG_DATA


def _build_severity_pattern(keywords):
    """Compile a case-insensitive, left-word-boundary regex for ``keywords``.

    The pattern anchors matches with ``\\b`` on the left only. This
    eliminates substring-inside-word false positives (e.g. "fault"
    embedded in "default", "ok" embedded in "token") while still
    allowing common verb/noun suffixes ("fail" → "failed",
    "warn" → "warning"). ``re.escape`` keeps the builder safe if
    future keywords ever contain regex metacharacters.
    """
    alternation = "|".join(re.escape(kw) for kw in keywords)
    return re.compile(rf"\b(?:{alternation})", re.IGNORECASE)


class TagDetector:
    """Classifies serial messages by severity. 7 tags, zero guessing."""

    # Priority order: most severe first. Ensures CRITICAL is checked
    # before ERROR, ERROR before WARNING, etc.
    _PRIORITY_ORDER: ClassVar[tuple[str, ...]] = (
        "CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG",
    )

    # Precompiled patterns — one per severity tag. Built once at class
    # definition; immutable thereafter. Word-boundary matching
    # eliminates the substring-inside-word false positives that the
    # earlier ``kw in message`` approach produced.
    _PATTERNS: ClassVar[dict[str, re.Pattern[str]]] = {
        tag: _build_severity_pattern(SEVERITY_KEYWORDS[tag])
        for tag in _PRIORITY_ORDER
    }

    @staticmethod
    def detect(message: str, data_type: str = TAG_DATA) -> str:
        """Classify a serial message into one of 7 severity tags.

        Severity keywords are matched at a left word boundary, so a
        keyword embedded inside another word (e.g. "fault" inside
        "default", "ok" inside "token") does not trigger a match.

        Args:
            message: The decoded serial line to classify.
            data_type: If "COMMAND", returns COMMAND tag immediately
                       (used for user-sent commands via CommandBar).

        Returns:
            One of: "CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG",
                    "COMMAND", or "DATA".
        """
        if data_type == TAG_COMMAND:
            return TAG_COMMAND

        # Priority order: most severe first.
        for tag in TagDetector._PRIORITY_ORDER:
            if TagDetector._PATTERNS[tag].search(message):
                return tag

        return TAG_DATA
