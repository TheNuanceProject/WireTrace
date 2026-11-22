# SPDX-License-Identifier: GPL-3.0-or-later
"""What the tour draws over a window: the window dimmed except for the
control a step is about, a ring around that control, and a card that
says what it does.

The overlay covers its window and takes every click and key while it
is shown, the window's shortcuts included, so nothing behind it can be
changed by accident; Esc closes the tour. Something the step opened
over the window, such as a menu, is kept clear of the card. While the
card is over a window in front of the main one (Preferences, the
report, the plot on its own), the main window is dimmed and covered
the same way (``Backdrop``), so the whole screen reads as one. Both
stay above anything added to their window while they are shown.

The scrim is drawn at once and stays while the tour moves between
steps and windows; only the card arrives, with a short fade and move
(``MOTION.base``);
when the system asks for reduced motion it does not move, and fades in
``MOTION.fast`` (design language, section 10).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import (
    QChildEvent,
    QEasingCurve,
    QEvent,
    QObject,
    QParallelAnimationGroup,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QContextMenuEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)

from ui.dialogs.common import action_button, heading, note
from ui.themes import tokens
from ui.widgets.styling import QUIET, plain_label, set_state

#: Where a target is, in the overlay's own coordinates, or None for none.
Locator = Callable[[], QRect | None]


@dataclass(frozen=True)
class CardAction:
    """A button of the card."""

    label: str
    run: Callable[[], None]
    primary: bool = False
    #: A quiet button at the left end, such as "Close tour".
    leading: bool = False


def easing(points: tuple[float, float, float, float]) -> QEasingCurve:
    """The cubic Bezier curve of a motion token, as Qt draws it."""
    x1, y1, x2, y2 = points
    curve = QEasingCurve(QEasingCurve.Type.BezierSpline)
    curve.addCubicBezierSegment(QPointF(x1, y1), QPointF(x2, y2), QPointF(1, 1))
    return curve


class TourCard(QFrame):
    """The card of a step: a title, what it says, where the step is in its
    chapter, and buttons."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("tourCard")
        metrics = tokens.METRICS
        self.setFixedWidth(metrics.dialog_width)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(metrics.space_6, metrics.space_6, metrics.space_6, metrics.space_6)
        layout.setSpacing(metrics.space_4)
        words = QVBoxLayout()
        words.setSpacing(metrics.space_2)
        # Where the step is: above the title, where it has the card's
        # whole width.
        self._count = note(small=True)
        words.addWidget(self._count)
        self._title = heading("")
        self._title.setWordWrap(True)
        self._text = plain_label("")
        self._text.setWordWrap(True)
        words.addWidget(self._title)
        words.addWidget(self._text)
        layout.addLayout(words)
        self._layout = layout
        #: The row of buttons, made again for each step.
        self._row: QWidget | None = None
        self._default = None

    def show_step(self, title: str, text: str, count: str, actions: list[CardAction]) -> None:
        """Show ``title`` and ``text``, ``count`` (such as "2 of 5", or empty)
        and a button for each action."""
        self._title.setText(title)
        self._text.setText(text)
        self.setAccessibleName(title)
        self.setAccessibleDescription(text)
        if self._row is not None:
            self._layout.removeWidget(self._row)
            self._row.hide()
            self._row.deleteLater()
        self._default = None
        self._row = QWidget(self)
        row = QHBoxLayout(self._row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(tokens.METRICS.space_2)
        self._count.setText(count)
        self._count.setVisible(bool(count))
        for action in actions:
            if action.leading:
                row.addWidget(self._button(action))
        row.addStretch(1)
        for action in actions:
            if not action.leading:
                row.addWidget(self._button(action))
        self._layout.addWidget(self._row)
        # A widget added to a shown one is shown only on the next turn of
        # the event loop; the row is shown with the card, at once.
        self._row.show()
        self.ensurePolished()
        self._fit()

    def _fit(self) -> None:
        """Take the height the text needs at the card's width. Asked again
        whenever the layout changes: the text takes its final size only
        once the style has given it its font."""
        self._layout.activate()
        height = self._layout.totalHeightForWidth(self.width())
        if height != self.height():
            self.resize(self.width(), height)

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.LayoutRequest:
            self._fit()
        return super().event(event)

    def focus_default(self) -> None:
        """Give the keyboard focus to the main button."""
        if self._default is not None:
            self._default.setFocus(Qt.FocusReason.OtherFocusReason)

    def _button(self, action: CardAction) -> QWidget:
        button = action_button(action.label, primary=action.primary)
        if action.leading:
            set_state(button, QUIET, True)
        button.clicked.connect(action.run)
        if action.primary:
            button.setDefault(True)
            self._default = button
        return button


def paint_scrim(widget: QWidget, hole: QRect | None = None) -> None:
    """Dim ``widget`` with the scrim, except for ``hole``."""
    radius = tokens.METRICS.radius_md
    palette = tokens.palette()
    painter = QPainter(widget)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    dimmed = QPainterPath()
    dimmed.addRect(QRectF(widget.rect()))
    if hole is not None:
        cut = QPainterPath()
        cut.addRoundedRect(QRectF(hole), radius, radius)
        dimmed = dimmed.subtracted(cut)
    scrim = QColor(palette.scrim.colour)
    scrim.setAlphaF(palette.scrim.opacity)
    painter.fillPath(dimmed, scrim)
    if hole is not None:
        width = tokens.METRICS.mark
        painter.setPen(QPen(QColor(palette.accent_strong), width))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        inset = width / 2
        painter.drawRoundedRect(QRectF(hole).adjusted(inset, inset, -inset, -inset), radius, radius)
    painter.end()


class _Cover(QWidget):
    """Covers ``host`` with the scrim and takes every click and key that
    reaches it, the host window's shortcuts included.

    Signals:
        close_requested():  Esc was pressed.
    """

    close_requested = Signal()

    def __init__(self, host: QWidget) -> None:
        super().__init__(host)
        self._host = host
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation)

    def _cover(self) -> None:
        """Cover the host and follow it: called once what the cover holds
        has been made."""
        self._host.installEventFilter(self)
        self.setGeometry(self._host.rect())
        self.show()
        self.raise_()

    @property
    def host(self) -> QWidget:
        return self._host

    def _uncover(self) -> None:
        self._host.removeEventFilter(self)
        self.hide()
        self.deleteLater()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._host:
            kind = event.type()
            if kind in (QEvent.Type.Resize, QEvent.Type.LayoutRequest):
                self.setGeometry(self._host.rect())
                self._host_resized()
            elif kind == QEvent.Type.ChildAdded and isinstance(event, QChildEvent):
                child = event.child()
                if child is not self and child.isWidgetType():
                    # A widget added to the window, a notice say, would be
                    # drawn over the cover: the cover goes back on top.
                    QTimer.singleShot(0, self, self.raise_)
        return super().eventFilter(watched, event)

    def _host_resized(self) -> None:
        """The host was resized or laid out again."""

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ShortcutOverride:
            # Keys pressed here, or on what is in the cover, come here;
            # taken as keys, they reach no shortcut of the window behind.
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close_requested.emit()
        # No key goes on to the window behind.
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def wheelEvent(self, event: QWheelEvent) -> None:
        event.accept()

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        # Not passed on: the window would offer its own menu.
        event.accept()


