# SPDX-License-Identifier: GPL-3.0-or-later
"""WireTrace severity-based message classifier.

Classifies serial messages into exactly 7 tags:
  CRITICAL: fatal, critical, panic, crash
  ERROR   : error, fail, exception, fault
  WARNING : warn, warning, caution
  INFO    : info, connected, ready, started, success, ok, initialized, complete
  DEBUG   : debug, trace, verbose
  COMMAND : user-sent commands (assigned externally, not by keyword)
  DATA    : everything else (default)

Matching order:
  1. Firmware level markers. Logging frameworks print an explicit level
     with every line, and that level is exact:
       ESP-IDF   ``E (1234) wifi: ...``   (E, W, I, D, V)
       Zephyr    ``[00:00:01.234,000] <err> net: ...``
       Generic   ``[ERROR] ...``, ``[WARN] ...``
     A recognised marker decides the tag, so ``[INFO] error count: 0``
     is INFO, not ERROR.
  2. Severity keywords, for lines with no marker.

Keyword rule:
  Keywords are matched at a left word boundary, so they cannot match as
  a substring embedded inside an unrelated word. For example, "fault"
  matches in "fault detected" but not inside "default"; "ok" matches
  in "HTTP 200 ok" but not inside "token". The right side is
  intentionally unbounded so morphological suffixes still hit
  ("fail" matches "failed"/"failing", "warn" matches "warning"/"warned").

  Standalone words that are simultaneously legitimate data labels and
  severity keywords (e.g. "trace" in "Trace element: 0.003",
  "connected" in "Nodes connected: 5") remain ambiguous: keyword
  classification cannot resolve those without sentence-level
  understanding. The tag only selects a colour and feeds the filter;
  it never decides whether a line is logged, exported, or plotted.

Design principles:
  - Priority order: most severe tag wins
  - Case-insensitive keyword matching
  - Predictable: same input always produces same output
  - Patterns are precompiled once at import; detection is stateless

This module does not touch the GUI, the disk, the serial port or any other I/O.
"""

import re
from typing import ClassVar

from app.constants import (
    BRACKETED_LEVEL_TAGS,
    ESP_IDF_LEVEL_TAGS,
    SEVERITY_KEYWORDS,
    TAG_DATA,
    ZEPHYR_LEVEL_TAGS,
)


def _alternation(words) -> str:
    return "|".join(re.escape(word) for word in words)


def _build_keyword_pattern(keywords_by_tag) -> re.Pattern[str]:
    """Compile one case-insensitive regex over every severity keyword.

    Each tag's keywords form a group named after the tag, so a match
    says which tag it belongs to without looking the matched text up.

    Matches are anchored with ``\\b`` on the left only. This eliminates
    substring-inside-word false positives (e.g. "fault" embedded in
    "default", "ok" embedded in "token") while still allowing common
    verb/noun suffixes ("fail" matches "failed", "warn" matches "warning").
    ``re.escape`` keeps the builder safe if future keywords ever
    contain regex metacharacters.

    The lookahead on the first letter lets the engine reject a position
    with one comparison instead of trying every keyword there.
    """
    first_letters = "".join(sorted({
        re.escape(word[0]) for words in keywords_by_tag.values() for word in words
    }))
    groups = "|".join(
        f"(?P<{tag}>{_alternation(words)})" for tag, words in keywords_by_tag.items()
    )
    return re.compile(rf"\b(?=[{first_letters}])(?:{groups})", re.IGNORECASE)


class TagDetector:
    """Classifies serial messages into one of seven severity tags."""

    # Priority order: most severe first.
    _PRIORITY_ORDER: ClassVar[tuple[str, ...]] = (
        "CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG",
    )
    _RANK: ClassVar[dict[str, int]] = {
        tag: rank for rank, tag in enumerate(_PRIORITY_ORDER)
    }

    # One pattern over every keyword of every tag, built once. A line
    # is scanned a single time and the most severe tag found wins.
    _ANY_KEYWORD: ClassVar[re.Pattern[str]] = _build_keyword_pattern(
        {tag: SEVERITY_KEYWORDS[tag] for tag in _PRIORITY_ORDER},
    )

    # Firmware level markers (see the module docstring).
    _ESP_IDF_LEVEL: ClassVar[re.Pattern[str]] = re.compile(
        rf"^({_alternation(ESP_IDF_LEVEL_TAGS)}) \(\d+\) ",
    )
    _ZEPHYR_LEVEL: ClassVar[re.Pattern[str]] = re.compile(
        rf"<({_alternation(ZEPHYR_LEVEL_TAGS)})> ",
    )
    _BRACKETED_LEVEL: ClassVar[re.Pattern[str]] = re.compile(
        rf"\[({_alternation(BRACKETED_LEVEL_TAGS)})\]", re.IGNORECASE,
    )

    @staticmethod
    def detect(message: str) -> str:
        """Classify a line received from a device.

        Args:
            message: The decoded serial line to classify.

        Returns:
            One of: "CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG" or
            "DATA". A command the user sent is tagged where it is
            recorded, not here.
        """
        level_tag = TagDetector._tag_from_level_marker(message)
        if level_tag is not None:
            return level_tag

        best_tag = TAG_DATA
        best_rank = len(TagDetector._RANK)
        for match in TagDetector._ANY_KEYWORD.finditer(message):
            tag = match.lastgroup
            rank = TagDetector._RANK[tag]
            if rank < best_rank:
                best_tag, best_rank = tag, rank
                if rank == 0:
                    break
        return best_tag

    @staticmethod
    def _tag_from_level_marker(message: str) -> str | None:
        """Return the tag of an explicit firmware level marker, if any."""
        # Each pattern is only tried on a line that contains the
        # character its marker cannot exist without.
        if "(" in message:
            match = TagDetector._ESP_IDF_LEVEL.match(message)
            if match is not None:
                return ESP_IDF_LEVEL_TAGS[match.group(1)]
        if "<" in message:
            match = TagDetector._ZEPHYR_LEVEL.search(message)
            if match is not None:
                return ZEPHYR_LEVEL_TAGS[match.group(1)]
        if "[" in message:
            match = TagDetector._BRACKETED_LEVEL.search(message)
            if match is not None:
                return BRACKETED_LEVEL_TAGS[match.group(1).upper()]
        return None
