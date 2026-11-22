# SPDX-License-Identifier: GPL-3.0-or-later
"""Serial port access behind one interface.

``Transport`` is what the reader talks to: open, write, close, the
control lines, and a signal for received bytes. Two drivers implement it:

  QtSerialTransport   Qt's QSerialPort. The primary driver.
  PyserialTransport   pyserial. Used when QSerialPort cannot open a
                      port, which happens with a few USB-CDC chipsets.

``open_serial_transport`` tries them in that order.

Threading:
  A transport belongs to the thread that creates it, which is the
  reader thread. Nothing here runs on the GUI thread.

A lost device:
  An open port whose device was unplugged or reset, or whose handle the
  system took back, is closed at once and reported by ``link_lost``.
  Drivers report this differently: on Windows a CH340 gives "Access is
  denied", others a resource error. An error that can only mean the
  port is unusable ends the link (``LINK_LOST_ERRORS``). A read error
  or an unknown error ends it if the system no longer lists the port
  (``LINK_IN_DOUBT_ERRORS``). Any other error leaves the port working
  and is reported by ``error_occurred``.

Messages:
  A reason given by a driver (the system's or the library's words) is
  passed on as a phrase, without a final full stop, for the caller to
  place in its own sentence: "Access is denied", not "Access is denied.".

Reading (QtSerialTransport):
  A timer polls the port for available bytes, and Qt's readyRead signal
  is connected as well. The poll exists because readyRead silently
  fails on Windows with several common USB-to-serial chipsets (CH340,
  CP2102, FTDI, PL2303); the signal gives a faster response where it
  does work.

  The poll can only take what Qt has collected. On Windows, Qt collects
  by waiting for the driver to report a received character
  (``WaitCommEvent`` for ``EV_RXCHAR``) and then reading. A driver that
  does not report a character arriving just before that wait begins
  leaves the wait pending while the input sits in the system's queue,
  and Qt collects nothing more: the port stays open and the session
  receives nothing. So, on Windows, a second check reads the system's
  own count of waiting input (``ClearCommError``). Input found waiting
  at two checks in a row, with nothing collected between them, means
  Qt's wait was missed; setting the port's event mask again ends that
  wait at once (``SetCommMask``, as Windows documents for
  ``WaitCommEvent``), and Qt reads the waiting input as usual. Nothing
  is lost and the port is not reopened, so no board is reset. Each such
  recovery is written to the diagnostic log and counted.

  On Linux and macOS, Qt is told about input by a notifier on the
  port's file, which keeps reporting while input is waiting, so no
  input can be left behind there.

Line settings:
  A port is opened with the framing and flow control of its
  ``LineSettings``; DTR and RTS are then set to the states those
  settings give. While the port is open, DTR and RTS can be changed, a
  break can be held, and the input lines can be read.

Ports in use:
  A port that another program holds is not opened alongside it.
  When QSerialPort is refused access, pyserial is not tried, and
  pyserial itself always opens a port with its exclusive lock.

This module does not touch the GUI, the disk or log files.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtSerialPort import QSerialPort, QSerialPortInfo

from app.constants import (
    MS_PER_SECOND,
    NAME_SEPARATOR,
    SERIAL_LISTED_RECHECK_MS,
    SERIAL_POLL_INTERVAL_MS,
    SERIAL_STALL_CHECK_MS,
)
from core.serial_settings import FlowControl, InputLines, LineSettings, Parity, StopBits

logger = logging.getLogger(__name__)

_PYSERIAL_READ_TIMEOUT_SECONDS = 0.1
_PYSERIAL_WRITE_TIMEOUT_SECONDS = 1.0
_PYSERIAL_THREAD_NAME = "wiretrace-pyserial-reader"
_NOT_OPEN = "The port is not open"

#: The QSerialPort errors that can only mean an open port is unusable.
#: Qt documents the permission and not-found errors for opening a port,
#: so on a port already open they mean its handle is gone.
LINK_LOST_ERRORS = frozenset({
    QSerialPort.SerialPortError.ResourceError,
    QSerialPort.SerialPortError.PermissionError,
    QSerialPort.SerialPortError.DeviceNotFoundError,
    QSerialPort.SerialPortError.NotOpenError,
})
#: The QSerialPort errors that a removed device and a working port can
#: both give; the system's list of ports tells them apart. A failed
#: write, a timeout and an unsupported setting leave the port working.
LINK_IN_DOUBT_ERRORS = frozenset({
    QSerialPort.SerialPortError.ReadError,
    QSerialPort.SerialPortError.UnknownError,
})

_QT_DATA_BITS: dict[int, QSerialPort.DataBits] = {
    5: QSerialPort.DataBits.Data5,
    6: QSerialPort.DataBits.Data6,
    7: QSerialPort.DataBits.Data7,
    8: QSerialPort.DataBits.Data8,
}
_QT_PARITY: dict[Parity, QSerialPort.Parity] = {
    Parity.NONE: QSerialPort.Parity.NoParity,
    Parity.EVEN: QSerialPort.Parity.EvenParity,
    Parity.ODD: QSerialPort.Parity.OddParity,
    Parity.MARK: QSerialPort.Parity.MarkParity,
    Parity.SPACE: QSerialPort.Parity.SpaceParity,
}
_QT_STOP_BITS: dict[StopBits, QSerialPort.StopBits] = {
    StopBits.ONE: QSerialPort.StopBits.OneStop,
    StopBits.ONE_AND_HALF: QSerialPort.StopBits.OneAndHalfStop,
    StopBits.TWO: QSerialPort.StopBits.TwoStop,
}
_QT_FLOW_CONTROL: dict[FlowControl, QSerialPort.FlowControl] = {
    FlowControl.NONE: QSerialPort.FlowControl.NoFlowControl,
    FlowControl.HARDWARE: QSerialPort.FlowControl.HardwareControl,
    FlowControl.SOFTWARE: QSerialPort.FlowControl.SoftwareControl,
}
_PYSERIAL_PARITY: dict[Parity, str] = {
    Parity.NONE: "N",
    Parity.EVEN: "E",
    Parity.ODD: "O",
    Parity.MARK: "M",
    Parity.SPACE: "S",
}
_PYSERIAL_STOP_BITS: dict[StopBits, float] = {
    StopBits.ONE: 1,
    StopBits.ONE_AND_HALF: 1.5,
    StopBits.TWO: 2,
}


# ── Port Information ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PortInfo:
    """Immutable descriptor for an available serial port."""
    device: str
    description: str
    manufacturer: str

    @property
    def display_name(self) -> str:
        if self.description:
            return f"{self.device}{NAME_SEPARATOR}{self.description}"
        return self.device


def available_ports() -> list[PortInfo]:
    """Enumerate all available serial ports, sorted by name."""
    ports = [
        PortInfo(
            device=info.portName(),
            description=info.description(),
            manufacturer=info.manufacturer(),
        )
        for info in QSerialPortInfo.availablePorts()
    ]
    ports.sort(key=lambda port: port.device)
    return ports


# ── Transport Interface ──────────────────────────────────────────────────────

class Transport(QObject):
    """A serial connection.

    Signals:
        data_received(bytes):  Bytes read from the port.
        link_lost(str):        The connection ended on its own (device
                               unplugged, adapter reset). The transport
                               has already closed itself.
        error_occurred(str):   An error that did not end the connection.

    ``access_refused`` is True after an ``open`` that failed because the
    system refused access to the port, for example because another
    program holds it.
    """

    data_received = Signal(bytes)
    link_lost = Signal(str)
    error_occurred = Signal(str)

    access_refused = False

    def open(self, port: str, baud: int, line: LineSettings) -> str:
        """Open the port. Returns an empty string, or why it failed."""
        raise NotImplementedError

    def write(self, data: bytes) -> str:
        """Send bytes. Returns an empty string, or why it failed."""
        raise NotImplementedError

    def close(self) -> None:
        """Deliver any bytes already read, then close the port.

        Safe to call when the port is not open.
        """
        raise NotImplementedError

    def set_dtr(self, active: bool) -> str:
        """Set DTR. Returns an empty string, or why it failed."""
        raise NotImplementedError

    def set_rts(self, active: bool) -> str:
        """Set RTS. Returns an empty string, or why it failed."""
        raise NotImplementedError

    def set_break(self, active: bool) -> str:
        """Hold or release a break. Returns an empty string, or why it failed."""
        raise NotImplementedError

    def input_lines(self) -> InputLines:
        """The states of CTS, DSR, DCD and RI, as far as the driver reports them."""
        raise NotImplementedError


class SystemInputQueue:
    """The system's queue of received input of an open Windows port.

    The two calls the stall check needs, on the port's handle:
    ``waiting`` is the number of bytes the system holds that nothing
    has read yet, or None if the system did not say; ``rewait`` ends a
    pending wait for input by setting the port's event mask again to
    the one Qt opened it with, and says whether the system accepted it.
    """

    #: Qt opens a port for reading with this event mask (``EV_RXCHAR``).
    RECEIVED_CHARACTER = 0x0001

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class _Comstat(ctypes.Structure):
            # COMSTAT: a word of status flags, then the input and output
            # queue counts.
            _fields_ = (
                ("flags", wintypes.DWORD),
                ("input_queued", wintypes.DWORD),
                ("output_queued", wintypes.DWORD),
            )

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # Qt gives the handle as a Python integer, which can be negative
        # (an unopened port's is -1); the system takes it as a pointer.
        self._handle_mask = (1 << (8 * ctypes.sizeof(ctypes.c_void_p))) - 1
        self._comstat_type = _Comstat
        self._byref = ctypes.byref
        self._clear_comm_error = kernel32.ClearCommError
        self._clear_comm_error.argtypes = (
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_Comstat),
        )
        self._clear_comm_error.restype = wintypes.BOOL
        self._set_comm_mask = kernel32.SetCommMask
        self._set_comm_mask.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self._set_comm_mask.restype = wintypes.BOOL

    def waiting(self, handle: int) -> int | None:
        status = self._comstat_type()
        if not self._clear_comm_error(handle & self._handle_mask, None, self._byref(status)):
            return None
        return int(status.input_queued)

    def rewait(self, handle: int) -> bool:
        return bool(self._set_comm_mask(handle & self._handle_mask, self.RECEIVED_CHARACTER))


def _phrase(text: str, fallback: str) -> str:
    """A driver's message as a phrase: trimmed, with no final full stop."""
    return text.strip().rstrip(".").rstrip() or fallback


