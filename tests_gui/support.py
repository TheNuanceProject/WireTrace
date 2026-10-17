# SPDX-License-Identifier: GPL-3.0-or-later
"""Test doubles and helpers shared by the real-Qt suite."""

from __future__ import annotations

import queue
import time
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QWidget

from core.transport import Transport

DEFAULT_WAIT_SECONDS = 15.0
_POLL_SECONDS = 0.002


def wait_until(
    qapp: QApplication,
    condition: Callable[[], bool],
    timeout: float = DEFAULT_WAIT_SECONDS,
) -> None:
    """Run the event loop until ``condition`` holds, or fail after ``timeout``."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition was not met in time")
        qapp.processEvents()
        time.sleep(_POLL_SECONDS)


def pixel(widget: QWidget, x: float, y: float) -> QColor:
    """Colour drawn at a point of a widget, given in the widget's own units.

    A grabbed image is in device pixels, which are not the widget's
    units when the display is scaled.
    """
    image = widget.grab().toImage()
    ratio = image.devicePixelRatio()
    return image.pixelColor(int(x * ratio), int(y * ratio))


def log_body(path: str) -> list[str]:
    """The lines of a .txt log after its header block."""
    lines = Path(path).read_text(encoding="utf-8").split("\n")
    rules = [index for index, line in enumerate(lines) if line.startswith("=" * 80)]
    return [line for line in lines[rules[2] + 2:] if line != ""]


def line_text(log_line: str) -> str:
    """The text of a log line, without its ``[timestamp]`` prefix."""
    return log_line.split("] ", 1)[1]


class LoopbackTransport(Transport):
    """An in-process serial port.

    ``feed`` plays the device: the bytes arrive as if read from a port.
    Like a real driver, ``close`` first delivers everything already
    received. ``written`` collects what the application sent.
    """

    _bytes_queued = Signal()
    _drop_requested = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.written: list[bytes] = []
        self.write_error = ""
        self.is_open = False
        self._received: queue.SimpleQueue[bytes] = queue.SimpleQueue()
        self._bytes_queued.connect(self._deliver_received)
        self._drop_requested.connect(self._on_drop_requested)

    def open(self, port: str, baud: int) -> str:
        self.is_open = True
        return ""

    def write(self, data: bytes) -> str:
        if self.write_error:
            return self.write_error
        self.written.append(data)
        return ""

    def close(self) -> None:
        self._deliver_received()
        self.is_open = False

    def feed(self, data: bytes) -> None:
        """Make ``data`` arrive from the device. Callable from any thread."""
        self._received.put(data)
        self._bytes_queued.emit()

    def drop(self, message: str = "device removed") -> None:
        """End the connection as an unplugged device would."""
        self._drop_requested.emit(message)

    @Slot()
    def _deliver_received(self) -> None:
        while self.is_open:
            try:
                data = self._received.get_nowait()
            except queue.Empty:
                return
            self.data_received.emit(data)

    @Slot(str)
    def _on_drop_requested(self, message: str) -> None:
        if self.is_open:
            self.close()
            self.link_lost.emit(message)


class LoopbackPort:
    """Hands a ``LoopbackTransport`` to a pipeline and keeps hold of it."""

    def __init__(self) -> None:
        self.transport: LoopbackTransport | None = None
        self.refuse_with = ""

    def factory(self, port: str, baud: int) -> tuple[Transport | None, str]:
        if self.refuse_with:
            return None, self.refuse_with
        self.transport = LoopbackTransport()
        self.transport.open(port, baud)
        return self.transport, ""

    def feed(self, data: bytes) -> None:
        assert self.transport is not None
        self.transport.feed(data)
