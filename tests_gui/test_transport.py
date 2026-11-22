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
from PySide6.QtCore import QByteArray
from PySide6.QtSerialPort import QSerialPort
from PySide6.QtWidgets import QApplication

import core.transport as transport_module
from app.constants import SERIAL_LISTED_RECHECK_MS, SERIAL_STALL_CHECK_MS
from core.serial_settings import FlowControl, InputLines, LineSettings, Parity, StopBits
from core.transport import (
    PyserialTransport,
    QtSerialTransport,
    SystemInputQueue,
    available_ports,
    open_serial_transport,
)
from tests_gui.support import wait_until

_BAUD = 115200
_LINE = LineSettings()
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

        assert transport.open(name, _BAUD, _LINE) == ""
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
        assert transport.open(name, _BAUD, _LINE) == ""

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
        assert transport.open(os.ttyname(slave), _BAUD, _LINE) == ""

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
        assert transport.open("/dev/does-not-exist", _BAUD, _LINE) != ""

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


class _ListedPort:
    def __init__(self, name: str) -> None:
        self._name = name

    def portName(self) -> str:
        return self._name


_ERROR = QSerialPort.SerialPortError


def test_every_qt_serial_error_is_decided() -> None:
    """A Qt release that adds an error kind fails here until it is placed."""
    working = {_ERROR.NoError, _ERROR.OpenError, _ERROR.WriteError, _ERROR.TimeoutError,
               _ERROR.UnsupportedOperationError}
    lost, in_doubt = transport_module.LINK_LOST_ERRORS, transport_module.LINK_IN_DOUBT_ERRORS
    assert not (lost & in_doubt) and not ((lost | in_doubt) & working)
    assert lost | in_doubt | working == set(_ERROR)


@posix_only
class TestQtSerialErrors:
    """How each error QSerialPort reports on an open port is taken."""

    @pytest.fixture
    def opened(self, qapp, pseudo_terminal, monkeypatch):
        _, name = pseudo_terminal
        listed: list[_ListedPort] = []
        monkeypatch.setattr(
            transport_module.QSerialPortInfo, "availablePorts", staticmethod(lambda: list(listed)),
        )
        transport = QtSerialTransport()
        lost: list[str] = []
        errors: list[str] = []
        transport.link_lost.connect(lost.append)
        transport.error_occurred.connect(errors.append)
        assert transport.open(name, _BAUD, _LINE) == ""
        yield transport, lost, errors, listed, transport._port.portName()
        transport.close()

    @pytest.mark.parametrize("error", [
        _ERROR.ResourceError, _ERROR.PermissionError, _ERROR.DeviceNotFoundError,
        _ERROR.NotOpenError,
    ])
    def test_an_error_that_leaves_the_port_unusable_ends_the_link(self, qapp, opened, error):
        transport, lost, errors, _listed, _name = opened

        transport._port.errorOccurred.emit(error)
        transport._port.errorOccurred.emit(error)

        assert len(lost) == 1 and errors == []
        assert not transport._port.isOpen()

    @pytest.mark.parametrize("error", [_ERROR.ReadError, _ERROR.UnknownError])
    def test_a_doubtful_error_ends_the_link_once_the_port_is_no_longer_listed(
        self, qapp, opened, monkeypatch, error,
    ):
        transport, lost, errors, listed, name = opened
        clock = [100.0]
        monkeypatch.setattr(transport_module.time, "monotonic", lambda: clock[0])
        listed.append(_ListedPort(name))

        transport._port.errorOccurred.emit(error)
        assert lost == [] and len(errors) == 1 and transport._port.isOpen()

        listed.clear()
        clock[0] += SERIAL_LISTED_RECHECK_MS / 1000
        transport._port.errorOccurred.emit(error)
        assert len(lost) == 1 and not transport._port.isOpen()

    def test_a_repeated_doubtful_error_reads_the_port_list_twice_a_second_at_most(
        self, qapp, opened, monkeypatch,
    ):
        transport, lost, errors, _listed, name = opened
        clock = [100.0]
        monkeypatch.setattr(transport_module.time, "monotonic", lambda: clock[0])
        reads: list[int] = []
        monkeypatch.setattr(
            transport_module.QSerialPortInfo, "availablePorts",
            staticmethod(lambda: reads.append(1) or [_ListedPort(name)]),
        )

        for _ in range(25):
            transport._port.errorOccurred.emit(_ERROR.ReadError)
            clock[0] += 0.02

        assert len(reads) == 1 and lost == [] and len(errors) == 1
        assert transport._port.isOpen()

    @pytest.mark.parametrize("error", [
        _ERROR.WriteError, _ERROR.TimeoutError, _ERROR.UnsupportedOperationError,
    ])
    def test_an_error_that_leaves_the_port_working_is_reported_once(self, qapp, opened, error):
        transport, lost, errors, _listed, _name = opened

        for _ in range(_SETTLE_ROUNDS):
            transport._port.errorOccurred.emit(error)

        assert lost == [] and len(errors) == 1
        assert transport._port.isOpen()

