# SPDX-License-Identifier: GPL-3.0-or-later
"""Throughput gate for the data pipeline.

The README states a line rate the application sustains. This test holds
the pipeline to it on every platform the suite runs on: a large burst
goes from the port to the log file through the real reader and writer
threads, every line must arrive, in order, and the whole run must be at
least as fast as the stated rate.

The burst is measured as a whole rather than paced in real time, so a
pause of the test machine slows the result a little instead of failing
it, while a pipeline that has become several times slower still fails.
"""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtWidgets import QApplication

from core.device_pipeline import DevicePipeline
from core.log_writer import LogRequest
from tests_gui.support import LoopbackPort, line_text, log_body, wait_until

_STATED_LINES_PER_SECOND = 10_000
_LINES = 200_000
_LINES_PER_FEED = 2_000
_DRAIN_TIMEOUT_SECONDS = 120.0


def test_burst_reaches_the_log_complete_at_the_stated_rate(
    qapp: QApplication, tmp_path: Path,
) -> None:
    expected = [
        f"I ({number}) sensor: temp=21.5 rpm=-40 status=ok" for number in range(_LINES)
    ]
    feeds = [
        "".join(f"{text}\n" for text in expected[start:start + _LINES_PER_FEED]).encode()
        for start in range(0, _LINES, _LINES_PER_FEED)
    ]

    port = LoopbackPort()
    pipeline = DevicePipeline(
        spool_root=str(tmp_path / "spool"),
        display_capacity=_LINES_PER_FEED,
        # The whole burst may wait for the disk: this test measures
        # speed. What happens when the buffer overflows has tests of
        # its own.
        max_pending_lines=_LINES,
        flush_interval_ms=1_000,
        transport_factory=port.factory,
    )
    connected: list[tuple[str, int]] = []
    disconnected: list[tuple[str, bool, object]] = []
    pipeline.connected.connect(lambda *args: connected.append(args))
    pipeline.disconnected.connect(lambda *args: disconnected.append(args))
    try:
        pipeline.open("PORT0", 115200)
        wait_until(qapp, lambda: bool(connected))
        text_path = str(tmp_path / "burst.txt")
        pipeline.start_log(LogRequest(text_path, None, "burst", "PORT0", 115200, "", 0))

        started = time.perf_counter()
        for feed in feeds:
            port.feed(feed)
        pipeline.close()
        wait_until(qapp, lambda: bool(disconnected), timeout=_DRAIN_TIMEOUT_SECONDS)
        elapsed = time.perf_counter() - started
    finally:
        pipeline.shutdown()

    summary = disconnected[0][2]
    assert summary.lines_lost == 0
    assert summary.lines_written == _LINES
    assert [line_text(line) for line in log_body(text_path)] == expected
    assert _LINES / elapsed >= _STATED_LINES_PER_SECOND
