# SPDX-License-Identifier: GPL-3.0-or-later
"""One piece of blocking work, done off the main thread.

The update check and the download wait on the network, so they run on a
thread of their own and report back by signal. The thread is a daemon:
if the application closes while a request is still waiting for a server,
the request is abandoned and nothing holds the exit up.

How the work ended is handed to the thread the task lives on, and the
task counts as running until it has been announced there. So whoever
asks whether it is still running never sees it finished before its
outcome has been handled.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)


class Cancelled(Exception):
    """Raised by the work itself when it saw that it was cancelled."""


class Task(QObject):
    """Runs ``work`` on a thread and announces how it ended.

    ``work`` receives the task, so that it can ask ``is_cancelled`` and
    report ``progress``. It returns its result, or raises: ``Cancelled``
    if it stopped on request, an exception of ``errors`` if it failed in
    a way the caller expects.

    Signals:
        succeeded(object):   The result of the work.
        failed(str):         What went wrong, in words.
        cancelled():         The work stopped because it was asked to.
        progress(int, int):  How far it is: done, of how many.
    """

    succeeded = Signal(object)
    failed = Signal(str)
    cancelled = Signal()
    progress = Signal(int, int)
    # How the work ended, from the worker thread to the task's own thread:
    # the public signal to emit there, and its arguments.
    _ended = Signal(object, tuple)

    def __init__(
        self,
        work: Callable[[Task], object],
        errors: tuple[type[Exception], ...],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._work = work
        self._errors = errors
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._announced = False
        self._ended.connect(self._announce_here)

    @property
    def running(self) -> bool:
        """Started, and how it ended not yet announced on the task's thread."""
        return self._thread is not None and not self._announced

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="wiretrace-update", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        """Ask the work to stop. It does so at its next opportunity."""
        self._cancel.set()

    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    def _run(self) -> None:
        try:
            result = self._work(self)
        except Cancelled:
            self._announce(self.cancelled)
        except self._errors as exc:
            self._announce(self.failed, str(exc))
        except Exception:
            logger.exception("An update task failed unexpectedly")
            self._announce(self.failed, "an unexpected error occurred")
        else:
            self._announce(self.succeeded, result)

    def _announce(self, signal: Signal, *arguments: object) -> None:
        """Hand how the work ended to the task's own thread (on the worker thread)."""
        # The owner may have been closed and deleted while the work ran.
        try:
            self._ended.emit(signal, arguments)
        except RuntimeError:
            logger.debug("An update task ended after its owner was gone")

    def _announce_here(self, signal: Signal, arguments: tuple) -> None:
        """Announce how the work ended (on the task's own thread)."""
        self._announced = True
        signal.emit(*arguments)
