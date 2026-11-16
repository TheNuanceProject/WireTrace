# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for core.serial_settings: how a port is set up."""

from __future__ import annotations

import pytest

from core.serial_settings import (
    FlowControl,
    LineSettings,
    Parity,
    StopBits,
    parities_for,
    stop_bits_for,
)


def test_the_usual_settings_are_8n1_without_flow_control_and_both_lines_on() -> None:
    settings = LineSettings()

    assert settings.framing == "8N1"
    assert settings.describe() == "8N1, no flow control"
    assert settings.dtr_on_open and settings.rts_on_open
    assert settings.has_usual_framing()


@pytest.mark.parametrize(
    ("settings", "framing", "description"),
    [
        (LineSettings(7, Parity.EVEN, StopBits.ONE), "7E1", "7E1, no flow control"),
        (LineSettings(8, Parity.ODD, StopBits.TWO, FlowControl.HARDWARE), "8O2",
         "8O2, RTS/CTS flow control"),
        (LineSettings(5, Parity.MARK, StopBits.ONE_AND_HALF, FlowControl.SOFTWARE), "5M1.5",
         "5M1.5, XON/XOFF flow control"),
        (LineSettings(6, Parity.SPACE), "6S1", "6S1, no flow control"),
    ],
)
def test_framing_is_written_the_usual_short_way(settings, framing, description) -> None:
    assert settings.framing == framing
    assert settings.describe() == description


def test_only_the_lines_at_open_differing_keeps_the_usual_framing() -> None:
    assert LineSettings(dtr_on_open=False, rts_on_open=False).has_usual_framing()
    assert not LineSettings(flow_control=FlowControl.SOFTWARE).has_usual_framing()


def test_rts_is_manual_unless_hardware_flow_control_drives_it() -> None:
    assert LineSettings().rts_is_manual
    assert LineSettings(flow_control=FlowControl.SOFTWARE).rts_is_manual
    assert not LineSettings(flow_control=FlowControl.HARDWARE).rts_is_manual


@pytest.mark.parametrize(
    ("platform", "parities", "stop_bits"),
    [
        ("win32", set(Parity), set(StopBits)),
        ("linux", set(Parity), {StopBits.ONE, StopBits.TWO}),
        ("darwin", {Parity.NONE, Parity.EVEN, Parity.ODD}, {StopBits.ONE, StopBits.TWO}),
    ],
)
def test_only_choices_the_system_supports_are_offered(platform, parities, stop_bits) -> None:
    assert set(parities_for(platform)) == parities
    assert set(stop_bits_for(platform)) == stop_bits


def test_settings_survive_a_round_trip_through_stored_data() -> None:
    settings = LineSettings(7, Parity.ODD, StopBits.TWO, FlowControl.HARDWARE, False, False)

    assert LineSettings.from_dict(settings.to_dict(), "linux") == settings


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "8N1",
        {"data_bits": 9},
        {"data_bits": True},
        {"data_bits": "8"},
        {"parity": "sideways"},
        {"stop_bits": 1},
        {"flow_control": "magic"},
        {"dtr_on_open": "no"},
    ],
)
def test_values_that_are_not_valid_take_their_defaults(raw) -> None:
    assert LineSettings.from_dict(raw, "linux") == LineSettings()


def test_a_choice_the_system_cannot_make_takes_its_default() -> None:
    stored = LineSettings(8, Parity.MARK, StopBits.ONE_AND_HALF).to_dict()

    loaded = LineSettings.from_dict(stored, "darwin")

    assert loaded.parity is Parity.NONE
    assert loaded.stop_bits is StopBits.ONE
    assert LineSettings.from_dict(stored, "win32").parity is Parity.MARK


def test_supported_on_checks_every_part() -> None:
    assert LineSettings().supported_on("darwin")
    assert not LineSettings(parity=Parity.SPACE).supported_on("darwin")
    assert not LineSettings(stop_bits=StopBits.ONE_AND_HALF).supported_on("linux")
    assert LineSettings(stop_bits=StopBits.ONE_AND_HALF).supported_on("win32")
