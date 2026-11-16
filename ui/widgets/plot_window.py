# SPDX-License-Identifier: GPL-3.0-or-later
"""The plot in a window of its own, for a second monitor.

The window holds the same ``PlotView`` the tab can show under the
console. Closing it closes the plot; the tab's Plot button opens it
again.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QWidget

from app.constants import FIELD_SEPARATOR, PLOT_STATS_INTERVAL_MS
from ui.themes import tokens
from ui.widgets.least_size import keep_least_size
from ui.widgets.plot_view import PlotView
from ui.widgets.status_bar import ElidedLabel
from version import APP_NAME


class PlotWindow(QWidget):
    """A top-level window around a tab's plot.

    Signals:
        closed():  The user closed the window; the plot should close.
    """

    closed = Signal()

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowType.Window)
        self.setObjectName("plotView")
        metrics = tokens.METRICS
        self._view: PlotView | None = None
        self._state = ""

        self._summary = ElidedLabel()
        footer = QFrame()
        footer.setObjectName("plotFooter")
        footer.setFixedHeight(metrics.status_height)
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(metrics.space_3, 0, metrics.space_3, 0)
        footer_layout.setSpacing(metrics.space_4)
        footer_layout.addWidget(self._summary, 1)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._layout.addWidget(footer)

        self.set_title(title)
        self.resize(metrics.plot_window_width, metrics.plot_window_height)
        keep_least_size(self, metrics.plot_window_min_width, metrics.plot_window_min_height)

        self._refresh = QTimer(self)
        self._refresh.setInterval(PLOT_STATS_INTERVAL_MS)
        self._refresh.timeout.connect(self._show_summary)

    # ── Public API ───────────────────────────────────────────────────────

    def set_title(self, title: str) -> None:
        """Name the window after the tab it belongs to."""
        self.setWindowTitle(f"{title} plot - {APP_NAME}" if title else f"Plot - {APP_NAME}")

    def set_state(self, state: str) -> None:
        """Say what the connection is doing: "COM7, live"."""
        self._state = state
        self._show_summary()

    def summary_text(self) -> str:
        return self._summary.text()

    def take(self, view: PlotView) -> None:
        """Put ``view`` in the window, above the footer."""
        self._view = view
        self._layout.insertWidget(0, view, 1)
        view.show()
        self._show_summary()
        self._refresh.start()

    def release(self) -> PlotView | None:
        """Give the view back, leaving the window empty."""
        view, self._view = self._view, None
        self._refresh.stop()
        if view is not None:
            self._layout.removeWidget(view)
        return view

    def hasHeightForWidth(self) -> bool:
        """Never: the window keeps the size the user gives it.

        Wrapped text inside (the measuring table, the note) makes the
        layout's height depend on its width. A window that says so is
        resized by Qt on Windows, at every move and resize, to the
        height all its content prefers (``QWidgetWindowPrivate::
        closestAcceptableGeometry``). Inside the window the layout still
        wraps its text to the width it is given.
        """
        return False

    # ── Internal ─────────────────────────────────────────────────────────

    def _show_summary(self) -> None:
        parts = [self._state] if self._state else []
        if self._view is not None:
            parts.append(self._view.summary())
        self._summary.setText(FIELD_SEPARATOR.join(parts))

    def closeEvent(self, event: QCloseEvent) -> None:
        # The plot hides this window; it is destroyed only once the plot
        # has been taken out of it.
        event.ignore()
        self.closed.emit()
