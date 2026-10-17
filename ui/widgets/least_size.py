# SPDX-License-Identifier: GPL-3.0-or-later
"""A window's least size: its design floor, or what its content needs if more.

A window, a dialog or a plot window has a floor in the design language
(at least 960 by 600 for the main window, 520 wide for a form dialog).
Set with ``setMinimumSize`` alone, that floor would also be the most a
window could be squeezed to: Qt stops enforcing the minimum its layout
needs once a window has a minimum of its own, so text set larger, or
longer in another language, would be cut off.

``keep_least_size`` gives a window the larger of the two, and keeps it
so whenever the content, the font or the style changes. It is never
more than the screen the window is on can hold.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QSize, Qt
from PySide6.QtWidgets import QWidget

#: The Qt property of the keeper that holds the window's floor.
_FLOOR = "leastSizeFloor"


def _apply(window: QWidget, floor: QSize) -> None:
    """Give ``window`` the larger of ``floor`` and what its content needs."""
    least = floor.expandedTo(window.minimumSizeHint())
    # Never more than the screen holds, or the window could not be seen
    # whole; what does not fit then scrolls.
    screen = window.screen()
    if screen is not None:
        least = least.boundedTo(screen.availableGeometry().size())
    if window.minimumSize() != least:
        window.setMinimumSize(least)


class _LeastSize(QObject):
    """Keeps a window's minimum at its floor or at what its content needs.

    It is a child of its window and watches only that window. All it knows
    is held by Qt (its parent, and its floor as a property), never in
    attributes of the Python object: Qt can go on sending it the window's
    events after Python has let go of the Python side of it, as it does
    when a window Qt still owns is no longer referred to from Python.
    """

    def __init__(self, window: QWidget, floor: QSize) -> None:
        super().__init__(window)
        self.setProperty(_FLOOR, floor)
        window.installEventFilter(self)
        _apply(window, floor)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # The events after which what the content needs may have changed.
        recheck = (
            QEvent.Type.LayoutRequest,
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
            QEvent.Type.Polish,
            QEvent.Type.Show,
        )
        # Installed on its window alone, so ``watched`` is that window.
        if event.type() in recheck and isinstance(watched, QWidget):
            _apply(watched, self.property(_FLOOR))
        return False


def keep_least_size(window: QWidget, width: int = 0, height: int = 0) -> None:
    """Never let ``window`` be smaller than ``width`` by ``height``, nor than
    its content needs.

    Calling it again replaces the floor set before.
    """
    direct = Qt.FindChildOption.FindDirectChildrenOnly
    for keeper in window.findChildren(_LeastSize, options=direct):
        window.removeEventFilter(keeper)
        keeper.setParent(None)
        keeper.deleteLater()
    _LeastSize(window, QSize(width, height))
