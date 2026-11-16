# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for core.highlight_rules."""

from __future__ import annotations

import json

import pytest

from app.constants import HIGHLIGHT_COLOURS, HIGHLIGHT_RULES_MAX, HIGHLIGHT_TEXT_MAX
from core.highlight_rules import (
    KEY,
    SECTION,
    HighlightMatcher,
    HighlightRule,
    HighlightStore,
    clean_rules,
)


class FakeConfig:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get(self, section: str, key: str, fallback: str | None = None) -> str:
        return self.values.get((section, key), fallback)

    def set(self, section: str, key: str, value: str) -> None:
        self.values[(section, key)] = value


def test_the_first_matching_rule_decides_ignoring_case() -> None:
    matcher = HighlightMatcher((HighlightRule("Timeout", 1), HighlightRule("wifi", 5)))

    assert matcher.colour_of("E (12) WIFI: TIMEOUT") == 1
    assert matcher.colour_of("wifi connected") == 5
    assert matcher.colour_of("all good") is None


def test_no_rules_match_nothing() -> None:
    matcher = HighlightMatcher()

    assert not matcher
    assert matcher.colour_of("anything") is None


@pytest.mark.parametrize("raw", [
    None, "x", [1], [{"text": "", "colour": 1}], [{"text": "  ", "colour": 1}],
    [{"text": "a", "colour": 0}], [{"text": "a", "colour": HIGHLIGHT_COLOURS + 1}],
    [{"text": "a", "colour": True}], [{"text": 5, "colour": 1}],
])
def test_rules_that_are_not_valid_are_dropped(raw) -> None:
    assert clean_rules(raw) == ()


def test_texts_are_cut_and_rules_limited() -> None:
    raw = [{"text": "x" * (HIGHLIGHT_TEXT_MAX + 9), "colour": 2}] * (HIGHLIGHT_RULES_MAX + 3)

    rules = clean_rules(raw)

    assert len(rules) == HIGHLIGHT_RULES_MAX
    assert len(rules[0].text) == HIGHLIGHT_TEXT_MAX


def test_rules_survive_a_round_trip_through_the_config() -> None:
    config = FakeConfig()
    store = HighlightStore(config)

    store.set_rules((HighlightRule("boot", 3), HighlightRule("panic", 1)))

    assert json.loads(config.values[(SECTION, KEY)]) == [
        {"text": "boot", "colour": 3}, {"text": "panic", "colour": 1},
    ]
    assert HighlightStore(config).rules() == (HighlightRule("boot", 3), HighlightRule("panic", 1))


@pytest.mark.parametrize("stored", ["{not json", "[" * 100_000])
def test_unreadable_stored_rules_are_ignored(stored: str) -> None:
    config = FakeConfig()
    config.set(SECTION, KEY, stored)

    assert HighlightStore(config).rules() == ()


def test_every_theme_has_a_colour_for_each_highlight_and_ansi_colour() -> None:
    from app.constants import HIGHLIGHT_COLOUR_NAMES
    from ui.themes import tokens

    for palette in tokens.PALETTES.values():
        assert len(palette.highlights) == HIGHLIGHT_COLOURS == len(HIGHLIGHT_COLOUR_NAMES)
        assert len(palette.ansi) == 16
