# SPDX-License-Identifier: GPL-3.0-or-later
"""Garbage is collected on the main thread and nowhere else."""

from __future__ import annotations

import gc
import threading

import pytest

from app.main_thread_gc import MainThreadCollector


class _Node:
    """Something that can refer to itself, so only the collector frees it."""

    def __init__(self, collected_in: list[int]) -> None:
        self.me = self
        self._collected_in = collected_in

    def __del__(self) -> None:
        self._collected_in.append(threading.get_ident())


@pytest.fixture
def collector(qapp):
    """A collector of the test's own; the session's one is set aside."""
    was_enabled = gc.isenabled()
    gc.enable()
    own = MainThreadCollector()
    yield own
    own.stop()
    own.deleteLater()
    if not was_enabled:
        gc.disable()


def test_automatic_collection_is_off_while_the_collector_runs(collector):
    assert collector.active
    assert not gc.isenabled()

    collector.stop()

    assert not collector.active
    assert gc.isenabled()


def test_a_worker_thread_never_collects(collector):
    collected_in: list[int] = []
    threshold = gc.get_threshold()[0]

    def churn() -> None:
        for _ in range(threshold * 4):
            _Node(collected_in)

    worker = threading.Thread(target=churn)
    worker.start()
    worker.join()

    assert collected_in == []

    collector.collect_if_due()

    assert len(collected_in) == threshold * 4
    assert set(collected_in) == {threading.get_ident()}


def test_nothing_is_collected_before_a_threshold_is_passed(collector):
    gc.collect()
    collected_in: list[int] = []
    _Node(collected_in)

    collector.collect_if_due()

    assert collected_in == []
    gc.collect()
    assert len(collected_in) == 1
