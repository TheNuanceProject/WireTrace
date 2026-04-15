# SPDX-License-Identifier: MIT
"""WireTrace serial port connection manager.

Responsibilities (spec section 3.4):
  - Open / close a single serial port via QSerialPort
  - Enumerate available serial ports with metadata
  - Write bytes to an open port
  - Emit signals for connect, disconnect, and error events
  - Relay incoming bytes to subscribers (thread-safe)

Threading model:
  QSerialPort lives in the main thread. Data is read via a 20ms QTimer poll
  (checking bytesAvailable) PLUS the readyRead signal as backup. This dual
  approach handles the known Qt/Windows bug where readyRead silently fails
  with certain USB-to-serial chipsets (CH340, CP2102, FTDI, PL2303).

DTR/RTS policy:
  After opening, DTR and RTS are set HIGH (standard terminal behavior).
  This allows devices to transmit data normally.

Primary driver: QSerialPort (spec decision #4).
Fallback driver: pyserial (for platforms where QSerialPort cannot open).

This module does NOT touch: GUI, disk I/O, or log files.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtSerialPort import QSerialPort, QSerialPortInfo

logger = logging.getLogger(__name__)

# Poll interval for reading serial data (milliseconds).
# 20ms = 50 polls/sec — fast enough for 10K+ lines/sec, low CPU.
_POLL_INTERVAL_MS = 20


# ── Port Information ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PortInfo:
    """Immutable descriptor for an available serial port."""
    device: str
    description: str
    manufacturer: str
    serial_number: str
    vid: int
    pid: int

    @property
    def display_name(self) -> str:
        if self.description:
            return f"{self.device} — {self.description}"
        return self.device


# ── Serial Manager ───────────────────────────────────────────────────────────

class SerialManager(QObject):
    """Manages a single QSerialPort connection for one device tab.

    Data reading strategy:
      Uses a fast QTimer poll (20ms) that checks bytesAvailable() and reads.
      The readyRead signal is ALSO connected as a secondary trigger.
      This dual approach guarantees data delivery even when Qt's readyRead
      signal fails (a known issue on Windows with many USB serial adapters).

    Signals:
        connected(str):       Emitted after successful port open.
        disconnected(str):    Emitted after port close.
        error_occurred(str):  Emitted on any serial error.
        data_received(bytes): Raw bytes read from port.
    """

    connected = Signal(str)
    disconnected = Signal(str)
    error_occurred = Signal(str)
    data_received = Signal(bytes)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._port = QSerialPort(self)
        self._port_name = ""
        self._baud_rate = 0
        self._using_fallback = False
        self._fallback_serial = None
        self._fallback_thread = None

        # Polling timer for reliable data reading
        self._poll_timer = QTimer(self)
        self._poll_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._poll_timer.timeout.connect(self._poll_read)

        # Connect QSerialPort error signal
        self._port.errorOccurred.connect(self._on_port_error)

    # ── Public API ───────────────────────────────────────────────────────

    def open(self, port: str, baud: int) -> bool:
        """Open a serial port at the given baud rate.

        Primary: QSerialPort with timer-based polling + readyRead.
        Fallback: pyserial if QSerialPort fails (spec decision #4).
        """
        if self._port.isOpen() or self._using_fallback:
            logger.warning("Port already open (%s) — closing before re-open",
                           self._port_name)
            self.close()

        # ── Primary: QSerialPort ─────────────────────────────────────
        self._port.setPortName(port)
        self._port.setBaudRate(baud)
        self._port.setDataBits(QSerialPort.DataBits.Data8)
        self._port.setParity(QSerialPort.Parity.NoParity)
        self._port.setStopBits(QSerialPort.StopBits.OneStop)
        self._port.setFlowControl(QSerialPort.FlowControl.NoFlowControl)

        if self._port.open(QSerialPort.OpenModeFlag.ReadWrite):
            # DTR/RTS HIGH — standard terminal behavior
            self._port.setDataTerminalReady(True)
            self._port.setRequestToSend(True)

            # Dual data reading: poll timer (primary) + readyRead (backup)
            self._port.readyRead.connect(self._on_ready_read)
            self._poll_timer.start(_POLL_INTERVAL_MS)

            self._port_name = port
            self._baud_rate = baud
            self._using_fallback = False
            logger.info("Opened %s @ %d baud via QSerialPort (poll=%dms)",
                        port, baud, _POLL_INTERVAL_MS)
            self.connected.emit(port)
            return True

        # ── Fallback: pyserial ───────────────────────────────────────
        qsp_error = self._port.errorString() or "Unknown error"
        logger.warning("QSerialPort failed on %s: %s — trying pyserial fallback",
                       port, qsp_error)

        if self._try_pyserial_fallback(port, baud):
            return True

        logger.error("Failed to open %s @ %d: QSerialPort (%s), pyserial also failed",
                     port, baud, qsp_error)
        self.error_occurred.emit(f"Failed to open {port}: {qsp_error}")
        return False

    def close(self) -> None:
        """Close the serial port. Safe to call even if already closed."""
        port_name = self._port_name

        # Stop polling
        self._poll_timer.stop()

        if self._using_fallback:
            self._close_fallback()
        elif self._port.isOpen():
            with contextlib.suppress(RuntimeError, TypeError):
                self._port.readyRead.disconnect(self._on_ready_read)
            self._port.close()

        self._port_name = ""
        self._baud_rate = 0
        self._using_fallback = False

        if port_name:
            logger.info("Closed %s", port_name)
            self.disconnected.emit(port_name)

    def write(self, data: bytes) -> bool:
        """Write raw bytes to the open serial port."""
        if self._using_fallback:
            return self._write_fallback(data)

        if not self._port.isOpen():
            self.error_occurred.emit("Cannot write: port is not open")
            return False

        written = self._port.write(data)
        if written == -1:
            error_msg = self._port.errorString() or "Write failed"
            self.error_occurred.emit(f"Write error: {error_msg}")
            return False

        return written == len(data)

    def is_open(self) -> bool:
        """Check if the port is currently open."""
        if self._using_fallback:
            return (self._fallback_serial is not None and
                    self._fallback_serial.is_open)
        return self._port.isOpen()

    @staticmethod
    def available_ports() -> list[PortInfo]:
        """Enumerate all available serial ports."""
        ports = []
        for info in QSerialPortInfo.availablePorts():
            ports.append(PortInfo(
                device=info.portName(),
                description=info.description(),
                manufacturer=info.manufacturer(),
                serial_number=info.serialNumber(),
                vid=info.vendorIdentifier(),
                pid=info.productIdentifier(),
            ))
        ports.sort(key=lambda p: p.device)
        return ports

    # ── Data Reading ─────────────────────────────────────────────────────

    @Slot()
    def _poll_read(self) -> None:
        """Timer-based poll: check for available bytes and read them.

        This is the PRIMARY data reading mechanism. It runs every 20ms
        and handles the known Windows bug where readyRead never fires.
        """
        if not self._port.isOpen():
            return

        avail = self._port.bytesAvailable()
        if avail > 0:
            raw = self._port.readAll()
            if not raw.isEmpty():
                self.data_received.emit(bytes(raw.data()))

    @Slot()
    def _on_ready_read(self) -> None:
        """readyRead signal handler (secondary/backup).

        On platforms where readyRead works, this provides immediate
        response between poll intervals. If readyRead never fires
        (Windows bug), the poll timer handles everything.
        """
        if not self._port.isOpen():
            return

        avail = self._port.bytesAvailable()
        if avail > 0:
            raw = self._port.readAll()
            if not raw.isEmpty():
                self.data_received.emit(bytes(raw.data()))

    # ── QSerialPort Error Handler ────────────────────────────────────────

    def _on_port_error(self, error: QSerialPort.SerialPortError) -> None:
        if error == QSerialPort.SerialPortError.NoError:
            return
        error_msg = self._port.errorString() or f"Serial error code: {error}"
        logger.error("Serial error on %s: %s", self._port_name, error_msg)
        self.error_occurred.emit(error_msg)

    # ── pyserial Fallback ────────────────────────────────────────────────

    def _try_pyserial_fallback(self, port: str, baud: int) -> bool:
        """Open with pyserial when QSerialPort fails (spec decision #4)."""
        try:
            import serial
        except ImportError:
            logger.error("pyserial not installed — fallback unavailable")
            return False

        try:
            self._fallback_serial = serial.Serial(
                port=port,
                baudrate=baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.1,
                write_timeout=1.0,
            )
            self._fallback_serial.dtr = True
            self._fallback_serial.rts = True

            self._start_fallback_reader()

            self._port_name = port
            self._baud_rate = baud
            self._using_fallback = True
            logger.info("Opened %s @ %d baud via pyserial fallback", port, baud)
            self.connected.emit(port)
            return True

        except Exception as e:
            logger.error("pyserial fallback failed on %s: %s", port, e)
            self._fallback_serial = None
            return False

    def _start_fallback_reader(self) -> None:
        """Background thread for pyserial blocking reads."""
        import threading

        self._fallback_running = True

        def _read_loop():
            while self._fallback_running:
                try:
                    ser = self._fallback_serial
                    if ser is None or not ser.is_open:
                        break
                    waiting = ser.in_waiting
                    data = ser.read(waiting) if waiting > 0 else ser.read(1)
                    if data:
                        if ser.in_waiting > 0:
                            data += ser.read(ser.in_waiting)
                        self.data_received.emit(data)
                except Exception as e:
                    if self._fallback_running:
                        self.error_occurred.emit(str(e))
                    break

        self._fallback_thread = threading.Thread(
            target=_read_loop, daemon=True, name="pyserial-fallback"
        )
        self._fallback_thread.start()

    def _close_fallback(self) -> None:
        self._fallback_running = False
        if self._fallback_thread:
            self._fallback_thread.join(timeout=2.0)
            self._fallback_thread = None
        if self._fallback_serial:
            try:
                self._fallback_serial.close()
            except Exception as e:
                logger.warning("Error closing fallback serial: %s", e)
            self._fallback_serial = None

    def _write_fallback(self, data: bytes) -> bool:
        if not self._fallback_serial or not self._fallback_serial.is_open:
            self.error_occurred.emit("Cannot write: fallback port is not open")
            return False
        try:
            self._fallback_serial.write(data)
            return True
        except Exception as e:
            self.error_occurred.emit(f"Write error: {e}")
            return False
