# SPDX-License-Identifier: GPL-3.0-or-later
"""A sample device, built in, for the tour.

The tour shows every tool on a real session, and a session needs lines.
``SampleTransport`` stands in for a serial port and sends lines like
those of an ESP32 board running ESP-IDF: its boot line (so a restart is
recognised), lines at every level, and readings of three values to plot.
The lines go through the same reading path as a real port's, so what
the tour shows is what WireTrace does.

The lines are the same every time: the readings follow fixed curves, so
nothing about them is random. No device and no port is involved.

This module does not touch the GUI, the disk or any port.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QObject, QTimer

from app.constants import SAMPLE_LINE_INTERVAL_MS, SAMPLE_PORT_NAME
from core.serial_settings import InputLines, LineSettings
from core.transport import Transport

#: What the sample board prints as it starts: the boot ROM's reset line,
#: then two of its own.
_START = (
    "rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)",
    "I (312) boot: sample board, firmware 1.4.0",
    "I (845) wifi: connected, signal -58 dBm",
)
#: Milliseconds since the start at the first reading, as ESP-IDF counts them.
_FIRST_READING_MS = 1000

#: Lines the board prints now and then, every so many readings.
_EVERY = (
    (15, "I ({ms}) mqtt: published 3 values"),
    (25, "W ({ms}) sensor: humidity reading slow, retrying"),
    (40, "D ({ms}) loop: cycle {step} took 4 ms"),
    (60, "E ({ms}) sensor: read failed, using the last value"),
)
_REPLY = "I ({ms}) console: command received"
_LINE_END = b"\r\n"


def sample_lines(step: int) -> list[str]:
    """The lines the sample board prints at reading ``step`` (from 0)."""
    ms = _FIRST_READING_MS + step * SAMPLE_LINE_INTERVAL_MS
    temperature = 24 + 1.5 * math.sin(step / 12)
    humidity = 52 + 4 * math.sin(step / 20 + 1)
    current = 0.8 + 0.25 * math.sin(step / 7)
    lines = [f"temp={temperature:.1f} humidity={humidity:.1f} current={current:.2f}"]
    for every, text in _EVERY:
        if step and step % every == 0:
            lines.append(text.format(ms=ms, step=step))
    return lines


class SampleTransport(Transport):
    """A port that the sample board sends to. Owned by the thread that
    opened it, as every transport is."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._timer: QTimer | None = None
        self._step = 0

    def open(self, port: str, baud: int, line: LineSettings) -> str:
        self._timer = QTimer(self)
        self._timer.setInterval(SAMPLE_LINE_INTERVAL_MS)
        self._timer.timeout.connect(self._send_reading)
        self._step = 0
        # The reader connects to the transport once it is open, so the
        # board starts on the next turn of the event loop.
        QTimer.singleShot(0, self._start)
        return ""

    def write(self, data: bytes) -> str:
        if self._timer is None:
            return "the sample session is closed"
        self._send([_REPLY.format(ms=_FIRST_READING_MS + self._step * SAMPLE_LINE_INTERVAL_MS)])
        return ""

    def close(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer.deleteLater()
            self._timer = None

    def set_dtr(self, active: bool) -> str:
        return ""

    def set_rts(self, active: bool) -> str:
        return ""

    def set_break(self, active: bool) -> str:
        return ""

    def input_lines(self) -> InputLines:
        return InputLines(cts=False, dsr=False, dcd=False, ri=False)

    def _start(self) -> None:
        if self._timer is not None:
            self._send(_START)
            self._timer.start()

    def _send_reading(self) -> None:
        self._send(sample_lines(self._step))
        self._step += 1

    def _send(self, lines: list[str] | tuple[str, ...]) -> None:
        self.data_received.emit(b"".join(line.encode("ascii") + _LINE_END for line in lines))


def open_sample_transport(
    port: str,
    baud: int,
    line: LineSettings | None = None,
    driver: type[Transport] | None = None,
) -> tuple[Transport | None, str]:
    """Open the sample board, as ``open_serial_transport`` opens a port.

    Only the sample port is opened; any other name is refused, so a
    sample tab can never open a real port.
    """
    if port != SAMPLE_PORT_NAME:
        return None, f"{port} is not the sample board"
    transport = SampleTransport()
    transport.open(port, baud, line or LineSettings())
    return transport, ""
