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

Design principles:
  - Priority order: most severe tag wins
  - Case-insensitive keyword matching
  - Zero guessing: no protocol detection, no regex patterns
  - Predictable: same input always produces same output

This module does NOT touch: GUI, disk, serial, or any I/O.
"""

from app.constants import SEVERITY_KEYWORDS, TAG_COMMAND, TAG_DATA


class TagDetector:
    """Classifies serial messages by severity. 7 tags, zero guessing."""

    # Pre-built ordered list for priority matching (most severe first).
    # This ensures CRITICAL is checked before ERROR, ERROR before WARNING, etc.
    _PRIORITY_ORDER = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")

    @staticmethod
    def detect(message: str, data_type: str = TAG_DATA) -> str:
        """Classify a serial message into one of 7 severity tags.

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

        msg_lower = message.lower()

        # Priority order: most severe first
        for tag in TagDetector._PRIORITY_ORDER:
            keywords = SEVERITY_KEYWORDS[tag]
            if any(kw in msg_lower for kw in keywords):
                return tag

        return TAG_DATA
