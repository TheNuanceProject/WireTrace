# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for core.ansi: the colours a device sets with ANSI codes."""

from __future__ import annotations

import pytest

from core.ansi import AnsiColours
from core.framing import decode_line

_ESC = b"\x1b"


def _decode(*lines: bytes) -> list[tuple[str, tuple]]:
    colours = AnsiColours()
    return [result for line in lines if (result := colours.decode(line)) is not None]


def test_esp_idf_log_lines_keep_their_text_and_gain_their_colour() -> None:
    error = _ESC + b"[0;31mE (1234) wifi: timeout" + _ESC + b"[0m"
    warning = _ESC + b"[0;33mW (1240) app: low" + _ESC + b"[0m"

    assert _decode(error, warning) == [
        ("E (1234) wifi: timeout", ((0, 22, 1),)),
        ("W (1240) app: low", ((0, 17, 3),)),
    ]


@pytest.mark.parametrize("raw", [
    _ESC + b"[0;31mE (1) x: y" + _ESC + b"[0m",
    b"plain \x07 bell",
    b"tab\tseparated",
    _ESC + b"[1;32mok" + _ESC + b"[0m \x01 done",
    b"",
])
def test_the_text_is_exactly_what_is_logged(raw: bytes) -> None:
    assert AnsiColours().decode(raw)[0] == decode_line(raw)


def test_a_line_of_codes_only_is_no_line_but_sets_the_colour_of_the_next() -> None:
    assert _decode(_ESC + b"[34m", b"blue text", _ESC + b"[0m", b"plain") == [
        ("blue text", ((0, 9, 4),)),
        ("plain", ()),
    ]


def test_bright_256_and_true_colours_map_to_the_sixteen() -> None:
    lines = _decode(
        _ESC + b"[91ma" + _ESC + b"[39mb",
        _ESC + b"[38;5;196mc" + _ESC + b"[0m",
        _ESC + b"[38;5;12md" + _ESC + b"[0m",
        _ESC + b"[38;2;0;205;0me" + _ESC + b"[0m",
        _ESC + b"[38;5;244mf" + _ESC + b"[0m",
    )

    assert [spans for _, spans in lines] == [
        ((0, 1, 9),), ((0, 1, 9),), ((0, 1, 12),), ((0, 1, 2),), ((0, 1, 8),),
    ]


def test_background_and_other_codes_change_no_text_colour() -> None:
    assert _decode(_ESC + b"[42;1;4mx" + _ESC + b"[48;5;20;22my") == [("xy", ())]


def test_control_characters_shift_colours_by_their_escapes() -> None:
    (text, spans), = _decode(b"\x01" + _ESC + b"[32mok")

    assert text == "\\x01ok"
    assert spans == ((4, 6, 2),)


def test_reset_returns_to_the_default_colour() -> None:
    colours = AnsiColours()
    colours.decode(_ESC + b"[31m")

    colours.reset()

    assert colours.decode(b"plain") == ("plain", ())


@pytest.mark.parametrize("digits", [4, 5_000, 60_000])
def test_a_garbled_code_of_many_digits_is_ignored_and_the_text_kept(digits: int) -> None:
    garbled = _ESC + b"[0;31mred " + _ESC + b"[" + b"9" * digits + b"mstill red"

    assert _decode(garbled) == [("red still red", ((0, 13, 1),))]


def test_leading_zeros_do_not_hide_a_code() -> None:
    assert _decode(_ESC + b"[00031mred") == [("red", ((0, 3, 1),))]


def test_a_huge_colour_number_chooses_no_colour() -> None:
    assert _decode(_ESC + b"[38;5;" + b"9" * 50 + b"mplain") == [("plain", ())]
