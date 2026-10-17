# SPDX-License-Identifier: GPL-3.0-or-later
"""Serial port access behind one interface.

``Transport`` is what the reader talks to: open, write, close, and a
signal for received bytes. Two drivers implement it:

  QtSerialTransport   Qt's QSerialPort. The primary driver.
  PyserialTransport   pyserial. Used when QSerialPort cannot open a
                      port, which happens with a few USB-CDC chipsets.

``open_serial_transport`` tries them in that order.

Threading:
  A transport belongs to the thread that creates it, which is the
  reader thread. Nothing here runs on the GUI thread.

Reading (QtSerialTransport):
  A timer polls the port for available bytes, and Qt's readyRead signal
  is connected as well. The poll exists because readyRead silently
  fails on Windows with several common USB-to-serial chipsets (CH340,
  CP2102, FTDI, PL2303); the signal gives a faster response where it
  does work.

DTR/RTS policy:
  After opening, DTR and RTS are asserted (standard terminal
  behaviour), which lets devices transmit normally.

This module does not touch the GUI, the disk or log files.
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtSerialPort import QSerialPort, QSerialPortInfo

from app.constants import NAME_SEPARATOR, SERIAL_POLL_INTERVAL_MS

logger = logging.getLogger(__name__)

_PYSERIAL_READ_TIMEOUT_SECONDS = 0.1
_PYSERIAL_WRITE_TIMEOUT_SECONDS = 1.0
_PYSERIAL_THREAD_NAME = "wiretrace-pyserial-reader"


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
    """

    data_received = Signal(bytes)
    link_lost = Signal(str)
    error_occurred = Signal(str)

    def open(self, port: str, baud: int) -> str:
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


# ── Qt Driver ────────────────────────────────────────────────────────────

class QtSerialTransport(Transport):
    """``Transport`` on Qt's QSerialPort."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._port = QSerialPort(self)
        self._poll_timer = QTimer(self)
        self._poll_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._poll_timer.setInterval(SERIAL_POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._read_available)

    def open(self, port: str, baud: int) -> str:
        self._port.setPortName(port)
        self._port.setBaudRate(baud)
        self._port.setDataBits(QSerialPort.DataBits.Data8)
        self._port.setParity(QSerialPort.Parity.NoParity)
        self._port.setStopBits(QSerialPort.StopBits.OneStop)
        self._port.setFlowControl(QSerialPort.FlowControl.NoFlowControl)

        if not self._port.open(QSerialPort.OpenModeFlag.ReadWrite):
            return self._port.errorString() or "Unknown error"

        self._port.setDataTerminalReady(True)
        self._port.setRequestToSend(True)
        self._port.readyRead.connect(self._read_available)
        self._port.errorOccurred.connect(self._on_port_error)
        self._poll_timer.start()
        logger.info("Opened %s @ %d baud via QSerialPort", port, baud)
        return ""

    def write(self, data: bytes) -> str:
        if not self._port.isOpen():
            return "The port is not open"
        written = self._port.write(data)
        if written != len(data):
            return self._port.errorString() or "Write failed"
        return ""

    def close(self) -> None:
        self._close(collect_pending=True)

    def _close(self, collect_pending: bool) -> None:
        self._poll_timer.stop()
        if not self._port.isOpen():
            return
        # The port's signals are disconnected first: nothing done while
        # closing may call back into this object.
        self._port.readyRead.disconnect(self._read_available)
        self._port.errorOccurred.disconnect(self._on_port_error)
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
                self.data_received.emit(bytes(raw.data()))

    @Slot(QSerialPort.SerialPortError)
    def _on_port_error(self, error: QSerialPort.SerialPortError) -> None:
        if error == QSerialPort.SerialPortError.NoError:
            return
        message = self._port.errorString() or f"Serial error code: {error}"
        logger.error("Serial error on %s: %s", self._port.portName(), message)

        # A ResourceError means the device became unavailable (USB cable
        # pulled, adapter reset, remote close). Closing the port at once
        # lets the kernel release the tty node; otherwise the node stays
        # locked and the device re-enumerates under a new path (for
        # example /dev/ttyACM1 instead of /dev/ttyACM0) when it returns.
        if error == QSerialPort.SerialPortError.ResourceError:
            # The device is gone, so there is nothing more to collect
            # from it; what Qt already holds is still delivered.
            self._close(collect_pending=False)
            self.link_lost.emit(message)
        else:
            self.error_occurred.emit(message)


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

    def open(self, port: str, baud: int) -> str:
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
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=_PYSERIAL_READ_TIMEOUT_SECONDS,
                write_timeout=_PYSERIAL_WRITE_TIMEOUT_SECONDS,
            )
            self._serial.dtr = True
            self._serial.rts = True
        except (OSError, ValueError) as exc:
            self._serial = None
            return str(exc)

        self._running.set()
        self._thread = threading.Thread(
            target=self._read_loop, name=_PYSERIAL_THREAD_NAME, daemon=True,
        )
        self._thread.start()
        logger.info("Opened %s @ %d baud via pyserial", location, baud)
        return ""

    def write(self, data: bytes) -> str:
        if self._serial is None or not self._serial.is_open:
            return "The port is not open"
        try:
            self._serial.write(data)
        except (OSError, ValueError) as exc:
            return str(exc)
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

    def _read_loop(self) -> None:
        """Blocking reads with a short timeout, until closed or the link fails."""
        serial_port = self._serial
        while self._running.is_set():
            try:
                data = serial_port.read(max(1, serial_port.in_waiting))
            except (OSError, ValueError) as exc:
                if self._running.is_set():
                    self._read_failed.emit(str(exc))
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

def open_serial_transport(port: str, baud: int) -> tuple[Transport | None, str]:
    """Open ``port`` with the first driver that can.

    Must be called on the thread that will own the transport.

    Returns:
        ``(transport, "")`` on success, ``(None, reason)`` on failure.
    """
    qt_transport = QtSerialTransport()
    qt_error = qt_transport.open(port, baud)
    if not qt_error:
        return qt_transport, ""
    qt_transport.deleteLater()
    logger.warning("QSerialPort failed on %s (%s). Trying pyserial", port, qt_error)

    fallback = PyserialTransport()
    if not fallback.open(port, baud):
        return fallback, ""
    fallback.deleteLater()
    return None, qt_error
