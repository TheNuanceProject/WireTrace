# SPDX-License-Identifier: MIT
"""B10 investigation + regression — pyserial-fallback teardown signals.

When ``QSerialPort`` cannot open a port, ``SerialManager`` falls back to a
plain ``threading.Thread`` (``_read_loop``) that reads via pyserial and
emits ``data_received`` / ``error_occurred``. The audit flagged a
*suspected* race: does that worker stop cleanly on ``_close_fallback``, or
keep emitting signals after teardown has begun?

Static analysis found a concrete asymmetry: ``error_occurred.emit`` was
guarded by ``self._fallback_running`` but ``data_received.emit`` was not.
So a read completing during ``_close_fallback`` could deliver
``data_received`` after ``_fallback_running`` had already gone ``False`` —
a signal emitted after the manager considers the fallback stopped. The
fix guards the data emit the same way.

The fallback path can't be triggered without a port that defeats
``QSerialPort`` (hardware-specific), and ``SerialManager`` itself can't be
constructed under the headless Qt stubs. So these tests exercise the real
``_start_fallback_reader`` / ``_close_fallback`` methods against a minimal
fake ``self`` and a fake serial whose reads are gated by an ``Event``,
which lets the teardown window be reproduced deterministically rather than
relied on by luck. Full stress verification on real fallback-triggering
hardware (VERIFY S-7) still belongs on the target machine.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import core.serial_manager as serial_manager


class _RecordingSignal:
    """Records each emit together with a snapshot taken by the caller."""

    def __init__(self, owner: object, attr: str) -> None:
        self._owner = owner
        self._attr = attr  # flag name to snapshot at emit time
        self.emits: list[tuple[object, bool]] = []

    def emit(self, payload: object) -> None:
        self.emits.append((payload, getattr(self._owner, self._attr)))


class _FakeSerial:
    """Fake pyserial handle. ``read`` can be gated for deterministic
    interleaving with teardown."""

    def __init__(self, data: bytes = b"X", gate: threading.Event | None = None,
                 entered: threading.Event | None = None) -> None:
        self.is_open = True
        self._data = data
        self._gate = gate          # if set, read() blocks until set()
        self._entered = entered     # if set, read() signals on entry
        self.in_waiting = 0
        self.closed = False

    def read(self, _n: int) -> bytes:
        if self._entered is not None:
            self._entered.set()
        if self._gate is not None:
            self._gate.wait(timeout=2.0)
        return self._data

    def close(self) -> None:
        self.closed = True
        self.is_open = False


def _make_fake_self() -> SimpleNamespace:
    fake = SimpleNamespace(
        _fallback_running=False,
        _fallback_serial=None,
        _fallback_thread=None,
    )
    fake.data_received = _RecordingSignal(fake, "_fallback_running")
    fake.error_occurred = _RecordingSignal(fake, "_fallback_running")
    return fake


def _start(fake: SimpleNamespace) -> None:
    serial_manager.SerialManager._start_fallback_reader(fake)


def _close(fake: SimpleNamespace) -> None:
    serial_manager.SerialManager._close_fallback(fake)


class TestTeardownTerminatesThread:
    def test_close_stops_thread_and_clears_state(self):
        fake = _make_fake_self()
        fake._fallback_serial = _FakeSerial(data=b"A")
        _start(fake)
        time.sleep(0.05)  # let it spin a few iterations
        thread = fake._fallback_thread
        assert thread is not None and thread.is_alive()

        _close(fake)

        assert fake._fallback_running is False
        assert fake._fallback_thread is None
        assert fake._fallback_serial is None
        assert not thread.is_alive()

    def test_normal_reads_emit_while_running(self):
        fake = _make_fake_self()
        fake._fallback_serial = _FakeSerial(data=b"DATA")
        _start(fake)
        time.sleep(0.05)
        _close(fake)

        # Happy path: data flowed through the fallback reader. (We don't
        # assert the flag snapshot here — _close runs concurrently with
        # the spinning thread, so the guard/snapshot ordering is
        # nondeterministic. The post-teardown guarantee is pinned
        # deterministically in TestNoSignalAfterTeardownBegins.)
        assert fake.data_received.emits, "expected data while running"
        assert all(payload == b"DATA" for payload, _flag in fake.data_received.emits)


class TestNoSignalAfterTeardownBegins:
    """The B10 fix: no data_received emit once _fallback_running is False."""

    def test_data_not_emitted_after_flag_cleared(self):
        gate = threading.Event()
        entered = threading.Event()
        fake = _make_fake_self()
        fake._fallback_serial = _FakeSerial(data=b"LATE", gate=gate, entered=entered)

        _start(fake)
        # Wait until the worker is blocked inside read().
        assert entered.wait(timeout=2.0)

        # Simulate the start of teardown: flag clears before the read
        # completes. (This is what _close_fallback does first.)
        fake._fallback_running = False
        gate.set()  # let the gated read return its data

        # Let the loop finish its current iteration and exit.
        if fake._fallback_thread:
            fake._fallback_thread.join(timeout=2.0)

        # No data_received may have been emitted while the flag was False.
        late = [p for p, flag in fake.data_received.emits if flag is False]
        assert late == [], (
            "data_received emitted after teardown began (B10 window open)"
        )

    def test_error_not_emitted_after_flag_cleared(self):
        # The error path was already guarded; pin it so it stays guarded.
        gate = threading.Event()
        entered = threading.Event()

        class _RaisingSerial(_FakeSerial):
            def read(self, _n: int) -> bytes:
                if self._entered is not None:
                    self._entered.set()
                if self._gate is not None:
                    self._gate.wait(timeout=2.0)
                raise OSError("device vanished")

        fake = _make_fake_self()
        fake._fallback_serial = _RaisingSerial(gate=gate, entered=entered)

        _start(fake)
        assert entered.wait(timeout=2.0)
        fake._fallback_running = False
        gate.set()
        if fake._fallback_thread:
            fake._fallback_thread.join(timeout=2.0)

        late = [p for p, flag in fake.error_occurred.emits if flag is False]
        assert late == []
