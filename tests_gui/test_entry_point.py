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


def test_an_uncaught_error_on_any_thread_is_logged_with_its_traceback() -> None:
    script = (
        "import sys, threading\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "import main\n"
        "main.setup_logging()\n"
        "thread = threading.Thread(target=lambda: 1 / 0, name='worker')\n"
        "thread.start(); thread.join()\n"
        "raise LookupError('on the main thread')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True,
        timeout=_TIMEOUT_SECONDS, check=False,
    )

    assert "[CRITICAL] main: Uncaught error on thread worker" in result.stderr
    assert "ZeroDivisionError" in result.stderr
    assert "[CRITICAL] main: Uncaught error\n" in result.stderr
    assert "LookupError: on the main thread" in result.stderr
