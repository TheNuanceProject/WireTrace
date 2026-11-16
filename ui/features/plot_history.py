# SPDX-License-Identifier: GPL-3.0-or-later
"""Older values of one tab's plot, read from the session recording.

Joins ``core.plot_history.HistoryReader``, which reads on a thread of
its own, to the plot on the GUI thread. What the reader hands over
arrives here through a queued signal; samples of the engine's current
generation go into an overview per value, and anything older is
dropped.

The reader follows the recording only while the plot is shown, and
starts again from the start of the session whenever the engine's
generation changes (see ``core.plot_engine``).
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal, Slot

from core.plot_engine import PlotEngine
from core.plot_history import (
    DetailRequest,
    DetailResult,
    HistoryBatch,
    HistoryReader,
    HistorySource,
    Recording,
)
from core.plot_overview import Overview

logger = logging.getLogger(__name__)


class PlotHistory(QObject):
    """The older values of one plot.

    Signals:
        changed():  Older values arrived, or what is known about them changed.
    """

    changed = Signal()
    _arrived = Signal(object)

    def __init__(self, engine: PlotEngine, recording: Recording, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._engine = engine
        self._reader = HistoryReader(recording, self._arrived.emit)
        self._arrived.connect(self._take)
        self._generation: int | None = None
        self._overviews: dict[str, Overview] = {}
        self._detail: DetailResult | None = None
        self._asked: DetailRequest | None = None
        self.start_lost = False
        self.stalled = False
        # The time of the latest device line read, or None before any.
        self.reached: float | None = None

    # ── Public API ───────────────────────────────────────────────────────

    def sync(self) -> None:
        """Follow the engine's current generation, starting again if it changed."""
        generation = self._engine.generation
        if generation == self._generation:
            return
        origin = self._engine.origin
        parser = self._engine.history_parser() if origin is not None else None
        self._clear()
        if parser is None:
            self._generation = None
            self._reader.follow(None)
            return
        self._generation = generation
        columns = tuple(self._engine.columns)
        self._overviews = {name: Overview() for name in columns}
        self._reader.follow(HistorySource(generation, origin, columns, parser))

    def set_reading(self, reading: bool) -> None:
        """Read the recording only while the plot can be seen."""
        self._reader.set_reading(reading)

    def reset(self) -> None:
        """Forget everything; a new session has begun."""
        self._clear()
        self._generation = None
        self._reader.follow(None)

    def overview(self, name: str) -> Overview | None:
        """The overview of ``name``, or None while none is being read."""
        return self._overviews.get(name)

    @property
    def detail(self) -> DetailResult | None:
        """The answer to the latest detail request, once it has come."""
        return self._detail

    @property
    def reading_detail(self) -> bool:
        """True while a detail request has not been answered."""
        return self._asked is not None and (
            self._detail is None or self._detail.request is not self._asked
        )

    def request_detail(self, request: DetailRequest) -> None:
        """Read ``request`` in full; ``changed`` follows the answer."""
        if self._generation is None or request.generation != self._generation:
            return
        if self._asked == request:
            return
        self._asked = request
        self._reader.request_detail(request)

    def shutdown(self) -> None:
        """Stop the reader and wait for its thread."""
        self._reader.stop()

    # ── Internal ─────────────────────────────────────────────────────────

    def _clear(self) -> None:
        self._overviews = {}
        self._detail = None
        self._asked = None
        self.start_lost = False
        self.stalled = False
        self.reached = None

    @Slot(object)
    def _take(self, item: object) -> None:
        if isinstance(item, HistoryBatch):
            if item.generation != self._generation:
                return
            for name, (x, y) in item.samples.items():
                overview = self._overviews.get(name)
                if overview is not None:
                    overview.add(x, y)
            self.start_lost = self.start_lost or item.start_lost
            self.stalled = self.stalled or item.stalled
            if item.reached is not None and (self.reached is None or item.reached > self.reached):
                self.reached = item.reached
        elif isinstance(item, DetailResult):
            if item.request is not self._asked:
                return
            self._detail = item
        self.changed.emit()

