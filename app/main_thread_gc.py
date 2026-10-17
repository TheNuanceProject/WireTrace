# SPDX-License-Identifier: GPL-3.0-or-later
"""Run Python's garbage collector on the main thread only.

Python runs its collector in whichever thread happens to be allocating.
When that is a worker thread, the collector can destroy interface objects
there, although they belong to the main thread. Qt does not allow that:
it can stop the program dead, with the worker holding a Qt lock while it
waits for the interpreter and the main thread holding the interpreter
while it waits for the lock.

So automatic collection is switched off, and a timer on the main thread
collects whenever the collector's own thresholds have been passed.
Objects that are not part of a reference cycle are unaffected: they are
still freed the moment the last reference goes.
"""

from __future__ import annotations

import gc

from PySide6.QtCore import QObject, QTimer

from app.constants import GC_CHECK_INTERVAL_MS

_GENERATIONS = 3


class MainThreadCollector(QObject):
    """Collects garbage from the thread it lives in, and from no other."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._was_enabled = gc.isenabled()
        gc.disable()
        self._timer = QTimer(self)
        self._timer.setInterval(GC_CHECK_INTERVAL_MS)
        self._timer.timeout.connect(self.collect_if_due)
        self._timer.start()

    @property
    def active(self) -> bool:
        return self._timer.isActive()

    def collect_if_due(self) -> None:
        """Collect each generation whose threshold has been passed.

        A generation is looked at only when the one before it was due,
        which is the order the automatic collector works in.
        """
        counts = gc.get_count()
        thresholds = gc.get_threshold()
        for generation in range(_GENERATIONS):
            if counts[generation] <= thresholds[generation]:
                break
            gc.collect(generation)

    def stop(self) -> None:
        """Hand collection back to the interpreter."""
        if not self._timer.isActive():
            return
        self._timer.stop()
        if self._was_enabled:
            gc.enable()
