# SPDX-License-Identifier: GPL-3.0-or-later
"""Runs the application's own ``--smoke-test`` mode as a subprocess.

This is the same check the build pipeline runs against a frozen binary,
here run against the source tree with the real dependencies installed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_TIMEOUT_SECONDS = 120


def test_smoke_test_mode_exits_cleanly() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--smoke-test"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Smoke test passed: every check succeeded" in result.stderr
    assert "FAIL" not in result.stderr
    assert "Installed as: source" in result.stderr
