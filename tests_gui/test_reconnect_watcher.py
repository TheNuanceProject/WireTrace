# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for ui.reconnect_watcher: its thread and its pauses."""

from __future__ import annotations

import threading
import time

from app.constants import (
    MS_PER_SECOND,
    RECONNECT_RETRY_FIRST_MS,
    RECONNECT_RETRY_MAX_MS,
    RECONNECT_STOP_WAIT_MS,
)
from core.port_identity import PortIdentity
from tests_gui.support import wait_until
from ui.device_claims import DeviceClaims
from ui.reconnect_watcher import ReconnectWatcher

_BOARD = PortIdentity("COM7", 0x10C4, 0xEA60, "0001", "1-1")


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_closing_while_the_port_list_hangs_waits_a_bounded_time_and_reports_nothing(qapp):
    release = threading.Event()
    reads = [0]

    def hanging_read() -> list[PortIdentity]:
        reads[0] += 1
        if reads[0] > 1:
            release.wait()
        return [_BOARD]

    watcher = ReconnectWatcher(object(), DeviceClaims(), hanging_read)
    heard: list[object] = []
    for signal in (watcher.recorded, watcher.found, watcher.asked, watcher.withdrawn):
        signal.connect(heard.append)
    watcher.record("COM7")
    wait_until(qapp, lambda: heard == ["COM7"])
    watcher.watch(by_port_name=True)
    wait_until(qapp, lambda: reads[0] > 1)

    started = time.monotonic()
    watcher.shutdown()
    waited = time.monotonic() - started
    release.set()
    end = time.monotonic() + 0.3
    while time.monotonic() < end:
        qapp.processEvents()

    assert waited < RECONNECT_STOP_WAIT_MS / MS_PER_SECOND + 0.5
    assert heard == ["COM7"]


def test_each_failed_open_doubles_the_pause_up_to_its_limit(qapp):
    clock = _Clock()
    reads = [0]

    def read() -> list[PortIdentity]:
        reads[0] += 1
        return [_BOARD]

    watcher = ReconnectWatcher(object(), DeviceClaims(), read, clock=clock)
    found: list[object] = []
    watcher.found.connect(found.append)
    watcher.record("COM7")
    wait_until(qapp, lambda: watcher.board is not None)
    watcher.watch(by_port_name=True)
    wait_until(qapp, lambda: len(found) == 1)

    def wake() -> None:
        with watcher._condition:
            watcher._condition.notify()

    pauses: list[int] = []
    for _ in range(6):
        watcher.retry_later()
        pauses.append(watcher._retry_ms)
        clock.now += watcher._retry_ms / MS_PER_SECOND / 2
        wake()
        time.sleep(0.05)
        qapp.processEvents()
        assert len(found) == len(pauses)  # Still within the pause.
        clock.now += watcher._retry_ms / MS_PER_SECOND / 2
        wake()
        wait_until(qapp, lambda: len(found) == len(pauses) + 1)

    assert pauses == [
        min(RECONNECT_RETRY_FIRST_MS * 2 ** step, RECONNECT_RETRY_MAX_MS) for step in range(6)
    ]
    assert pauses[-1] == RECONNECT_RETRY_MAX_MS
    watcher.shutdown()