class _StandInQueue:
    """The system's input queue as a test sets it."""

    def __init__(self) -> None:
        self.waiting_bytes: int | None = 0
        self.accepts = True
        self.rewaits: list[int] = []

    def waiting(self, handle: int) -> int | None:
        return self.waiting_bytes

    def rewait(self, handle: int) -> bool:
        self.rewaits.append(handle)
        return self.accepts


@posix_only
class TestStallCheck:
    """Input the system holds while Qt collects nothing sets Qt reading
    again; a port Qt is reading is left alone."""

    @pytest.fixture
    def opened(self, qapp, pseudo_terminal):
        master, name = pseudo_terminal
        system = _StandInQueue()
        transport = QtSerialTransport(system_input=system)
        received: list[bytes] = []
        transport.data_received.connect(received.append)
        assert transport.open(name, _BAUD, _LINE) == ""
        yield transport, system, master, received
        transport.close()

    def test_input_waiting_at_two_checks_with_nothing_collected_restarts_the_wait(
        self, qapp, opened, caplog,
    ):
        transport, system, _master, _received = opened
        system.waiting_bytes = 12

        transport._check_stall()
        assert system.rewaits == []
        with caplog.at_level("WARNING", logger=transport_module.__name__):
            transport._check_stall()

        assert system.rewaits == [int(transport._port.handle())]
        assert transport.stall_recoveries == 1
        assert "12 received bytes waited in the system" in caplog.text
        assert transport._port.isOpen()

    def test_input_waiting_at_one_check_only_is_left_to_qt(self, qapp, opened):
        transport, system, _master, _received = opened

        system.waiting_bytes = 5
        transport._check_stall()
        system.waiting_bytes = 0
        transport._check_stall()
        system.waiting_bytes = 5
        transport._check_stall()

        assert system.rewaits == [] and transport.stall_recoveries == 0

    def test_input_collected_between_checks_is_not_a_stall(self, qapp, opened):
        transport, system, master, received = opened
        system.waiting_bytes = 64

        for round_ in range(1, 5):
            transport._check_stall()
            os.write(master, b"line\n")
            wait_until(qapp, lambda expected=round_: len(b"".join(received)) >= 5 * expected)

        assert system.rewaits == [] and transport.stall_recoveries == 0

    def test_a_lasting_stall_is_met_at_every_second_check(self, qapp, opened):
        transport, system, _master, _received = opened
        system.waiting_bytes = 1

        for _ in range(6):
            transport._check_stall()

        assert len(system.rewaits) == 3 and transport.stall_recoveries == 3

    def test_a_refused_restart_is_logged_and_not_counted(self, qapp, opened, caplog):
        transport, system, _master, _received = opened
        system.waiting_bytes = 3
        system.accepts = False

        with caplog.at_level("ERROR", logger=transport_module.__name__):
            transport._check_stall()
            transport._check_stall()

        assert len(system.rewaits) == 1 and transport.stall_recoveries == 0
        assert "could not be restarted" in caplog.text

    def test_the_check_runs_only_while_the_port_is_open(self, qapp, opened):
        transport, _system, _master, _received = opened
        assert transport._stall_timer.isActive()
        assert transport._stall_timer.interval() == SERIAL_STALL_CHECK_MS

        transport.close()

        assert not transport._stall_timer.isActive()


class _StalledPort:
    """An open port whose received bytes Qt has stopped collecting: they
    stay in the system's queue until the wait for input is restarted."""

    def __init__(self, waiting: bytes) -> None:
        self.system = waiting
        self.collected = b""

    def isOpen(self) -> bool:
        return True

    def handle(self) -> int:
        return 7

    def portName(self) -> str:
        return "COM15"

    def bytesAvailable(self) -> int:
        return len(self.collected)

    def readAll(self) -> QByteArray:
        data, self.collected = self.collected, b""
        return QByteArray(data)


class _QueueOf:
    """The system's queue of a ``_StalledPort``; restarting the wait lets
    Qt collect what was waiting."""

    def __init__(self, port: _StalledPort) -> None:
        self._port = port

    def waiting(self, handle: int) -> int | None:
        return len(self._port.system)

    def rewait(self, handle: int) -> bool:
        self._port.collected += self._port.system
        self._port.system = b""
        return True


