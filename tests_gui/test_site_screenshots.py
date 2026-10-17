# SPDX-License-Identifier: GPL-3.0-or-later
"""The website's pictures can always be made again from the application.

The tool replaces parts of the application for its imagined devices, so
it runs in a process of its own, as it does when it is used. Importing it
here only reads its window size: it sets nothing until it is run.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtGui import QImage

from tests_gui.offscreen import offscreen_platform
from tools.make_site_screenshots import WINDOW_SIZE

ROOT = Path(__file__).resolve().parent.parent
SCENES = ("first-screen", "session", "plot", "connection-lost")
THEMES = ("light", "dark")
TIMEOUT_S = 240


def test_every_picture_is_made_at_the_window_size(tmp_path) -> None:
    environment = {**os.environ, "QT_QPA_PLATFORM": offscreen_platform(ROOT)}
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "make_site_screenshots.py"), str(tmp_path)],
        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=TIMEOUT_S, check=False,
    )
    assert result.returncode == 0, result.stderr

    made = sorted(path.name for path in tmp_path.iterdir())
    assert made == sorted(f"wiretrace-{scene}-{theme}.png" for scene in SCENES for theme in THEMES)
    for name in made:
        image = QImage(str(tmp_path / name))
        assert (image.width(), image.height()) == WINDOW_SIZE, name


def test_importing_the_tool_sets_no_scale() -> None:
    environment = {key: value for key, value in os.environ.items() if key != "QT_SCALE_FACTOR"}
    program = "import os, tools.make_site_screenshots; assert 'QT_SCALE_FACTOR' not in os.environ"
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=TIMEOUT_S, check=False,
    )
    assert result.returncode == 0, result.stderr
