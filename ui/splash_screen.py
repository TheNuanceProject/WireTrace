# SPDX-License-Identifier: GPL-3.0-or-later
"""The splash screen: shown while the application starts.

It reports the real start-up steps and closes the moment the main
window is ready. There is no minimum time on screen.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPaintEvent
from PySide6.QtWidgets import QApplication, QProgressBar, QSplashScreen, QWidget

from app.icon_loader import app_icon_pixmap
from ui.dialogs.common import dialog_layout, heading, note
from ui.themes import tokens
from ui.widgets.dot_grid import DotGridWidget
from ui.widgets.styling import plain_label
from version import APP_DESCRIPTION, APP_NAME, COPYRIGHT

#: The bar is filled in steps of a thousandth, so any number of
#: start-up steps divides it evenly enough.
_PROGRESS_RANGE = 1000


class _SplashSurface(DotGridWidget):
    """The splash background: paper, dot grid, and an outline.

    The splash window has no frame, so the outline is what separates it
    from whatever is behind it on the desktop.
    """

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setPen(QColor(tokens.palette().edge))
        border = tokens.METRICS.border
        painter.drawRect(self.rect().adjusted(0, 0, -border, -border))
        painter.end()


class SplashScreen(QSplashScreen):
    """Logo, name, version, a progress bar and the current start-up step."""

    def __init__(self) -> None:
        super().__init__()
        metrics = tokens.METRICS
        self.setFixedSize(metrics.splash_width, metrics.splash_height)
        self.setWindowFlags(
            Qt.WindowType.SplashScreen
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )

        surface = _SplashSurface(self, glow=True)
        surface.setGeometry(self.rect())
        layout = dialog_layout(surface)
        layout.setSpacing(metrics.space_1)
        layout.addStretch(3)

        logo = app_icon_pixmap(metrics.logo_large)
        if not logo.isNull():
            picture = plain_label()
            picture.setPixmap(logo)
            picture.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            layout.addWidget(picture)

        name = heading(APP_NAME, "displayTitle")
        name.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(name)
        self._version = note(centred=True)
        layout.addWidget(self._version)
        layout.addWidget(note(APP_DESCRIPTION, centred=True))
        layout.addStretch(3)

        self._progress = QProgressBar()
        self._progress.setRange(0, _PROGRESS_RANGE)
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(metrics.progress_height)
        layout.addWidget(self._progress)
        self._status = note(small=True, centred=True)
        layout.addWidget(self._status)
        layout.addStretch(1)
        layout.addWidget(note(COPYRIGHT, small=True, centred=True))

    # ── Public API ───────────────────────────────────────────────────────

    @property
    def status(self) -> str:
        return self._status.text()

    @property
    def fraction(self) -> float:
        """How much of the bar is filled, from 0 to 1."""
        return self._progress.value() / _PROGRESS_RANGE

    def set_version(self, version: str) -> None:
        self._version.setText(f"v{version}")

    def show(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.move(
                area.x() + (area.width() - self.width()) // 2,
                area.y() + (area.height() - self.height()) // 2,
            )
        super().show()

    def show_step(self, message: str, fraction: float) -> None:
        """Say which start-up step is running and how far along it is.

        The screen is drawn at once: the step that follows may keep the
        program busy, and the text must be up before it starts.
        """
        self._status.setText(message)
        self._progress.setValue(round(max(0.0, min(1.0, fraction)) * _PROGRESS_RANGE))
        self.repaint()
        QApplication.processEvents()

    def finish_and_close(self, main_window: QWidget) -> None:
        """Show the main window and close the splash."""
        main_window.show()
        self.finish(main_window)