# ── Qt Driver ────────────────────────────────────────────────────────────

class QtSerialTransport(Transport):
    """``Transport`` on Qt's QSerialPort.

    ``system_input`` reads the system's input queue for the stall check
    (see the module's "Reading"); it is given on Windows only, and a
    test may give a stand-in. ``stall_recoveries`` counts the times Qt
    was set reading again since the port was opened.
    """

    def __init__(
        self,
        parent: QObject | None = None,
        system_input: SystemInputQueue | None = None,
    ) -> None:
        super().__init__(parent)
        self._port = QSerialPort(self)
        self._poll_timer = QTimer(self)
        self._poll_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._poll_timer.setInterval(SERIAL_POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._read_available)
        if system_input is None and sys.platform == "win32":
            system_input = SystemInputQueue()
        self._system_input = system_input
        self._stall_timer = QTimer(self)
        self._stall_timer.setInterval(SERIAL_STALL_CHECK_MS)
        self._stall_timer.timeout.connect(self._check_stall)
        # Whether input was waiting in the system at the last check, and
        # whether anything has been collected since.
        self._input_was_waiting = False
        self._collected = False
        self.stall_recoveries = 0
        # Whether the port's signals are connected to this object.
        self._watching = False
        # The errors that left the port working, already reported while
        # it has been open: a driver may repeat one at every poll.
        self._reported: set[tuple[QSerialPort.SerialPortError, str]] = set()
        # When the port list last showed the port, while it has been open.
        self._listed_at = -float("inf")

    def open(self, port: str, baud: int, line: LineSettings) -> str:
        self.access_refused = False
        self._reported.clear()
        self._listed_at = -float("inf")
        self._input_was_waiting = False
        self._collected = False
        self.stall_recoveries = 0
        self._port.setPortName(port)
        self._port.setBaudRate(baud)
        self._port.setDataBits(_QT_DATA_BITS[line.data_bits])
        self._port.setParity(_QT_PARITY[line.parity])
        self._port.setStopBits(_QT_STOP_BITS[line.stop_bits])
        self._port.setFlowControl(_QT_FLOW_CONTROL[line.flow_control])

        if not self._port.open(QSerialPort.OpenModeFlag.ReadWrite):
            self.access_refused = (
                self._port.error() == QSerialPort.SerialPortError.PermissionError
            )
            return self._reason("Unknown error")

        self._port.setDataTerminalReady(line.dtr_on_open)
        if line.rts_is_manual:
            self._port.setRequestToSend(line.rts_on_open)
        self._port.readyRead.connect(self._read_available)
        self._port.errorOccurred.connect(self._on_port_error)
        self._watching = True
        self._poll_timer.start()
        if self._system_input is not None:
            self._stall_timer.start()
        logger.info("Opened %s @ %d baud, %s, via QSerialPort", port, baud, line.describe())
        return ""

    def write(self, data: bytes) -> str:
        if not self._port.isOpen():
            return _NOT_OPEN
        written = self._port.write(data)
        if written != len(data):
            return self._reason("Write failed")
        return ""

    def close(self) -> None:
        self._close(collect_pending=True)

    def set_dtr(self, active: bool) -> str:
        if not self._port.isOpen():
            return _NOT_OPEN
        if self._port.setDataTerminalReady(active):
            return ""
        return self._reason("DTR could not be set")

    def set_rts(self, active: bool) -> str:
        if not self._port.isOpen():
            return _NOT_OPEN
        if self._port.setRequestToSend(active):
            return ""
        return self._reason("RTS could not be set")

    def set_break(self, active: bool) -> str:
        if not self._port.isOpen():
            return _NOT_OPEN
        if self._port.setBreakEnabled(active):
            return ""
        return self._reason("The break could not be sent")

    def input_lines(self) -> InputLines:
        if not self._port.isOpen():
            return InputLines()
        signals = self._port.pinoutSignals()
        pinout = QSerialPort.PinoutSignal
        return InputLines(
            cts=bool(signals & pinout.ClearToSendSignal),
            dsr=bool(signals & pinout.DataSetReadySignal),
            dcd=bool(signals & pinout.DataCarrierDetectSignal),
            ri=bool(signals & pinout.RingIndicatorSignal),
        )

    def _close(self, collect_pending: bool) -> None:
        self._poll_timer.stop()
        self._stall_timer.stop()
        # The port's signals are disconnected first: nothing done while
        # closing may call back into this object. Qt may have closed the
        # port itself after an error, so this does not depend on it being
        # open.
        if self._watching:
            self._watching = False
            self._port.readyRead.disconnect(self._read_available)
            self._port.errorOccurred.disconnect(self._on_port_error)
        if not self._port.isOpen():
            return
        if collect_pending:
            # Bytes the operating system has received but Qt has not yet
            # collected are fetched now; closing would discard them.
            self._port.waitForReadyRead(0)
        self._read_available()
        self._port.close()
        logger.info("Closed %s", self._port.portName())

    @Slot()
    def _read_available(self) -> None:
        if self._port.isOpen() and self._port.bytesAvailable() > 0:
            raw = self._port.readAll()
            if not raw.isEmpty():
                self._collected = True
                self.data_received.emit(bytes(raw.data()))

    @Slot()
    def _check_stall(self) -> None:
        """Set Qt reading again if input has waited in the system for a
        whole check while nothing was collected (the module's "Reading")."""
        if self._system_input is None or not self._port.isOpen():
            return
        collected, self._collected = self._collected, False
        handle = int(self._port.handle())
        waiting = self._system_input.waiting(handle)
        if not waiting:
            self._input_was_waiting = False
            return
        stalled = self._input_was_waiting and not collected
        self._input_was_waiting = not stalled
        if not stalled:
            return
        if self._system_input.rewait(handle):
            self.stall_recoveries += 1
            logger.warning(
                "%s: %d received bytes waited in the system for over %d ms without "
                "Qt collecting them; its wait for input was restarted (%d this session)",
                self._port.portName(), waiting, SERIAL_STALL_CHECK_MS, self.stall_recoveries,
            )
        else:
            logger.error(
                "%s: %d received bytes are waiting in the system and Qt's wait for "
                "input could not be restarted",
                self._port.portName(), waiting,
            )

    @Slot(QSerialPort.SerialPortError)
    def _on_port_error(self, error: QSerialPort.SerialPortError) -> None:
        if error == QSerialPort.SerialPortError.NoError:
            return
        message = self._reason(f"Serial error code: {error.name}")
        if error in LINK_LOST_ERRORS or (
            error in LINK_IN_DOUBT_ERRORS and not self._is_listed()
        ):
            logger.error(
                "Serial error on %s: %s. The port can no longer be used",
                self._port.portName(), message,
            )
            # Closing at once ends Qt's retries, which would report the
            # same error several times a second, and lets the system
            # release the device node, so a returning device takes its
            # old name rather than a new one. There is nothing more to
            # collect from the device; what Qt already holds is delivered.
            self._close(collect_pending=False)
            self.link_lost.emit(message)
        elif (error, message) not in self._reported:
            self._reported.add((error, message))
            logger.error("Serial error on %s: %s", self._port.portName(), message)
            self.error_occurred.emit(message)

    def _reason(self, fallback: str) -> str:
        return _phrase(self._port.errorString(), fallback)

    def _is_listed(self) -> bool:
        """Whether the system still lists this port; read again at most
        once every ``SERIAL_LISTED_RECHECK_MS``."""
        now = time.monotonic()
        if (now - self._listed_at) * MS_PER_SECOND < SERIAL_LISTED_RECHECK_MS:
            return True
        name = self._port.portName()
        if any(info.portName() == name for info in QSerialPortInfo.availablePorts()):
            self._listed_at = now
            return True
        return False