def test_input_left_in_the_system_reaches_the_session(qapp) -> None:
    port = _StalledPort(b"temp=21.5\r\n")
    transport = QtSerialTransport(system_input=_QueueOf(port))
    transport._port = port
    received: list[bytes] = []
    transport.data_received.connect(received.append)

    for _ in range(3):
        transport._read_available()
        transport._check_stall()
    transport._read_available()

    assert received == [b"temp=21.5\r\n"]
    assert transport.stall_recoveries == 1


@posix_only
def test_without_a_system_queue_no_stall_check_runs(qapp, pseudo_terminal):
    _, name = pseudo_terminal
    transport = QtSerialTransport()

    assert transport.open(name, _BAUD, _LINE) == ""
    try:
        assert not transport._stall_timer.isActive()
    finally:
        transport.close()


def test_qt_gives_the_port_handle_as_an_integer(qapp) -> None:
    """The stall check passes Qt's handle to the system as an integer."""
    assert isinstance(int(QSerialPort().handle()), int)


@pytest.mark.skipif(sys.platform != "win32", reason="the system's input queue is read on Windows")
def test_the_system_queue_refuses_a_handle_that_is_not_a_port() -> None:
    system = SystemInputQueue()

    assert system.waiting(int(QSerialPort().handle())) is None
    assert system.rewait(int(QSerialPort().handle())) is False


@posix_only
class TestQtLineSettings:
    def test_framing_and_flow_control_are_applied(self, qapp, pseudo_terminal):
        _, name = pseudo_terminal
        transport = QtSerialTransport()
        line = LineSettings(7, Parity.EVEN, StopBits.TWO, FlowControl.SOFTWARE)

        assert transport.open(name, _BAUD, line) == ""

        port = transport._port
        assert port.dataBits() == QSerialPort.DataBits.Data7
        assert port.parity() == QSerialPort.Parity.EvenParity
        assert port.stopBits() == QSerialPort.StopBits.TwoStop
        assert port.flowControl() == QSerialPort.FlowControl.SoftwareControl
        transport.close()

    def test_controls_on_a_closed_port_are_refused(self, qapp):
        transport = QtSerialTransport()

        assert transport.set_dtr(True) == "The port is not open"
        assert transport.set_rts(True) == "The port is not open"
        assert transport.set_break(True) == "The port is not open"
        assert transport.input_lines() == InputLines()


class FakeSerial:
    """Stands in for ``serial.Serial``."""

    instances: ClassVar[list[FakeSerial]] = []
    open_error: ClassVar[Exception | None] = None

    def __init__(self, port, baudrate, **options) -> None:
        if FakeSerial.open_error is not None:
            raise FakeSerial.open_error
        self.port = port
        self.baudrate = baudrate
        self.options = options
        self.is_open = True
        self.dtr = False
        self.rts = False
        self.break_condition = False
        self.cts = True
        self.dsr = False
        self.cd = True
        self.ri = False
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
    monkeypatch.setitem(sys.modules, "serial", module)
    return FakeSerial


class TestPyserialTransport:
    def test_reads_and_writes(self, qapp, fake_serial):
        transport = PyserialTransport()
        received: list[bytes] = []
        transport.data_received.connect(received.append)

        assert transport.open("PORT0", _BAUD, _LINE) == ""
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
        transport.open("PORT0", _BAUD, _LINE)
        device = fake_serial.instances[0]

        device.arrive(b"arrived just before close")
        wait_until(qapp, lambda: device.in_waiting == 0, timeout=5.0)
        transport.close()
        assert b"".join(received) == b"arrived just before close"

    def test_read_failure_closes_the_port_and_reports_a_lost_link(self, qapp, fake_serial):
        transport = PyserialTransport()
        lost: list[str] = []
        transport.link_lost.connect(lost.append)
        transport.open("PORT0", _BAUD, _LINE)
        device = fake_serial.instances[0]

        device.read_error = OSError("device reports readiness but returned no data")
        wait_until(qapp, lambda: bool(lost))
        assert not device.is_open
        assert transport.write(b"x") == "The port is not open"

    def test_a_driver_message_is_passed_on_as_a_phrase(self, qapp, fake_serial):
        transport = PyserialTransport()
        lost: list[str] = []
        transport.link_lost.connect(lost.append)
        transport.open("PORT0", _BAUD, _LINE)

        fake_serial.instances[0].read_error = OSError("  Access is denied.  ")
        wait_until(qapp, lambda: bool(lost))

        assert lost == ["Access is denied"]

    def test_open_failure_gives_a_reason(self, qapp, fake_serial):
        fake_serial.open_error = OSError("could not open port")
        assert PyserialTransport().open("PORT0", _BAUD, _LINE) == "could not open port"

    def test_a_port_whose_control_lines_cannot_be_set_is_closed_again(
        self, qapp, fake_serial, monkeypatch,
    ):
        class RefusingDtr(FakeSerial):
            def __init__(self, port, baudrate, **options) -> None:
                super().__init__(port, baudrate, **options)
                self.opened = True

            def __setattr__(self, name: str, value: object) -> None:
                if name == "dtr" and getattr(self, "opened", False):
                    raise OSError("DTR could not be set")
                super().__setattr__(name, value)

        monkeypatch.setattr(sys.modules["serial"], "Serial", RefusingDtr)

        assert PyserialTransport().open("PORT0", _BAUD, _LINE) == "DTR could not be set"
        assert not fake_serial.instances[0].is_open

    def test_user_close_is_not_reported_as_a_lost_link(self, qapp, fake_serial):
        transport = PyserialTransport()
        lost: list[str] = []
        transport.link_lost.connect(lost.append)
        transport.open("PORT0", _BAUD, _LINE)
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
        transport.open("PORT0", _BAUD, _LINE)

        # The read fails, and the user closes before that report has
        # been delivered to the owning thread.
        fake_serial.instances[0].read_error = OSError("device removed")
        transport._thread.join()
        transport.close()
        for _ in range(_SETTLE_ROUNDS):
            qapp.processEvents()
        assert lost == []


