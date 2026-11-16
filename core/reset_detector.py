# SPDX-License-Identifier: GPL-3.0-or-later
"""Recognises the lines a board prints when it starts again.

Built in and fixed, so it can run on the reader thread for every line
at a small, bounded cost: each check first looks for a short piece of
fixed text, and only a line holding it is matched further.

What is recognised:
  ESP32 (every chip of the family): the boot ROM prints, once for each
  reset of the chip, a line such as ``rst:0x1 (POWERON_RESET),boot:0x13
  (SPI_FAST_FLASH_BOOT)``. The name in brackets is the reason.

  Zephyr: each image prints a boot banner as it starts,
  ``*** Booting Zephyr OS build ... ***``, or with the nRF Connect SDK,
  ``*** Using Zephyr OS ... ***`` after a banner of its own.

One restart can show more than one of these: the ESP32 ROM starts a
Zephyr image, or the MCUboot bootloader, itself built on Zephyr, prints
a banner and then starts the application, which prints another. So a
Zephyr banner that follows an ESP32 reset line, or MCUboot's
``Jumping to the first image slot``, with no banner between, belongs to
that restart and is not counted again. A bootloader built on Zephyr
whose own log is switched off gives no such line, and its restarts are
then counted twice; the user guide says so.

This module does not touch the GUI, the disk, the serial port or any other I/O.
"""

from __future__ import annotations

import re

#: What a Zephyr boot banner is given as a reason.
ZEPHYR_REASON = "Zephyr boot banner"

_ESP32_MARK = "rst:0x"
_ESP32_RESET = re.compile(r"^\s*rst:0x[0-9a-fA-F]+\s*\(([A-Za-z0-9_]+)\)")
_ZEPHYR_MARK = "Zephyr OS"
_ZEPHYR_BANNER = re.compile(r"^\s*\*\*\* (?:Booting|Using) Zephyr OS\b")
_MCUBOOT_HAND_OFF = "Jumping to the first image slot"


class ResetDetector:
    """Says which lines of one session mark a restart of the board."""

    def __init__(self) -> None:
        # An ESP32 reset or a bootloader's hand-off was seen, and the
        # image it starts has not printed a banner yet.
        self._image_starting = False

    def reset(self) -> None:
        """Forget the previous session."""
        self._image_starting = False

    def check(self, line: str) -> str | None:
        """The reason ``line`` marks a new restart, or None if it does not."""
        if _ESP32_MARK in line:
            match = _ESP32_RESET.match(line)
            if match is not None:
                self._image_starting = True
                return match.group(1)
        if _ZEPHYR_MARK in line and _ZEPHYR_BANNER.match(line):
            if self._image_starting:
                self._image_starting = False
                return None
            return ZEPHYR_REASON
        if _MCUBOOT_HAND_OFF in line:
            self._image_starting = True
        return None
