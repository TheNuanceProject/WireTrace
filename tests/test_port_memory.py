# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for app.port_memory: what each port was last used with."""

from __future__ import annotations

import json

import pytest

from app.constants import PORT_MEMORY_LIMIT, PORT_TAB_NAME_MAX, LineEnding
from app.port_memory import KEY, SECTION, PortMemory, PortSettings

_NAMES = {LineEnding.NONE: "None", LineEnding.LF: "LF", LineEnding.CR: "CR", LineEnding.CRLF: "CR LF"}


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
def memory(config: FakeConfig) -> PortMemory:
    return PortMemory(config)


def test_unknown_port_has_no_settings(memory: PortMemory) -> None:
    assert memory.get("PORT0") is None
    assert memory.note("PORT0", _NAMES) == ""


def test_settings_are_remembered_per_port(memory: PortMemory) -> None:
    memory.remember("PORT0", baud_rate=9600, line_ending="CRLF")
    memory.remember("PORT1", baud_rate=115200)

    assert memory.get("PORT0") == PortSettings(baud_rate=9600, line_ending="CRLF")
    assert memory.get("PORT1") == PortSettings(baud_rate=115200)


def test_a_later_change_keeps_the_other_settings(memory: PortMemory) -> None:
    memory.remember("PORT0", baud_rate=9600, tab_name="Pump")
    memory.remember("PORT0", plot_profile="Motor")

    assert memory.get("PORT0") == PortSettings(
        baud_rate=9600, plot_profile="Motor", tab_name="Pump",
    )


def test_settings_survive_a_new_memory_on_the_same_config(config: FakeConfig) -> None:
    PortMemory(config).remember("PORT0", baud_rate=57600)

    assert PortMemory(config).get("PORT0") == PortSettings(baud_rate=57600)


def test_note_says_what_the_port_was_last_used_with(memory: PortMemory) -> None:
    memory.remember("PORT0", baud_rate=115200, line_ending="CRLF", plot_profile="Motor")

    assert memory.note("PORT0", _NAMES) == "Last used at 115200, CR LF, plot profile Motor"


def test_note_needs_a_baud_rate(memory: PortMemory) -> None:
    memory.remember("PORT0", tab_name="Pump")

    assert memory.note("PORT0", _NAMES) == ""


def test_line_ending_choice_is_the_enum_member(memory: PortMemory) -> None:
    memory.remember("PORT0", line_ending="CR")

    assert memory.get("PORT0").line_ending_choice() is LineEnding.CR
    assert PortSettings().line_ending_choice() is None


@pytest.mark.parametrize(
    ("change", "stored"),
    [
        ({"baud_rate": -5}, PortSettings()),
        ({"baud_rate": "fast"}, PortSettings()),
        ({"line_ending": "TAB"}, PortSettings()),
        ({"plot_profile": 7}, PortSettings()),
        ({"tab_name": "x" * (PORT_TAB_NAME_MAX + 10)}, PortSettings(tab_name="x" * PORT_TAB_NAME_MAX)),
    ],
)
def test_values_that_are_not_valid_are_not_kept(
    memory: PortMemory, change: dict[str, object], stored: PortSettings,
) -> None:
    memory.remember("PORT0", **change)

    assert memory.get("PORT0") == stored


def test_a_port_without_a_name_is_not_remembered(memory: PortMemory, config: FakeConfig) -> None:
    memory.remember("", baud_rate=9600)

    assert config.values == {}


def test_the_least_recently_used_port_is_forgotten_first(memory: PortMemory) -> None:
    for number in range(PORT_MEMORY_LIMIT):
        memory.remember(f"PORT{number}", baud_rate=9600)
    memory.remember("PORT0", baud_rate=19200)
    memory.remember("NEW", baud_rate=9600)

    assert memory.get("PORT1") is None
    assert memory.get("PORT0") == PortSettings(baud_rate=19200)
    assert memory.get("NEW") is not None


@pytest.mark.parametrize("stored", ["not json", "[1, 2]", '"text"', "{"])
def test_unreadable_stored_data_is_ignored(config: FakeConfig, stored: str) -> None:
    config.set(SECTION, KEY, stored)
    memory = PortMemory(config)

    assert memory.get("PORT0") is None
    memory.remember("PORT0", baud_rate=9600)
    assert memory.get("PORT0") == PortSettings(baud_rate=9600)


def test_damaged_entries_are_dropped_and_the_rest_kept(config: FakeConfig) -> None:
    config.set(SECTION, KEY, json.dumps({
        "GOOD": {"baud_rate": 9600},
        "BAD": "nonsense",
        "": {"baud_rate": 9600},
        "PARTLY": {"baud_rate": 10**12, "line_ending": "LF", "extra": True},
    }))
    memory = PortMemory(config)

    assert memory.get("GOOD") == PortSettings(baud_rate=9600)
    assert memory.get("BAD") is None
    assert memory.get("") is None
    assert memory.get("PARTLY") == PortSettings(line_ending="LF")
