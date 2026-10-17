# SPDX-License-Identifier: GPL-3.0-or-later
"""A scrolled area that moves its page up and down only.

Its page is never cut at the side: the area asks for the width its page
needs, with room for the scroll bar, so the window it is in grows to fit
(see ``least_size``) instead of hiding the end of a row.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QSize, Qt
from PySide6.QtWidgets import QScrollArea, QWidget


class VerticalScrollArea(QScrollArea):
    """Scrolls ``page`` up and down; as wide as ``page`` needs."""

    def __init__(self, page: QWidget | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        if page is not None:
            self.setWidget(page)

    def _around_the_page(self) -> int:
        """The width the area adds to its page: frame, margins and scroll bar."""
        margins = self.viewportMargins()
        bar = 0
        if self.verticalScrollBarPolicy() != Qt.ScrollBarPolicy.ScrollBarAlwaysOff:
            bar = self.verticalScrollBar().sizeHint().width()
        return 2 * self.frameWidth() + margins.left() + margins.right() + bar

    def _fitted(self, own: QSize, page_width: int) -> QSize:
        return QSize(max(own.width(), page_width + self._around_the_page()), own.height())

    def minimumSizeHint(self) -> QSize:
        page = self.widget()
        own = super().minimumSizeHint()
        return own if page is None else self._fitted(own, page.minimumSizeHint().width())

    def sizeHint(self) -> QSize:
        page = self.widget()
        own = super().sizeHint()
        return own if page is None else self._fitted(own, page.sizeHint().width())

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        handled = super().eventFilter(watched, event)
        # What the page needs changed: so may what the area asks for.
        if watched is self.widget() and event.type() == QEvent.Type.LayoutRequest:
            self.updateGeometry()
        return handled