# ── pyserial Driver ──────────────────────────────────────────────────────────

class PyserialTransport(Transport):
    """``Transport`` on pyserial, read by a thread of its own.

    The read thread never emits ``data_received`` itself. It places the
    bytes in a queue and wakes the owning thread, which emits them. That
    keeps every emission on one thread and lets ``close`` deliver what
    was read up to the last moment.
    """

    _bytes_queued = Signal()
    _read_failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._serial = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._received: queue.SimpleQueue[bytes] = queue.SimpleQueue()
        self._bytes_queued.connect(self._deliver_received)
        self._read_failed.connect(self._on_read_failed)

    def open(self, port: str, baud: int, line: LineSettings) -> str:
        try:
            import serial
        except ImportError:
            return "pyserial is not installed"

        # pyserial needs the operating system's own name for the port
        # (for example /dev/ttyUSB0), not the short name Qt lists.
        location = QSerialPortInfo(port).systemLocation() or port
        try:
            self._serial = serial.Serial(
                port=location,
                baudrate=baud,
                bytesize=line.data_bits,
                parity=_PYSERIAL_PARITY[line.parity],
                stopbits=_PYSERIAL_STOP_BITS[line.stop_bits],
                rtscts=line.flow_control is FlowControl.HARDWARE,
                xonxoff=line.flow_control is FlowControl.SOFTWARE,
                timeout=_PYSERIAL_READ_TIMEOUT_SECONDS,
                write_timeout=_PYSERIAL_WRITE_TIMEOUT_SECONDS,
                exclusive=True,
            )
            self._serial.dtr = line.dtr_on_open
            if line.rts_is_manual:
                self._serial.rts = line.rts_on_open
        except (OSError, ValueError) as exc:
            # A port opened before the control lines failed is closed again.
            if self._serial is not None:
                with contextlib.suppress(OSError, ValueError):
                    self._serial.close()
            self._serial = None
            return _phrase(str(exc), type(exc).__name__)

        self._running.set()
        self._thread = threading.Thread(
            target=self._read_loop, name=_PYSERIAL_THREAD_NAME, daemon=True,
        )
        self._thread.start()
        logger.info("Opened %s @ %d baud, %s, via pyserial", location, baud, line.describe())
        return ""

    def write(self, data: bytes) -> str:
        if self._serial is None or not self._serial.is_open:
            return _NOT_OPEN
        try:
            self._serial.write(data)
        except (OSError, ValueError) as exc:
            return _phrase(str(exc), type(exc).__name__)
        return ""

    def close(self) -> None:
        self._running.clear()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        self._deliver_received()
        if self._serial is not None:
            serial_port, self._serial = self._serial, None
            try:
                serial_port.close()
            except OSError as exc:
                logger.warning("Error closing the port: %s", exc)

    def set_dtr(self, active: bool) -> str:
        return self._set_line("dtr", active)

    def set_rts(self, active: bool) -> str:
        return self._set_line("rts", active)

    def set_break(self, active: bool) -> str:
        return self._set_line("break_condition", active)

    def input_lines(self) -> InputLines:
        serial_port = self._serial
        if serial_port is None or not serial_port.is_open:
            return InputLines()
        return InputLines(
            cts=self._read_line(serial_port, "cts"),
            dsr=self._read_line(serial_port, "dsr"),
            dcd=self._read_line(serial_port, "cd"),
            ri=self._read_line(serial_port, "ri"),
        )

    def _set_line(self, name: str, active: bool) -> str:
        if self._serial is None or not self._serial.is_open:
            return _NOT_OPEN
        try:
            setattr(self._serial, name, active)
        except (OSError, ValueError) as exc:
            return _phrase(str(exc), type(exc).__name__)
        return ""

    @staticmethod
    def _read_line(serial_port: object, name: str) -> bool | None:
        try:
            return bool(getattr(serial_port, name))
        except (OSError, ValueError):
            return None

    def _read_loop(self) -> None:
        """Blocking reads with a short timeout, until closed or the link fails."""
        serial_port = self._serial
        while self._running.is_set():
            try:
                data = serial_port.read(max(1, serial_port.in_waiting))
            except (OSError, ValueError) as exc:
                if self._running.is_set():
                    self._read_failed.emit(_phrase(str(exc), type(exc).__name__))
                return
            if data:
                self._received.put(data)
                self._bytes_queued.emit()

    @Slot()
    def _deliver_received(self) -> None:
        while True:
            try:
                data = self._received.get_nowait()
            except queue.Empty:
                return
            self.data_received.emit(data)

    @Slot(str)
    def _on_read_failed(self, message: str) -> None:
        if self._serial is None:
            return  # closed by the user before this report arrived
        self.close()
        self.link_lost.emit(message)


