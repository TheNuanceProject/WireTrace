# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for ui.shortcuts: the one table of keyboard shortcuts."""

from __future__ import annotations

from collections import Counter

from ui import shortcuts


def _normalised(keys: str) -> str:
    return "+".join(sorted(part.lower() for part in keys.split("+") if part)) + (
        "++" if keys.endswith("++") else ""
    )


def test_no_key_combination_is_used_twice() -> None:
    every = [shortcut.keys for shortcut in shortcuts.SHORTCUTS.values()]
    for alternates in shortcuts.ALTERNATE_KEYS.values():
        every.extend(alternates)
    repeated = [keys for keys, count in Counter(map(_normalised, every)).items() if count > 1]
    assert not repeated


def test_every_shortcut_is_described_and_grouped() -> None:
    for name, shortcut in shortcuts.SHORTCUTS.items():
        assert shortcut.keys, name
        assert shortcut.description, name
        assert shortcut.group in {"File", "Edit", "View", "Device", "Help"}, name


def test_groups_are_listed_together() -> None:
    groups = [shortcut.group for shortcut in shortcuts.SHORTCUTS.values()]
    runs = [group for index, group in enumerate(groups) if index == 0 or groups[index - 1] != group]
    assert len(runs) == len(set(runs))


def test_alternate_keys_belong_to_known_actions() -> None:
    assert set(shortcuts.ALTERNATE_KEYS) <= set(shortcuts.SHORTCUTS)


def test_keys_returns_the_combination_of_an_action() -> None:
    assert shortcuts.keys("find") == "Ctrl+F"
