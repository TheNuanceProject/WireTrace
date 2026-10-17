# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for app.file_names: how logs and exports are named."""

from __future__ import annotations

import os
from datetime import datetime

import pytest

from app.constants import FILENAME_DEFAULT_NAME, FILENAME_MAX_LENGTH, FILENAME_NO_PORT
from app.file_names import output_base_name, output_base_path, port_label, sanitize_file_name

_MOMENT = datetime(2026, 4, 18, 19, 7, 39)


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("Motor run", "Motor_run"),
        ("  Alpha-Beta  ", "Alpha-Beta"),
        ('a<b>c:d"e/f\\g|h?i*j', "abcdefghij"),
        ("tab\there", "tabhere"),
        ("trailing dot.", "trailing_dot"),
        ("Zähler Ω", "Zähler_Ω"),
        ("", FILENAME_DEFAULT_NAME),
        ("   ", FILENAME_DEFAULT_NAME),
        ("???", FILENAME_DEFAULT_NAME),
        ("...", FILENAME_DEFAULT_NAME),
    ],
)
def test_name_is_made_safe_for_every_file_system(typed: str, expected: str) -> None:
    assert sanitize_file_name(typed) == expected


def test_long_name_is_cut_to_the_limit() -> None:
    assert sanitize_file_name("x" * (FILENAME_MAX_LENGTH + 40)) == "x" * FILENAME_MAX_LENGTH


@pytest.mark.parametrize(
    ("port", "label"),
    [
        ("COM7", "COM7"),
        ("/dev/ttyUSB0", "_dev_ttyUSB0"),
        ("/dev/cu.usbserial-1420", "_dev_cu.usbserial-1420"),
        ("\\\\.\\COM12", "__._COM12"),
        ("", FILENAME_NO_PORT),
    ],
)
def test_port_becomes_part_of_a_file_name(port: str, label: str) -> None:
    assert port_label(port) == label


def test_base_name_is_name_port_and_time() -> None:
    assert output_base_name("Motor run", "COM7", _MOMENT) == "Motor_run_COM7_2026-04-18_19-07-39"


def test_base_path_joins_the_folder(tmp_path) -> None:
    path = output_base_path(str(tmp_path), "", "/dev/ttyACM0", _MOMENT)

    assert path == os.path.join(
        str(tmp_path), f"{FILENAME_DEFAULT_NAME}__dev_ttyACM0_2026-04-18_19-07-39",
    )
