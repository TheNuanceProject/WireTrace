# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for app.saved_commands: the commands saved to send again."""

from __future__ import annotations

import json

import pytest

from app.constants import SAVED_COMMAND_MAX_LENGTH, SAVED_COMMANDS_MAX
from app.saved_commands import KEY, SECTION, SavedCommands, clean


class FakeConfig:
    """Holds settings in memory the way ConfigManager holds them in a file."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get(self, section: str, key: str, fallback: str | None = None) -> str:
        return self.values.get((section, key), fallback or "")

    def set(self, section: str, key: str, value: object) -> None:
        self.values[(section, key)] = str(value)


@pytest.fixture
def config() -> FakeConfig:
    return FakeConfig()


@pytest.fixture
def commands(config: FakeConfig) -> SavedCommands:
    return SavedCommands(config)


def test_nothing_is_saved_at_first(commands: SavedCommands) -> None:
    assert commands.all() == []


def test_commands_are_kept_in_the_order_they_were_added(commands: SavedCommands) -> None:
    assert commands.add("status") is True
    assert commands.add("  reset  ") is True

    assert commands.all() == ["status", "reset"]


def test_commands_are_shared_by_every_reader_of_the_config(config: FakeConfig) -> None:
    SavedCommands(config).add("status")

    assert SavedCommands(config).all() == ["status"]


@pytest.mark.parametrize("command", ["", "   ", "status", " status "])
def test_empty_or_repeated_command_is_not_added(commands: SavedCommands, command: str) -> None:
    commands.add("status")

    assert commands.can_add(command) is False
    assert commands.add(command) is False
    assert commands.all() == ["status"]


def test_list_is_full_at_the_limit(commands: SavedCommands) -> None:
    for number in range(SAVED_COMMANDS_MAX):
        assert commands.add(f"command {number}") is True

    assert commands.can_add("one more") is False
    assert commands.add("one more") is False
    assert len(commands.all()) == SAVED_COMMANDS_MAX


def test_replace_sets_the_whole_list_cleaned(commands: SavedCommands) -> None:
    commands.add("old")

    commands.replace(["  b ", "a", "b", "", "c"])

    assert commands.all() == ["b", "a", "c"]


def test_clean_keeps_text_only_and_cuts_what_is_too_long() -> None:
    long = "x" * (SAVED_COMMAND_MAX_LENGTH + 10)

    assert clean(["ok", 7, None, ["nested"], long]) == ["ok", "x" * SAVED_COMMAND_MAX_LENGTH]
    assert len(clean(f"c{n}" for n in range(SAVED_COMMANDS_MAX + 5))) == SAVED_COMMANDS_MAX


def test_text_outside_ascii_survives(commands: SavedCommands) -> None:
    commands.add("größe 5 Ω")

    assert commands.all() == ["größe 5 Ω"]


@pytest.mark.parametrize("stored", ["not json", '{"a": 1}', '"text"', "["])
def test_unreadable_stored_data_is_ignored(config: FakeConfig, stored: str) -> None:
    config.set(SECTION, KEY, stored)
    commands = SavedCommands(config)

    assert commands.all() == []
    assert commands.add("status") is True
    assert json.loads(config.get(SECTION, KEY)) == ["status"]
