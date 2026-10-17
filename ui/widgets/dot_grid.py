# SPDX-License-Identifier: GPL-3.0-or-later
"""The dot grid of thenuanceproject.com, as a background for calm areas.

Used behind the first screen, the splash screen, the About window and
the body of dialogs. Never behind the console, a plot, a toolbar, a list
or a text field: those keep a plain background so nothing competes with
their content.

The grid is anchored to the top-left corner of the window, so two
neighbouring areas that both draw it line up.

On the first screen, the splash and About, the dots near the pointer
light up in the accent colour, as on the site. Nothing else is animated.
"""

from __future__ import annotations

import math
from itertools import pairwise

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPixmap
from PySide6.QtWidgets import QDialog, QWidget

from ui.themes import tokens

#: One tile per (colour, opacity, device pixel ratio).
_tiles: dict[tuple[str, float, float], QPixmap] = {}


def _tile(colour: str, opacity: float, pixel_ratio: float) -> QPixmap:
    key = (colour, opacity, pixel_ratio)
    cached = _tiles.get(key)
    if cached is not None:
        return cached

    pitch = tokens.METRICS.grid_pitch
    radius = tokens.METRICS.grid_dot_radius
    side = max(1, round(pitch * pixel_ratio))
    tile = QPixmap(side, side)
    tile.setDevicePixelRatio(side / pitch)
    tile.fill(Qt.GlobalColor.transparent)

    dot = QColor(colour)
    dot.setAlphaF(opacity)
    painter = QPainter(tile)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(dot)
    centre = pitch / 2
    painter.drawEllipse(QPointF(centre, centre), radius, radius)
    painter.end()

    _tiles[key] = tile
    return tile


def glow_strength(distance: float) -> float:
    """How brightly a dot ``distance`` pixels from the pointer is lit (0 to 1)."""
    share = distance / tokens.METRICS.grid_glow_radius
    for (near, near_strength), (far, far_strength) in pairwise(tokens.GLOW_STOPS):
        if share <= far:
            position = (share - near) / (far - near)
            return near_strength + (far_strength - near_strength) * position
    return 0.0


def paint_dot_grid(
    widget: QWidget, area: QRect, *, fill: bool = False, pointer: QPoint | None = None,
) -> None:
    """Draw the dot grid over ``area`` of ``widget``, in the active theme.

    With ``fill`` the area is first painted in the paper colour; use it
    for a widget whose background nothing else draws. With ``pointer``,
    a position in the widget, the dots near it are lit.
    """
    palette = tokens.palette()
    tile = _tile(palette.dot.colour, palette.dot.opacity, widget.devicePixelRatioF())
    origin = widget.mapTo(widget.window(), QPoint(0, 0))
    painter = QPainter(widget)
    if fill:
        painter.fillRect(area, QColor(palette.paper))
    painter.drawTiledPixmap(area, tile, area.topLeft() + origin)
    if pointer is not None:
        _paint_glow(painter, area, origin, pointer)
    painter.end()


def _paint_glow(painter: QPainter, area: QRect, origin: QPoint, pointer: QPoint) -> None:
    palette = tokens.palette()
    metrics = tokens.METRICS
    pitch, reach = metrics.grid_pitch, metrics.grid_glow_radius
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)

    # Dots sit at the centre of each tile, counted from the window's corner.
    def first_dot(low: int, offset: int) -> int:
        return ((low + offset - pitch // 2) // pitch) * pitch + pitch // 2 - offset

    left = max(area.left(), pointer.x() - reach)
    right = min(area.right(), pointer.x() + reach)
    top = max(area.top(), pointer.y() - reach)
    bottom = min(area.bottom(), pointer.y() + reach)
    y = first_dot(top, origin.y())
    while y <= bottom + pitch:
        x = first_dot(left, origin.x())
        while x <= right + pitch:
            strength = glow_strength(math.hypot(x - pointer.x(), y - pointer.y()))
            if strength > 0:
                colour = QColor(palette.dot_glow.colour)
                colour.setAlphaF(palette.dot_glow.opacity * strength)
                painter.setBrush(colour)
                painter.drawEllipse(QPointF(x, y), metrics.grid_dot_radius, metrics.grid_dot_radius)
            x += pitch
        y += pitch


class _PointerGlow:
    """Tracks the pointer over a widget and repaints the dots it lights.

    Hover events reach a widget while the pointer is over one of its
    children too, so the dots around a button light up as well.
    """

    def __init__(self, widget: QWidget) -> None:
        self._widget = widget
        self.pointer: QPoint | None = None
        widget.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def handle(self, event: QEvent) -> None:
        kind = event.type()
        if kind in (QEvent.Type.HoverMove, QEvent.Type.HoverEnter):
            self._move(event.position().toPoint())
        elif kind == QEvent.Type.HoverLeave:
            self._move(None)

    def _move(self, pointer: QPoint | None) -> None:
        reach = tokens.METRICS.grid_glow_radius + tokens.METRICS.grid_pitch
        for point in (self.pointer, pointer):
            if point is not None:
                self._widget.update(
                    QRect(point.x() - reach, point.y() - reach, 2 * reach, 2 * reach)
                )
        self.pointer = pointer


class DotGridWidget(QWidget):
    """A plain area in the paper colour with the dot grid on it.

    With ``glow`` the dots near the pointer light up.
    """

    def __init__(self, parent: QWidget | None = None, *, glow: bool = False) -> None:
        super().__init__(parent)
        self._glow = _PointerGlow(self) if glow else None

    def event(self, event: QEvent) -> bool:
        if self._glow is not None:
            self._glow.handle(event)
        return super().event(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        pointer = self._glow.pointer if self._glow is not None else None
        paint_dot_grid(self, event.rect(), fill=True, pointer=pointer)


class DotGridDialog(QDialog):
    """A dialog with the dot grid behind its content.

    With ``glow`` the dots near the pointer light up.
    """

    def __init__(self, parent: QWidget | None = None, *, glow: bool = False) -> None:
        super().__init__(parent)
        self._glow = _PointerGlow(self) if glow else None

    def event(self, event: QEvent) -> bool:
        if self._glow is not None:
            self._glow.handle(event)
        return super().event(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        pointer = self._glow.pointer if self._glow is not None else None
        paint_dot_grid(self, event.rect(), pointer=pointer)
