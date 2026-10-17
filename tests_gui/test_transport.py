# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the serial drivers.

The Qt driver is exercised against a pseudo-terminal, which behaves as
a serial port on Linux and macOS. Windows has no equivalent, so those
tests are skipped there; the pipeline tests cover the rest of the path
on every platform through the loopback port.

The pyserial driver is exercised against a stand-in ``serial`` module,
because a pseudo-terminal rejects the modem-control lines it sets.
"""

from __future__ import annotations

import os
import sys
import threading
import types
from typing import ClassVar

import pytest
from PySide6.QtWidgets import QApplication

import core.transport as transport_module
from core.transport import (
    PyserialTransport,
    QtSerialTransport,
    available_ports,
    open_serial_transport,
)
from tests_gui.support import wait_until

_BAUD = 115200
# Event-loop passes given to a signal that must not be emitted again.
_SETTLE_ROUNDS = 50
posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="pseudo-terminals are POSIX only",
)


@pytest.fixture
def pseudo_terminal():
    import pty
    import tty

    master, slave = pty.openpty()
    tty.setraw(slave)
    yield master, os.ttyname(slave)
    os.close(master)
    os.close(slave)


@posix_only
class TestQtSerialTransport:
    def test_reads_and_writes(self, qapp: QApplication, pseudo_terminal):
        master, name = pseudo_terminal
        transport = QtSerialTransport()
        received: list[bytes] = []
        transport.data_received.connect(received.append)

        assert transport.open(name, _BAUD) == ""
        os.write(master, b"hello\nworld\n")
        wait_until(qapp, lambda: b"".join(received) == b"hello\nworld\n")

        assert transport.write(b"cmd\n") == ""
        wait_until(qapp, lambda: transport._port.bytesToWrite() == 0)
        assert os.read(master, 100) == b"cmd\n"
        transport.close()

    def test_close_delivers_bytes_not_yet_collected(self, qapp, pseudo_terminal):
        master, name = pseudo_terminal
        transport = QtSerialTransport()
        received: list[bytes] = []
        transport.data_received.connect(received.append)
        assert transport.open(name, _BAUD) == ""

        os.write(master, b"arrived just before close")
        transport.close()
        assert b"".join(received) == b"arrived just before close"

    def test_vanished_device_is_reported_once_and_its_last_bytes_arrive(self, qapp):
        import pty
        import tty

        master, slave = pty.openpty()
        tty.setraw(slave)
        transport = QtSerialTransport()
        received: list[bytes] = []
        lost: list[str] = []
        transport.data_received.connect(received.append)
        transport.link_lost.connect(lost.append)
        assert transport.open(os.ttyname(slave), _BAUD) == ""

        os.write(master, b"last words\n")
        wait_until(qapp, lambda: b"".join(received) == b"last words\n")
        os.close(slave)
        os.close(master)
        wait_until(qapp, lambda: bool(lost))
        for _ in range(_SETTLE_ROUNDS):
            qapp.processEvents()

        assert len(lost) == 1
        assert transport.write(b"x") == "The port is not open"

    def test_open_failure_gives_a_reason(self, qapp):
        transport = QtSerialTransport()
        assert transport.open("/dev/does-not-exist", _BAUD) != ""

    def test_write_when_closed_is_reported(self, qapp):
        assert QtSerialTransport().write(b"x") == "The port is not open"

    def test_close_when_not_open_is_harmless(self, qapp):
        QtSerialTransport().close()

    def test_factory_opens_with_the_qt_driver(self, qapp, pseudo_terminal):
        _, name = pseudo_terminal
        transport, reason = open_serial_transport(name, _BAUD)
        assert reason == ""
        assert isinstance(transport, QtSerialTransport)
        transport.close()


class FakeSerial:
    """Stands in for ``serial.Serial``."""

    instances: ClassVar[list[FakeSerial]] = []
    open_error: ClassVar[Exception | None] = None

    def __init__(self, port, baudrate, **_options) -> None:
        if FakeSerial.open_error is not None:
            raise FakeSerial.open_error
        self.port = port
        self.baudrate = baudrate
        self.is_open = True
        self.dtr = False
        self.rts = False
        self.written: list[bytes] = []
        self.read_error: Exception | None = None
        self._incoming = bytearray()
        self._lock = threading.Lock()
        self._data_ready = threading.Event()
        FakeSerial.instances.append(self)

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._incoming)

    def read(self, size: int) -> bytes:
        self._data_ready.wait(0.02)
        if self.read_error is not None:
            raise self.read_error
        with self._lock:
            data = bytes(self._incoming[:size])
            del self._incoming[:size]
            if not self._incoming:
                self._data_ready.clear()
        return data

    def write(self, data: bytes) -> int:
        self.written.append(data)
        return len(data)

    def close(self) -> None:
        self.is_open = False

    def arrive(self, data: bytes) -> None:
        with self._lock:
            self._incoming.extend(data)
            self._data_ready.set()


@pytest.fixture
def fake_serial(monkeypatch):
    FakeSerial.instances = []
    FakeSerial.open_error = None
    module = types.ModuleType("serial")
    module.Serial = FakeSerial
    module.EIGHTBITS = 8
    module.PARITY_NONE = "N"
    module.STOPBITS_ONE = 1
    monkeypatch.setitem(sys.modules, "serial", module)
    return FakeSerial


class TestPyserialTransport:
    def test_reads_and_writes(self, qapp, fake_serial):
        transport = PyserialTransport()
        received: list[bytes] = []
        transport.data_received.connect(received.append)

        assert transport.open("PORT0", _BAUD) == ""
        device = fake_serial.instances[0]
        assert device.dtr and device.rts
        device.arrive(b"hello\n")
        wait_until(qapp, lambda: b"".join(received) == b"hello\n")

        assert transport.write(b"cmd\n") == ""
        assert device.written == [b"cmd\n"]
        transport.close()
        assert not device.is_open

    def test_close_delivers_bytes_already_read(self, qapp, fake_serial):
        transport = PyserialTransport()
        received: list[bytes] = []
        transport.data_received.connect(received.append)
        transport.open("PORT0", _BAUD)
        device = fake_serial.instances[0]

        device.arrive(b"arrived just before close")
        wait_until(qapp, lambda: device.in_waiting == 0, timeout=5.0)
        transport.close()
        assert b"".join(received) == b"arrived just before close"

    def test_read_failure_closes_the_port_and_reports_a_lost_link(self, qapp, fake_serial):
        transport = PyserialTransport()
        lost: list[str] = []
        transport.link_lost.connect(lost.append)
        transport.open("PORT0", _BAUD)
        device = fake_serial.instances[0]

        device.read_error = OSError("device reports readiness but returned no data")
        wait_until(qapp, lambda: bool(lost))
        assert not device.is_open
        assert transport.write(b"x") == "The port is not open"

    def test_open_failure_gives_a_reason(self, qapp, fake_serial):
        fake_serial.open_error = OSError("could not open port")
        assert PyserialTransport().open("PORT0", _BAUD) == "could not open port"

    def test_user_close_is_not_reported_as_a_lost_link(self, qapp, fake_serial):
        transport = PyserialTransport()
        lost: list[str] = []
        transport.link_lost.connect(lost.append)
        transport.open("PORT0", _BAUD)
        transport.close()
        for _ in range(_SETTLE_ROUNDS):
            qapp.processEvents()
        assert lost == []

    def test_read_failure_overtaken_by_a_user_close_is_not_reported(
        self, qapp, fake_serial,
    ):
        transport = PyserialTransport()
        lost: list[str] = []
        transport.link_lost.connect(lost.append)
        transport.open("PORT0", _BAUD)

        # The read fails, and the user closes before that report has
        # been delivered to the owning thread.
        fake_serial.instances[0].read_error = OSError("device removed")
        transport._thread.join()
        transport.close()
        for _ in range(_SETTLE_ROUNDS):
            qapp.processEvents()
        assert lost == []


class TestDriverSelection:
    def test_falls_back_to_pyserial_when_qt_cannot_open(self, qapp, fake_serial):
        transport, reason = open_serial_transport("PORT-ONLY-PYSERIAL-OPENS", _BAUD)
        assert reason == ""
        assert isinstance(transport, PyserialTransport)
        transport.close()

    def test_reports_the_qt_reason_when_both_fail(self, qapp, fake_serial):
        fake_serial.open_error = OSError("could not open port")
        transport, reason = open_serial_transport("PORT-NOBODY-OPENS", _BAUD)
        assert transport is None
        assert reason != ""


class _FakePortInfo:
    def __init__(self, name: str, description: str) -> None:
        self._name = name
        self._description = description

    def portName(self) -> str:
        return self._name

    def description(self) -> str:
        return self._description

    def manufacturer(self) -> str:
        return "Maker"


def test_available_ports_are_described_and_sorted(qapp, monkeypatch):
    monkeypatch.setattr(
        transport_module.QSerialPortInfo, "availablePorts",
        staticmethod(lambda: [
            _FakePortInfo("COM7", "USB-SERIAL CH340"), _FakePortInfo("COM3", ""),
        ]),
    )
    ports = available_ports()
    assert [port.device for port in ports] == ["COM3", "COM7"]
    assert ports[0].display_name == "COM3"
    assert ports[1].display_name == "COM7 \u00b7 USB-SERIAL CH340"
