# SPDX-License-Identifier: GPL-3.0-or-later
"""The display the tests and the picture tools draw on when nothing is shown.

Qt's ``offscreen`` platform has one screen of 800 by 600 unless it is told
otherwise. A window is never larger than its screen can hold
(``ui/widgets/least_size.py``), so the tests and the tools describe a
common desktop display instead, in ``offscreen.json``: 2560 by 1440,
which is 1280 by 720 at a display scale of 2.

This module imports nothing from Qt, so it can be used before Qt is.
"""

from __future__ import annotations

import os
from pathlib import Path

_PLATFORM = "QT_QPA_PLATFORM"
_OFFSCREEN = "offscreen"
_SCREENS = Path(__file__).resolve().parent / "offscreen.json"


class OffscreenDisplayError(RuntimeError):
    """The test display cannot be named to Qt from where the process runs."""


def offscreen_platform(working_directory: Path | None = None) -> str:
    """The value of ``QT_QPA_PLATFORM`` for the off-screen test display.

    Qt splits that value at every colon, so the file is named by a path
    relative to ``working_directory`` (the current one when not given):
    a Windows path such as ``D:\\...`` would be cut at its drive.

    Raises:
        OffscreenDisplayError: the process runs on another drive than
            the test display's file, so no relative path reaches it.
    """
    base = working_directory if working_directory is not None else Path.cwd()
    try:
        relative = os.path.relpath(_SCREENS, base)
    except ValueError as exc:
        raise OffscreenDisplayError(
            f"run the GUI tests from the drive that holds {_SCREENS.parent}"
        ) from exc
    return f"{_OFFSCREEN}:configfile={Path(relative).as_posix()}"


def use_offscreen_display() -> None:
    """Draw off screen on the test display, unless another platform was chosen.

    Must be called before Qt is imported, and before the working directory
    changes. A developer who sets another platform, to watch the widgets,
    keeps it.
    """
    if os.environ.get(_PLATFORM, _OFFSCREEN) == _OFFSCREEN:
        os.environ[_PLATFORM] = offscreen_platform()
