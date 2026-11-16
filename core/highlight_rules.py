# SPDX-License-Identifier: GPL-3.0-or-later
"""Highlight rules: lines that hold a chosen text, drawn in a chosen colour.

A rule is a piece of text and one of the highlight colours (numbered
from 1, in the order of the design language's highlight colours). A
line that holds the text, ignoring case, is drawn in the colour of the
first rule it matches. Matching is a plain search for the text, so it
costs little and the same for any line: no pattern the user types can
make the console slow.

The rules are kept in ``preferences.ini`` under ``[Highlights]`` as a
JSON list, with the user's other data (they stay when the defaults are
restored).

This module does not touch the GUI, the disk, the serial port or any other I/O.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.constants import HIGHLIGHT_COLOURS, HIGHLIGHT_RULES_MAX, HIGHLIGHT_TEXT_MAX

if TYPE_CHECKING:
    from app.config import ConfigManager

logger = logging.getLogger(__name__)

SECTION = "Highlights"
KEY = "rules_json"


@dataclass(frozen=True)
class HighlightRule:
    """Lines holding ``text``, ignoring case, are drawn in highlight colour ``colour``."""

    text: str
    colour: int

    def to_dict(self) -> dict[str, object]:
        return {"text": self.text, "colour": self.colour}


def clean_rules(raw: object) -> tuple[HighlightRule, ...]:
    """The valid rules of stored data: text not blank, colour known, at most the limit."""
    if not isinstance(raw, list):
        return ()
    rules: list[HighlightRule] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text, colour = item.get("text"), item.get("colour")
        if not isinstance(text, str) or not text.strip():
            continue
        if not isinstance(colour, int) or isinstance(colour, bool) or not 1 <= colour <= HIGHLIGHT_COLOURS:
            continue
        rules.append(HighlightRule(text[:HIGHLIGHT_TEXT_MAX], colour))
        if len(rules) == HIGHLIGHT_RULES_MAX:
            break
    return tuple(rules)


class HighlightMatcher:
    """Finds the colour of a line, if a rule matches it."""

    def __init__(self, rules: tuple[HighlightRule, ...] = ()) -> None:
        self._rules = tuple((rule.text.casefold(), rule.colour) for rule in rules)

    def __bool__(self) -> bool:
        return bool(self._rules)

    def colour_of(self, text: str) -> int | None:
        """The colour of the first rule ``text`` matches, or None."""
        if not self._rules:
            return None
        folded = text.casefold()
        for needle, colour in self._rules:
            if needle in folded:
                return colour
        return None


class HighlightStore:
    """Reads and writes the highlight rules."""

    def __init__(self, config: ConfigManager) -> None:
        self._config = config

    def rules(self) -> tuple[HighlightRule, ...]:
        raw = self._config.get(SECTION, KEY, fallback="[]")
        try:
            return clean_rules(json.loads(raw))
        except (ValueError, RecursionError) as exc:
            logger.warning("Highlight rules are unreadable and are ignored: %s", exc)
            return ()

    def set_rules(self, rules: tuple[HighlightRule, ...]) -> None:
        cleaned = clean_rules([rule.to_dict() for rule in rules])
        self._config.set(SECTION, KEY, json.dumps([rule.to_dict() for rule in cleaned]))
