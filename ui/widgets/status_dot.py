# SPDX-License-Identifier: GPL-3.0-or-later
"""The small dot that shows the state of a connection.

An outline while nothing is connected, filled green while connected,
filled red while logging. Used in the status bar and on each tab.
"""

from __future__ import annotations

import enum

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPaintEvent, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from ui.themes import tokens


class LinkState(enum.Enum):
    """What a tab's connection is doing."""

    IDLE = "idle"
    CONNECTED = "connected"
    LOGGING = "logging"


def _paint_dot(painter: QPainter, area: QRectF, state: LinkState) -> None:
    palette = tokens.palette()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    if state is LinkState.IDLE:
        pen = QPen(QColor(palette.edge))
        pen.setWidthF(tokens.METRICS.border)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        inset = pen.widthF() / 2
        painter.drawEllipse(area.adjusted(inset, inset, -inset, -inset))
        return
    role = palette.logging if state is LinkState.LOGGING else palette.connected
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(role))
    painter.drawEllipse(area)


def state_icon(state: LinkState, pixel_ratio: float) -> QIcon:
    """The dot as an icon, for a tab."""
    size = tokens.METRICS.status_dot
    pixmap = QPixmap(round(size * pixel_ratio), round(size * pixel_ratio))
    pixmap.setDevicePixelRatio(pixel_ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    _paint_dot(painter, QRectF(0, 0, size, size), state)
    painter.end()
    return QIcon(pixmap)


class StatusDot(QWidget):
    """The dot as a widget."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = LinkState.IDLE
        self.setFixedSize(self.sizeHint())

    def sizeHint(self) -> QSize:
        size = tokens.METRICS.status_dot
        return QSize(size, size)

    def set_state(self, state: LinkState) -> None:
        if state is not self._state:
            self._state = state
            self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        _paint_dot(painter, QRectF(self.rect()), self._state)
        painter.end()