class Backdrop(_Cover):
    """Dims ``host``, the main window, while the tour's card is over a
    window in front of it, and takes its clicks and keys.

    It holds the main window's focus, so that keys pressed after the main
    window is clicked come here rather than to the window's shortcuts.
    """

    def __init__(self, host: QWidget) -> None:
        super().__init__(host)
        self._cover()
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def close_backdrop(self) -> None:
        self._uncover()

    def paintEvent(self, event: QPaintEvent) -> None:
        paint_scrim(self)


class TourOverlay(_Cover):
    """Dims ``host`` except for a target, rings the target, and shows the
    card beside it.

    Signals:
        close_requested():  Esc was pressed.
        back_requested():   The left arrow was pressed.
        next_requested():   The right arrow was pressed.
    """

    back_requested = Signal()
    next_requested = Signal()

    def __init__(self, host: QWidget, reduced_motion: bool) -> None:
        super().__init__(host)
        self._reduced_motion = reduced_motion
        self._locate: Locator = lambda: None
        self._keep_clear: Locator = lambda: None
        #: A button of the card a click passed on by ``press_at`` went down on.
        self._pressed: QAbstractButton | None = None
        self.card = TourCard(self)
        self._fade = QGraphicsOpacityEffect(self.card)
        self.card.setGraphicsEffect(self._fade)
        self._arrival: QParallelAnimationGroup | None = None
        # A target not shown yet (the plot's values, before the plot has
        # found them) is looked for again until it is.
        self._seek = QTimer(self)
        self._seek.setInterval(tokens.MOTION.slow)
        self._seek.timeout.connect(self._look_again)
        self.card.installEventFilter(self)
        self._cover()

    def show_step(
        self,
        title: str,
        text: str,
        count: str,
        actions: list[CardAction],
        locate: Locator,
        *,
        has_target: bool = False,
        keep_clear: Locator | None = None,
    ) -> None:
        """Show a step: the card, beside what ``locate`` finds and clear of
        what ``keep_clear`` finds. A step that ``has_target`` whose target
        is not shown yet looks for it again until it is."""
        self._locate = locate
        self._keep_clear = keep_clear or (lambda: None)
        self.card.show_step(title, text, count, actions)
        self.raise_()
        self.update()
        self._arrive(self._card_place())
        self.card.focus_default()
        if has_target and self.ring() is None:
            self._seek.start()
        else:
            self._seek.stop()

    def _look_again(self) -> None:
        if self.ring() is not None:
            self._seek.stop()
            self.settle()

    def settle(self) -> None:
        """Put the card where it belongs now, at once: after what it is
        kept clear of has opened or moved."""
        self._settle_card()
        self.update()

    def press_at(self, point: QPoint) -> None:
        """A click that something open over the window took, going down at
        ``point`` on the screen: on a button of the card, the button is pressed."""
        self._pressed = self._button_at(point)
        if self._pressed is not None:
            self._pressed.setDown(True)

    def release_at(self, point: QPoint) -> None:
        """The click passed on by ``press_at`` comes up at ``point``: on the
        button it went down on, the button is clicked."""
        pressed, self._pressed = self._pressed, None
        if pressed is None:
            return
        pressed.setDown(False)
        if self._button_at(point) is pressed:
            pressed.click()

    def _button_at(self, point: QPoint) -> QAbstractButton | None:
        widget = self.card.childAt(self.card.mapFromGlobal(point))
        while widget is not None and widget is not self.card:
            if isinstance(widget, QAbstractButton):
                return widget if widget.isEnabled() else None
            widget = widget.parentWidget()
        return None

    def close_overlay(self) -> None:
        self._seek.stop()
        if self._arrival is not None:
            self._arrival.stop()
        self._uncover()

    # ── Where things go ──────────────────────────────────────────────────

    def ring(self) -> QRect | None:
        """The ring around the target, in the overlay's coordinates."""
        target = self._locate()
        if target is None or not target.isValid():
            return None
        pad = tokens.METRICS.space_2
        return target.adjusted(-pad, -pad, pad, pad).intersected(self.rect())

    def _card_place(self) -> QPoint:
        """Below the ring, and what is kept clear, if the card fits there,
        else above, right, left; where it covers the least of them when it
        fits beside them nowhere; in the middle when there is no ring."""
        metrics = tokens.METRICS
        margin, gap = metrics.space_4, metrics.space_4
        size = self.card.size()
        bounds = self.rect().adjusted(margin, margin, -margin, -margin)
        ring = self.ring()
        clear = self._keep_clear()
        if clear is not None and clear.isValid():
            clear = clear.intersected(self.rect())
            ring = clear if ring is None else ring.united(clear)
        if ring is None or not ring.isValid():
            return QPoint(
                bounds.left() + (bounds.width() - size.width()) // 2,
                bounds.top() + (bounds.height() - size.height()) // 2,
            )

        def inside(place: QPoint) -> QPoint:
            return QPoint(
                min(max(place.x(), bounds.left()), bounds.right() + 1 - size.width()),
                min(max(place.y(), bounds.top()), bounds.bottom() + 1 - size.height()),
            )

        def covered(place: QPoint) -> int:
            overlap = QRect(place, size).intersected(ring)
            return overlap.width() * overlap.height() if overlap.isValid() else 0

        places = [
            inside(QPoint(ring.left(), ring.bottom() + 1 + gap)),
            inside(QPoint(ring.left(), ring.top() - gap - size.height())),
            inside(QPoint(ring.right() + 1 + gap, ring.top())),
            inside(QPoint(ring.left() - gap - size.width(), ring.top())),
        ]
        return min(places, key=covered)

    def _settle_card(self) -> None:
        """Put the card where it belongs now, at once."""
        if self._arrival is not None:
            self._arrival.stop()
            self._fade.setOpacity(1.0)
        self.card.move(self._card_place())

    def _arrive(self, place: QPoint) -> None:
        motion = tokens.MOTION
        if self._arrival is not None:
            self._arrival.stop()
        group = QParallelAnimationGroup(self)
        fade = QPropertyAnimation(self._fade, b"opacity", group)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        fade.setEasingCurve(easing(motion.ease_out))
        if self._reduced_motion:
            fade.setDuration(motion.fast)
            self.card.move(place)
        else:
            fade.setDuration(motion.base)
            move = QPropertyAnimation(self.card, b"pos", group)
            move.setStartValue(place + QPoint(0, tokens.METRICS.space_2))
            move.setEndValue(place)
            move.setDuration(motion.base)
            move.setEasingCurve(easing(motion.ease_out))
            group.addAnimation(move)
        group.addAnimation(fade)
        self._arrival = group
        group.start()

    # ── Drawing and events ───────────────────────────────────────────────

    def paintEvent(self, event: QPaintEvent) -> None:
        paint_scrim(self, self.ring())

    def _host_resized(self) -> None:
        self._settle_card()
        self.update()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.card and event.type() == QEvent.Type.Resize:
            self._settle_card()
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key.Key_Left:
            self.back_requested.emit()
        elif key == Qt.Key.Key_Right:
            self.next_requested.emit()
        super().keyPressEvent(event)
