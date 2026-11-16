# SPDX-License-Identifier: GPL-3.0-or-later
"""Throughput gate for the data pipeline.

The README states a line rate the application sustains. This test holds
the pipeline to it on every platform the suite runs on: a large burst
goes from the port to the log file through the real reader and writer
threads, every line must arrive, in order, and the whole run must be at
least as fast as the stated rate. It is measured three times: the pipeline
alone, the pipeline with the board lost and back in the middle of the
burst, and a whole tab with every feature switched on and the live plot
open, as a user runs it. The tab is fed at twice the stated rate, in steady steps
as a port delivers, and must keep up, log every line, and keep the
window answering throughout.

The burst is measured as a whole rather than paced in real time, so a
pause of the test machine slows the result a little instead of failing
it, while a pipeline that has become several times slower still fails.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog

from app.config import ConfigManager
from app.constants import ThemeID
from app.modules import MODULES, ModuleSettings
from core.device_pipeline import DevicePipeline
from core.highlight_rules import HighlightRule, HighlightStore
from core.log_writer import LogRequest
from core.serial_settings import LineSettings
from tests_gui.support import LoopbackPort, line_text, log_body, wait_until
from ui.device_tab import DeviceTab
from ui.dialogs.new_log_dialog import NewLogDialog
from ui.themes.theme_manager import ThemeManager
from ui.widgets.tab_toolbar import TabToolbar

_STATED_LINES_PER_SECOND = 10_000
_LINES = 200_000
_LINES_PER_FEED = 2_000
_DRAIN_TIMEOUT_SECONDS = 120.0
_TAB_LINES = 60_000
_TAB_FEED_INTERVAL_MS = 100
_TAB_LINES_PER_FEED = 2 * _STATED_LINES_PER_SECOND * _TAB_FEED_INTERVAL_MS // 1_000
_HEARTBEAT_MS = 10
# The longest the window may go without answering while the burst runs.
_LONGEST_UNANSWERED_SECONDS = 0.5


def _burst(lines: int, per_feed: int) -> tuple[list[str], list[bytes]]:
    expected = [
        f"I ({number}) sensor: temp=21.5 rpm=-40 status=ok" for number in range(lines)
    ]
    feeds = [
        "".join(f"{text}\n" for text in expected[start:start + per_feed]).encode()
        for start in range(0, lines, per_feed)
    ]
    return expected, feeds


def test_burst_reaches_the_log_complete_at_the_stated_rate(
    qapp: QApplication, tmp_path: Path,
) -> None:
    expected, feeds = _burst(_LINES, _LINES_PER_FEED)

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


def test_a_board_lost_and_back_mid_burst_loses_nothing_outside_the_gap(
    qapp: QApplication, tmp_path: Path,
) -> None:
    expected, feeds = _burst(_LINES, _LINES_PER_FEED)
    before = len(feeds) // 2
    lines_before = before * _LINES_PER_FEED

    port = LoopbackPort()
    pipeline = DevicePipeline(
        spool_root=str(tmp_path / "spool"),
        display_capacity=_LINES_PER_FEED,
        max_pending_lines=_LINES,
        flush_interval_ms=1_000,
        transport_factory=port.factory,
    )
    pipeline.set_reconnect(True)
    connected: list[tuple[str, int]] = []
    lost: list[tuple[str, str]] = []
    back: list[tuple[str, int, float]] = []
    disconnected: list[tuple[str, bool, object]] = []
    pipeline.connected.connect(lambda *args: connected.append(args))
    pipeline.connection_lost.connect(lambda *args: lost.append(args))
    pipeline.connection_back.connect(lambda *args: back.append(args))
    pipeline.disconnected.connect(lambda *args: disconnected.append(args))
    try:
        pipeline.open("PORT0", 115200)
        wait_until(qapp, lambda: bool(connected))
        text_path = str(tmp_path / "burst.txt")
        pipeline.start_log(LogRequest(text_path, None, "burst", "PORT0", 115200, "", 0))

        started = time.perf_counter()
        for feed in feeds[:before]:
            port.feed(feed)
        port.transport.drop("the device was removed")
        wait_until(qapp, lambda: bool(lost), timeout=_DRAIN_TIMEOUT_SECONDS)
        pipeline.reconnect("PORT0", 115200, LineSettings(), "identified by port name", lambda: True)
        wait_until(qapp, lambda: bool(back))
        for feed in feeds[before:]:
            port.feed(feed)
        pipeline.close()
        wait_until(qapp, lambda: bool(disconnected), timeout=_DRAIN_TIMEOUT_SECONDS)
        elapsed = time.perf_counter() - started
    finally:
        pipeline.shutdown()

    texts = [line_text(line) for line in log_body(text_path)]
    assert texts[:lines_before] == expected[:lines_before]
    assert texts[lines_before].startswith("[WireTrace] Connection to PORT0 lost: ")
    assert texts[lines_before + 1].startswith("[WireTrace] Reconnected on PORT0 after ")
    assert texts[lines_before + 2:] == expected[lines_before:]
    assert disconnected[0][2].lines_lost == 0
    assert _LINES / elapsed >= _STATED_LINES_PER_SECOND


def test_a_tab_with_every_feature_on_logs_the_burst_complete_and_keeps_answering(
    qapp: QApplication,
    config: ConfigManager,
    theme_manager: ThemeManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected, feeds = _burst(_TAB_LINES, _TAB_LINES_PER_FEED)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    monkeypatch.setattr(NewLogDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(NewLogDialog, "log_directory", property(lambda self: str(output_dir)))

    theme_manager.apply_theme(ThemeID.LIGHT)
    modules = ModuleSettings(config)
    for spec in MODULES:
        modules.set_enabled(spec, True)
    HighlightStore(config).set_rules((HighlightRule("status=ok", 3), HighlightRule("rpm", 5)))
    config.alert_silence = True
    port = LoopbackPort()
    tab = DeviceTab(
        theme_manager, config, transport_factory=port.factory, read_ports=port.identities,
    )
    tab.show()
    toolbar = tab.findChild(TabToolbar)
    gaps: list[float] = []
    last_beat = [time.perf_counter()]

    def beat() -> None:
        now = time.perf_counter()
        gaps.append(now - last_beat[0])
        last_beat[0] = now

    heartbeat = QTimer()
    heartbeat.setInterval(_HEARTBEAT_MS)
    heartbeat.timeout.connect(beat)
    waiting = list(reversed(feeds))
    feeder = QTimer()
    feeder.setInterval(_TAB_FEED_INTERVAL_MS)
    feeder.timeout.connect(lambda: port.feed(waiting.pop()) if waiting else feeder.stop())
    try:
        toolbar.connect_requested.emit("PORT0", 115200)
        wait_until(qapp, lambda: tab.is_connected)
        tab.toggle_plot()
        toolbar.log_toggled.emit()
        assert tab.is_plot_visible() and tab.is_logging

        last_beat[0] = time.perf_counter()
        heartbeat.start()
        started = time.perf_counter()
        feeder.start()
        wait_until(
            qapp, lambda: tab.session.total_lines >= _TAB_LINES, timeout=_DRAIN_TIMEOUT_SECONDS,
        )
        elapsed = time.perf_counter() - started
        heartbeat.stop()
        toolbar.disconnect_requested.emit()
        wait_until(qapp, lambda: not tab.is_connected, timeout=_DRAIN_TIMEOUT_SECONDS)
        columns = sorted(tab.plot.engine.columns)
    finally:
        feeder.stop()
        heartbeat.stop()
        tab.ordered_shutdown()
        tab.close()
        tab.deleteLater()
        qapp.processEvents()

    logs = sorted(output_dir.glob("*.txt"))
    assert len(logs) == 1
    assert [line_text(line) for line in log_body(str(logs[0]))] == expected
    assert _TAB_LINES / elapsed >= _STATED_LINES_PER_SECOND
    assert columns == ["rpm", "temp"]
    assert max(gaps) < _LONGEST_UNANSWERED_SECONDS