# ── Driver Selection ─────────────────────────────────────────────────────────

def open_serial_transport(
    port: str,
    baud: int,
    line: LineSettings | None = None,
    driver: type[Transport] | None = None,
) -> tuple[Transport | None, str]:
    """Open ``port`` with the first driver that can, or with ``driver`` only.

    Must be called on the thread that will own the transport. A port to
    which access is refused is not tried with the second driver. A board
    found again is opened with the driver its session was using: the
    port may be listed a moment before it can be opened, and a failure
    then is retried, not taken as a reason to change driver.

    Returns:
        ``(transport, "")`` on success, ``(None, reason)`` on failure.
    """
    line = line or LineSettings()
    if driver is not None:
        chosen = driver()
        reason = chosen.open(port, baud, line)
        if not reason:
            return chosen, ""
        chosen.deleteLater()
        return None, reason
    qt_transport = QtSerialTransport()
    qt_error = qt_transport.open(port, baud, line)
    if not qt_error:
        return qt_transport, ""
    refused = qt_transport.access_refused
    qt_transport.deleteLater()
    if refused:
        logger.warning("Access to %s refused: %s", port, qt_error)
        return None, qt_error
    logger.warning("QSerialPort failed on %s (%s). Trying pyserial", port, qt_error)

    fallback = PyserialTransport()
    if not fallback.open(port, baud, line):
        return fallback, ""
    fallback.deleteLater()
    return None, qt_error
