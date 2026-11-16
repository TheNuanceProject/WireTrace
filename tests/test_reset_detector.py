# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for core.reset_detector: the lines that show a board restarted."""

from __future__ import annotations

import pytest

from core.reset_detector import ZEPHYR_REASON, ResetDetector


def _restarts(lines: list[str]) -> list[str]:
    detector = ResetDetector()
    return [reason for line in lines if (reason := detector.check(line)) is not None]


@pytest.mark.parametrize(("line", "reason"), [
    ("rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)", "POWERON_RESET"),
    ("rst:0x10 (RTCWDT_RTC_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)", "RTCWDT_RTC_RESET"),
    ("rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)", "POWERON"),
    ("rst:0xc (SW_CPU_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)", "SW_CPU_RESET"),
])
def test_an_esp32_reset_line_gives_its_reason(line: str, reason: str) -> None:
    assert _restarts([line]) == [reason]


@pytest.mark.parametrize("line", [
    "*** Booting Zephyr OS build v3.7.0 ***",
    "*** Booting Zephyr OS version 3.5.0 ***",
    "*** Using Zephyr OS v4.1.99-1612683d4010 ***",
])
def test_a_zephyr_banner_is_a_restart(line: str) -> None:
    assert _restarts([line]) == [ZEPHYR_REASON]


@pytest.mark.parametrize("line", [
    "I (305) boot: ESP-IDF v5.1 2nd stage bootloader",
    "temperature first: rst:0x99 is not at the start",
    "the docs say *** Booting Zephyr OS on boot",
    "*** Booting nRF Connect SDK v2.6.0 ***",
    "",
])
def test_other_lines_are_not_restarts(line: str) -> None:
    assert _restarts([line]) == []


def test_each_restart_of_a_board_without_a_bootloader_is_counted() -> None:
    banner = "*** Booting Zephyr OS build v3.7.0 ***"

    assert _restarts([banner, "app running", banner, "app running"]) == [ZEPHYR_REASON] * 2


def test_a_zephyr_image_started_by_the_esp32_rom_is_one_restart() -> None:
    lines = [
        "rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)",
        "load:0x3fff0030,len:1184",
        "*** Booting Zephyr OS build v3.7.0 ***",
        "*** Booting Zephyr OS build v3.7.0 ***",
    ]

    assert _restarts(lines) == ["POWERON_RESET", ZEPHYR_REASON]


def test_an_application_started_by_mcuboot_is_the_same_restart() -> None:
    lines = [
        "*** Booting MCUboot v2.1.0-dev ***",
        "*** Using nRF Connect SDK v3.1.0 ***",
        "*** Using Zephyr OS v4.1.99 ***",
        "I: Starting bootloader",
        "I: Jumping to the first image slot",
        "*** Booting My Application v0.1.6 ***",
        "*** Using nRF Connect SDK v3.1.0 ***",
        "*** Using Zephyr OS v4.1.99 ***",
    ]

    assert _restarts(lines) == [ZEPHYR_REASON]


def test_reset_forgets_a_restart_still_starting() -> None:
    detector = ResetDetector()
    detector.check("rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)")

    detector.reset()

    assert detector.check("*** Booting Zephyr OS build v3.7.0 ***") == ZEPHYR_REASON
