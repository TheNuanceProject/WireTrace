# SPDX-License-Identifier: GPL-3.0-or-later
"""Looks for a tab's lost board, on a thread of its own.

Reading the system's port list can take tens of milliseconds (Windows
asks its device manager), so it never runs on the GUI thread. The
watcher's thread records the identity of the tab's port when the tab
connects, and says when it has. The tab opens the port then, or after
``RECONNECT_RECORD_WAIT_MS`` should the list be slow, so a board that
drops as soon as it is opened is still known. While the tab waits for
its lost board, the thread reads the list every
``RECONNECT_POLL_INTERVAL_MS`` and applies the rules of
``core/reconnect.py``. No list is read while the tab is not waiting.

When it finds the board it says so and pauses while the tab opens it.
If the port cannot be opened yet, the tab asks it to try again later:
the pause doubles from ``RECONNECT_RETRY_FIRST_MS`` up to
``RECONNECT_RETRY_MAX_MS``. When a port only the user can decide about
appears, it asks once and keeps looking: a sure match found meanwhile
takes the question's place, and a question about a port that has gone
is withdrawn. A port the user confirmed is taken again on later tries,
while it is present, without asking again.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from PySide6.QtCore import QObject, Signal, Slot

from app.constants import (
    MS_PER_SECOND,
    RECONNECT_POLL_INTERVAL_MS,
    RECONNECT_RETRY_FIRST_MS,
    RECONNECT_RETRY_MAX_MS,
    RECONNECT_STOP_WAIT_MS,
)
from core.port_identity import PortIdentity, PortLister, identity_of, read_identities
from core.reconnect import Ask, Basis, Board, Found, find_board, record_board, still_the_board
from ui.device_claims import CLAIMS, DeviceClaims

logger = logging.getLogger(__name__)

_THREAD_NAME = "wiretrace-reconnect"


class ReconnectWatcher(QObject):
    """The identity of one tab's board, and the search for it once lost.

    Signals:
        recorded(str):     The identity of the port to be opened was read.
        found(object):     ``Found``: the board is back on a port.
        asked(object):     ``Ask``: a port of the same model appeared and
                           only the user can say whether it is the board.
        withdrawn(str):    The port asked about is no longer present.
    """

    recorded = Signal(str)
    found = Signal(object)
    asked = Signal(object)
    withdrawn = Signal(str)
    _read_done = Signal(str, int)
    _answered = Signal(object, int)
    _gone = Signal(str, int)

    def __init__(
        self,
        holder: object,
        claims: DeviceClaims = CLAIMS,
        read: PortLister = read_identities,
        parent: QObject | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(parent)
        self._holder = holder
        self._claims = claims
        self._read = read
        self._clock = clock
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stopping = False
        # Changes whenever the search is started, stopped or resumed, so
        # an answer to an older search is dropped.
        self._generation = 0
        # Changes with each port to record, apart from the search: a
        # record still being read when the board is lost is kept.
        self._record_generation = 0
        self._to_record: str | None = None
        self._board: Board | None = None
        self._last_present: list[PortIdentity] = []
        self._watching = False
        self._paused = False
        self._by_port_name = True
        self._declined: set[str] = set()
        self._present_at_loss: frozenset[PortIdentity] | None = None
        self._asking: PortIdentity | None = None
        self._confirmed: PortIdentity | None = None
        self._resume_at = 0.0
        self._retry_ms = 0
        self._read_done.connect(self._on_read_done)
        self._answered.connect(self._on_answered)
        self._gone.connect(self._on_gone)

    @property
    def board(self) -> Board | None:
        """The board recorded at the latest connect, once it has been read."""
        with self._condition:
            return self._board

    def record(self, port: str) -> None:
        """Record the identity of ``port``, about to be opened; ``recorded`` follows."""
        with self._condition:
            self._record_generation += 1
            self._to_record = port
            self._board = None
            self._condition.notify()
        self._ensure_thread()

    def adopt(self, identity: PortIdentity) -> None:
        """Record ``identity``, the port the board came back on, as the board."""
        with self._condition:
            self._record_generation += 1
            self._to_record = None
            present = self._last_present or [identity]
            self._board = record_board(identity, present)
            self._condition.notify()

    def watch(self, by_port_name: bool) -> None:
        """Look for the board until it is found, or ``stop`` is called."""
        with self._condition:
            self._generation += 1
            self._watching = True
            self._paused = False
            self._by_port_name = by_port_name
            self._declined.clear()
            self._present_at_loss = None
            self._asking = None
            self._confirmed = None
            self._resume_at = 0.0
            self._retry_ms = 0
            self._condition.notify()
        self._ensure_thread()

    def set_by_port_name(self, on: bool) -> None:
        """Whether a port without USB identity may be found by its name."""
        with self._condition:
            self._by_port_name = on
            self._condition.notify()

    def hold(self, confirmed: PortIdentity) -> None:
        """Pause the search while the tab opens ``confirmed``, a port the user
        said is the board; should it not open, later tries take it again."""
        with self._condition:
            self._generation += 1
            self._paused = True
            self._asking = None
            self._confirmed = confirmed

    def decline(self, port: str) -> None:
        """The user said ``port`` is not the board: never offer it again, and look on."""
        with self._condition:
            self._declined.add(port)
            if self._asking is not None and self._asking.port == port:
                self._asking = None
            self._condition.notify()

    def retry_later(self) -> None:
        """The port found could not be opened yet: look again after a growing pause."""
        with self._condition:
            self._generation += 1
            self._retry_ms = min(
                max(self._retry_ms * 2, RECONNECT_RETRY_FIRST_MS), RECONNECT_RETRY_MAX_MS,
            )
            self._resume_at = self._clock() + self._retry_ms / MS_PER_SECOND
            self._paused = False
            self._asking = None
            self._condition.notify()

    def stop(self) -> None:
        """Stop looking."""
        with self._condition:
            self._generation += 1
            self._watching = False
            self._asking = None
            self._condition.notify()

    def shutdown(self) -> None:
        """End the thread and wait for it, for at most ``RECONNECT_STOP_WAIT_MS``.

        Nothing is reported after this, even by a thread still reading.
        """
        with self._condition:
            self._stopping = True
            self._generation += 1
            self._record_generation += 1
            self._condition.notify()
        if self._thread is not None:
            self._thread.join(RECONNECT_STOP_WAIT_MS / MS_PER_SECOND)
            if self._thread.is_alive():
                logger.warning("The list of ports is still being read; the tab closes without it")
            self._thread = None

    def check_for(self, found: PortIdentity) -> Callable[[], bool]:
        """A test, run once the found port is open, that it still shows the same device."""
        read = self._read
        return lambda: still_the_board(found, read())

    # ── Thread ───────────────────────────────────────────────────────────

    def _ensure_thread(self) -> None:
        if self._thread is None and not self._stopping:
            self._thread = threading.Thread(target=self._run, name=_THREAD_NAME, daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._stopping and self._to_record is None and not self._searching():
                    self._condition.wait()
                if self._stopping:
                    return
                if self._to_record is None:
                    waiting = self._resume_at - self._clock()
                    if waiting > 0:
                        self._condition.wait(waiting)
                        continue
                record_generation = self._record_generation
                generation = self._generation
                port, self._to_record = self._to_record, None
                board, declined = self._board, frozenset(self._declined)
                by_port_name = self._by_port_name
                present_at_loss, asking = self._present_at_loss, self._asking
                confirmed = self._confirmed
            present = self._read()
            if port is not None:
                recorded = record_board(identity_of(port, present), present)
                with self._condition:
                    if record_generation == self._record_generation:
                        self._board = recorded
                        self._last_present = present
                        self._read_done.emit(port, record_generation)
                continue
            if present_at_loss is None:
                # The ports present as the board was lost, apart from its
                # own, are other boards: none of them is ever offered.
                present_at_loss = frozenset(
                    identity for identity in present if identity.port != board.identity.port
                )
            held_elsewhere = self._claims.held_by_others(self._holder)
            decision = find_board(
                board, present,
                held_elsewhere=held_elsewhere,
                declined=declined,
                present_at_loss=present_at_loss,
                by_port_name=by_port_name,
            )
            if confirmed is not None and not isinstance(decision, Found):
                if confirmed not in present:
                    confirmed = None
                elif confirmed.port not in held_elsewhere:
                    decision = Found(confirmed, Basis.CONFIRMED)
            with self._condition:
                if generation != self._generation:
                    continue
                self._confirmed = confirmed
                self._present_at_loss = present_at_loss
                self._last_present = present
                if isinstance(decision, Found):
                    self._paused = True
                    self._asking = None
                    self._answered.emit(decision, generation)
                    continue
                if asking is not None and asking not in present:
                    self._asking = None
                    self._gone.emit(asking.port, generation)
                elif isinstance(decision, Ask) and asking is None:
                    self._asking = decision.identity
                    self._answered.emit(decision, generation)
                self._condition.wait(RECONNECT_POLL_INTERVAL_MS / MS_PER_SECOND)

    def _searching(self) -> bool:
        return self._watching and not self._paused and self._board is not None

    def _current(self, generation: int) -> bool:
        with self._condition:
            return generation == self._generation and self._watching

    @Slot(str, int)
    def _on_read_done(self, port: str, record_generation: int) -> None:
        with self._condition:
            current = record_generation == self._record_generation
        if current:
            self.recorded.emit(port)

    @Slot(object, int)
    def _on_answered(self, decision: Found | Ask, generation: int) -> None:
        if not self._current(generation):
            return
        if isinstance(decision, Found):
            self.found.emit(decision)
        else:
            self.asked.emit(decision)

    @Slot(str, int)
    def _on_gone(self, port: str, generation: int) -> None:
        if self._current(generation):
            self.withdrawn.emit(port)
