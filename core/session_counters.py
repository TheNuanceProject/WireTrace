# SPDX-License-Identifier: GPL-3.0-or-later
"""Exact figures of one session, counted where every line passes.

The reader thread counts each line as it hands it over, before anything
can skip it, so the figures are exact however far the screen or a
module falls behind: lines received, lines per level, device lines not
recorded because the disk could not keep up, restarts of the board, and
the times the connection was lost and came back.
The end-of-test report only formats them.

``figures`` may be called from any thread; it returns a copy taken
under a lock.

This module does not touch the GUI, the disk, the serial port or any other I/O.
"""

from __future__ import annotations

import threading
from collections import Counter, deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from app.constants import SESSION_DISCONNECTS_KEPT, SESSION_RESTARTS_KEPT
from core.records import LineRecord, RecordOrigin


@dataclass(frozen=True)
class Restart:
    """A restart of the board: when its line arrived, and why, as the board said."""

    at: datetime
    reason: str


@dataclass(frozen=True)
class Disconnect:
    """The connection was lost at ``lost`` and came back at ``back`` (None while it has not)."""

    lost: datetime
    back: datetime | None


@dataclass(frozen=True)
class SessionFigures:
    """The figures of a session, at one moment.

    Attributes:
        lines: Device lines received.
        by_level: Device lines received, by level (``ERROR``, ``WARNING`` ...).
        not_recorded: Device lines that could not be recorded because
            the disk could not keep up. They were shown, but are in no
            log and no export.
        restart_count: Restarts of the board recognised.
        restarts: The latest of them, at most ``SESSION_RESTARTS_KEPT``.
        disconnect_count: Times the connection was lost within the
            session and the board was waited for.
        seconds_away: Time the board was away, over the losses it came
            back from.
        disconnects: The latest of them, at most ``SESSION_DISCONNECTS_KEPT``.
        ended: When the connection ended, None while it lasts.
        ended_unexpectedly: Whether the end was unexpected (the board was
            unplugged or the link lost); None while it lasts.
    """

    port: str
    baud: int
    started: datetime
    lines: int
    by_level: dict[str, int]
    not_recorded: int
    restart_count: int
    restarts: tuple[Restart, ...]
    disconnect_count: int
    seconds_away: float
    disconnects: tuple[Disconnect, ...]
    ended: datetime | None
    ended_unexpectedly: bool | None


class SessionCounters:
    """Counts the figures of the current session. Fed by the reader thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started: tuple[str, int, datetime] | None = None
        self._lines = 0
        self._by_level: Counter[str] = Counter()
        self._not_recorded = 0
        self._restart_count = 0
        self._restarts: deque[Restart] = deque(maxlen=SESSION_RESTARTS_KEPT)
        self._disconnects: deque[Disconnect] = deque(maxlen=SESSION_DISCONNECTS_KEPT)
        self._disconnect_count = 0
        self._seconds_away = 0.0
        self._ended: tuple[datetime, bool] | None = None

    def start(self, port: str, baud: int, at: datetime) -> None:
        """A new session began; the previous one's figures are forgotten."""
        with self._lock:
            self._started = (port, baud, at)
            self._lines = 0
            self._by_level.clear()
            self._not_recorded = 0
            self._restart_count = 0
            self._restarts.clear()
            self._disconnects.clear()
            self._disconnect_count = 0
            self._seconds_away = 0.0
            self._ended = None

    def received(self, records: Iterable[LineRecord]) -> int:
        """Count the device lines among ``records``; returns how many there were."""
        levels = [record.tag for record in records if record.origin is RecordOrigin.DEVICE]
        if levels:
            with self._lock:
                self._lines += len(levels)
                self._by_level.update(levels)
        return len(levels)

    def not_recorded(self, records: Iterable[LineRecord]) -> None:
        """Count the device lines among ``records`` that could not be recorded."""
        count = sum(1 for record in records if record.origin is RecordOrigin.DEVICE)
        if count:
            with self._lock:
                self._not_recorded += count

    def restarted(self, at: datetime, reason: str) -> int:
        """Count a restart of the board; returns how many this session has seen."""
        with self._lock:
            self._restart_count += 1
            self._restarts.append(Restart(at, reason))
            return self._restart_count

    def lost(self, at: datetime) -> None:
        """The connection was lost; the session waits for the board."""
        with self._lock:
            self._disconnects.append(Disconnect(at, None))
            self._disconnect_count += 1

    def back(self, at: datetime) -> None:
        """The board is back after the latest loss."""
        with self._lock:
            if self._disconnects and self._disconnects[-1].back is None:
                lost = self._disconnects[-1].lost
                self._disconnects[-1] = Disconnect(lost, at)
                self._seconds_away += max(0.0, (at - lost).total_seconds())

    def end(self, at: datetime, unexpected: bool) -> None:
        with self._lock:
            if self._started is not None and self._ended is None:
                self._ended = (at, unexpected)

    def figures(self) -> SessionFigures | None:
        """The figures now, or None before the first session."""
        with self._lock:
            if self._started is None:
                return None
            port, baud, started = self._started
            ended, unexpected = self._ended if self._ended is not None else (None, None)
            return SessionFigures(
                port=port,
                baud=baud,
                started=started,
                lines=self._lines,
                by_level=dict(self._by_level),
                not_recorded=self._not_recorded,
                restart_count=self._restart_count,
                restarts=tuple(self._restarts),
                disconnect_count=self._disconnect_count,
                seconds_away=self._seconds_away,
                disconnects=tuple(self._disconnects),
                ended=ended,
                ended_unexpectedly=unexpected,
            )
