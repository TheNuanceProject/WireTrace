# SPDX-License-Identifier: GPL-3.0-or-later
"""A task counts as running until how it ended is announced on its own thread."""

from __future__ import annotations

from PySide6.QtCore import QObject

from tests_gui.support import wait_until
from updater.background import Cancelled, Task


class _Owner(QObject):
    """Records each outcome together with whether the task still ran."""

    def __init__(self) -> None:
        super().__init__()
        self.outcomes: list[tuple[str, object, bool]] = []
        self.task: Task | None = None

    def watch(self, task: Task) -> Task:
        self.task = task
        task.succeeded.connect(lambda result: self._note("succeeded", result))
        task.failed.connect(lambda message: self._note("failed", message))
        task.cancelled.connect(lambda: self._note("cancelled", None))
        return task

    def _note(self, kind: str, value: object) -> None:
        assert self.task is not None
        self.outcomes.append((kind, value, self.task.running))


def _finished_thread(task: Task) -> None:
    """Wait for the worker thread to end without handling any event."""
    assert task._thread is not None
    task._thread.join()


def test_running_until_the_outcome_is_announced(qapp):
    owner = _Owner()
    task = owner.watch(Task(lambda _task: 7, (OSError,), owner))

    assert not task.running
    task.start()
    _finished_thread(task)

    # The worker has ended, but its outcome has not reached this thread.
    assert task.running
    assert owner.outcomes == []

    wait_until(qapp, lambda: not task.running)

    assert owner.outcomes == [("succeeded", 7, False)]


def test_a_failure_is_announced_once(qapp):
    owner = _Owner()

    def work(_task: Task) -> None:
        raise OSError("offline")

    task = owner.watch(Task(work, (OSError,), owner))
    task.start()
    _finished_thread(task)
    assert task.running

    wait_until(qapp, lambda: not task.running)
    qapp.processEvents()

    assert owner.outcomes == [("failed", "offline", False)]


def test_a_cancellation_is_announced_once(qapp):
    owner = _Owner()

    def work(task: Task) -> None:
        task.cancel()
        if task.is_cancelled():
            raise Cancelled

    task = owner.watch(Task(work, (OSError,), owner))
    task.start()
    _finished_thread(task)
    assert task.running

    wait_until(qapp, lambda: not task.running)
    qapp.processEvents()

    assert owner.outcomes == [("cancelled", None, False)]
