# SPDX-License-Identifier: MIT
"""Regression tests for B1 — TTY port not released on Linux disconnect.

The bug: when a connected USB serial device was unplugged (or otherwise
became unavailable), ``QSerialPort`` emitted ``SerialPortError`` of type
``ResourceError``. The handler logged and re-emitted the error but never
closed the port, so the kernel kept the tty node locked. On reconnect the
device re-enumerated to a new node (``/dev/ttyACM1`` instead of
``/dev/ttyACM0``), breaking automatic reconnection to the same path — a
real problem for the hardware bring-up workflow that unplugs frequently.

The fix closes the port on ``ResourceError`` (and only that error type)
before re-emitting, so the kernel releases the node immediately. All
other error types keep their previous behaviour.

``_on_port_error`` cannot be exercised through a constructed
``SerialManager`` without a real Qt stack (the constructor and the
``SerialPortError`` enum need PySide6), so these tests call the handler
directly with a minimal fake ``self`` and a stand-in error enum. This
keeps the test independent of Qt, so it runs identically under the test
stubs and against real PySide6. The end-to-end node-release behaviour is
verified manually per VERIFY_V1_2_0.md S-1 on Ubuntu hardware.
"""

from __future__ import annotations

import enum
from types import SimpleNamespace

import pytest

import core.serial_manager as serial_manager


class _FakeError(enum.Enum):
    """Stand-in for QSerialPort.SerialPortError covering the cases the
    handler distinguishes."""

    NoError = 0
    ResourceError = 1
    PermissionError = 2
    OpenError = 3
    WriteError = 4


@pytest.fixture()
def patched_enum(monkeypatch):
    """Point the module's QSerialPort.SerialPortError at the fake enum so
    the handler's comparisons resolve without real Qt."""
    monkeypatch.setattr(
        serial_manager, "QSerialPort",
        SimpleNamespace(SerialPortError=_FakeError),
    )


def _make_fake_self(error_string: str = "device disconnected"):
    """Minimal fake collaborator exposing exactly what _on_port_error
    touches. Records the order of close() and error_occurred.emit()."""
    events: list = []

    fake = SimpleNamespace(
        _port=SimpleNamespace(errorString=lambda: error_string),
        _port_name="/dev/ttyACM0",
        close=lambda: events.append("close"),
        error_occurred=SimpleNamespace(
            emit=lambda msg: events.append(("emit", msg)),
        ),
    )
    return fake, events


def _call(fake_self, error):
    serial_manager.SerialManager._on_port_error(fake_self, error)


class TestResourceErrorClosesPort:
    def test_resource_error_closes_before_emitting(self, patched_enum):
        fake, events = _make_fake_self("Resource temporarily unavailable")
        _call(fake, _FakeError.ResourceError)
        # close() runs first so the tty node is released, then the error
        # is surfaced to subscribers.
        assert events == ["close", ("emit", "Resource temporarily unavailable")]

    def test_resource_error_emits_exactly_once(self, patched_enum):
        fake, events = _make_fake_self()
        _call(fake, _FakeError.ResourceError)
        emits = [e for e in events if isinstance(e, tuple) and e[0] == "emit"]
        closes = [e for e in events if e == "close"]
        assert len(emits) == 1
        assert len(closes) == 1


class TestOtherErrorsUnchanged:
    @pytest.mark.parametrize(
        "error",
        [
            _FakeError.PermissionError,
            _FakeError.OpenError,
            _FakeError.WriteError,
        ],
    )
    def test_non_resource_errors_do_not_close(self, patched_enum, error):
        fake, events = _make_fake_self("some other error")
        _call(fake, error)
        # Error is still surfaced, but the port is NOT auto-closed.
        assert "close" not in events
        assert events == [("emit", "some other error")]


class TestNoError:
    def test_no_error_is_a_noop(self, patched_enum):
        fake, events = _make_fake_self()
        _call(fake, _FakeError.NoError)
        assert events == []


class TestErrorStringFallback:
    def test_blank_error_string_uses_code_fallback(self, patched_enum):
        # When errorString() is empty, the emitted message falls back to
        # the error code — and a ResourceError still closes.
        fake, events = _make_fake_self(error_string="")
        _call(fake, _FakeError.ResourceError)
        assert events[0] == "close"
        kind, msg = events[1]
        assert kind == "emit"
        assert "Serial error code" in msg