class TestPyserialLineSettings:
    def test_framing_flow_control_and_exclusive_lock_are_asked_for(self, qapp, fake_serial):
        transport = PyserialTransport()
        line = LineSettings(5, Parity.MARK, StopBits.ONE_AND_HALF, FlowControl.HARDWARE,
                            dtr_on_open=False, rts_on_open=False)

        assert transport.open("PORT0", _BAUD, line) == ""

        options = fake_serial.instances[0].options
        assert options["bytesize"] == 5
        assert options["parity"] == "M"
        assert options["stopbits"] == 1.5
        assert options["rtscts"] is True and options["xonxoff"] is False
        assert options["exclusive"] is True
        transport.close()

    def test_lines_at_open_follow_the_settings(self, qapp, fake_serial):
        transport = PyserialTransport()
        transport.open("PORT0", _BAUD, LineSettings(dtr_on_open=False, rts_on_open=True))
        serial_port = fake_serial.instances[0]

        assert (serial_port.dtr, serial_port.rts) == (False, True)
        transport.close()

    def test_hardware_flow_control_leaves_rts_to_the_driver(self, qapp, fake_serial):
        transport = PyserialTransport()
        transport.open("PORT0", _BAUD, LineSettings(flow_control=FlowControl.HARDWARE,
                                                    rts_on_open=True))

        assert fake_serial.instances[0].rts is False
        transport.close()

    def test_controls_and_input_lines(self, qapp, fake_serial):
        transport = PyserialTransport()
        transport.open("PORT0", _BAUD, _LINE)
        serial_port = fake_serial.instances[0]

        assert transport.set_dtr(False) == "" and serial_port.dtr is False
        assert transport.set_rts(False) == "" and serial_port.rts is False
        assert transport.set_break(True) == "" and serial_port.break_condition is True
        assert transport.input_lines() == InputLines(cts=True, dsr=False, dcd=True, ri=False)
        transport.close()
        assert transport.set_dtr(True) == "The port is not open"
        assert transport.input_lines() == InputLines()


class TestDriverSelection:
    def test_falls_back_to_pyserial_when_qt_cannot_open(self, qapp, fake_serial):
        transport, reason = open_serial_transport("PORT-ONLY-PYSERIAL-OPENS", _BAUD)
        assert reason == ""
        assert isinstance(transport, PyserialTransport)
        transport.close()

    def test_a_port_refused_to_qt_is_not_opened_through_pyserial(
        self, qapp, fake_serial, monkeypatch,
    ):
        def refuse(transport, port, baud, line):
            transport.access_refused = True
            return "Permission error while locking the device"

        monkeypatch.setattr(QtSerialTransport, "open", refuse)

        transport, reason = open_serial_transport("PORT-IN-USE", _BAUD, _LINE)

        assert transport is None
        assert reason == "Permission error while locking the device"
        assert fake_serial.instances == []

    def test_a_named_driver_is_the_only_one_tried(self, qapp, fake_serial, monkeypatch):
        tried: list[str] = []

        def qt_fails(transport, port, baud, line):
            tried.append("qt")
            return "The system cannot find the file specified"

        monkeypatch.setattr(QtSerialTransport, "open", qt_fails)

        transport, reason = open_serial_transport("COM3", _BAUD, _LINE, QtSerialTransport)
        assert (transport, reason) == (None, "The system cannot find the file specified")
        assert tried == ["qt"] and fake_serial.instances == []

        transport, reason = open_serial_transport("COM3", _BAUD, _LINE, PyserialTransport)
        assert isinstance(transport, PyserialTransport) and reason == ""
        assert tried == ["qt"]
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
