# SPDX-License-Identifier: MIT
"""WireTrace splash screen — professional splash with smooth animated progress.

Per spec section 6.3:
  - 480 × 320px, centered on screen, frameless
  - Smooth animated progress bar (interpolated, not jumpy)
  - Minimum 3 second display for branding presence
  - Smooth fade-out transition
"""

from __future__ import annotations

import time

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    Qt,
    QTimer,
)
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (
    QGraphicsOpacityEffect,
    QLabel,
    QProgressBar,
    QSplashScreen,
    QVBoxLayout,
    QWidget,
)


class SplashScreen(QSplashScreen):
    """Professional splash screen with animated progress and fade-out."""

    WIDTH = 480
    HEIGHT = 320
    MIN_DISPLAY_SECS = 3.0
    FADE_OUT_MS = 500

    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self.setWindowFlags(
            Qt.WindowType.SplashScreen
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )

        self._show_time: float = 0.0
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self._opacity_effect.setOpacity(1.0)
        self.setGraphicsEffect(self._opacity_effect)

        # For smooth progress animation
        self._progress_anim: QPropertyAnimation | None = None

        self._setup_ui()

    def _setup_ui(self) -> None:
        self._widget = QWidget(self)
        self._widget.setGeometry(0, 0, self.WIDTH, self.HEIGHT)
        self._widget.setObjectName("splashWidget")

        layout = QVBoxLayout(self._widget)
        layout.setContentsMargins(40, 36, 40, 24)
        layout.setSpacing(4)

        layout.addStretch(3)

        # App icon (64x64, per spec section 6.3)
        from app.icon_loader import app_icon_pixmap
        pix = app_icon_pixmap(64)
        if not pix.isNull():
            icon_label = QLabel()
            icon_label.setPixmap(pix)
            icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(icon_label)
            layout.addSpacing(8)

        # App name
        name_label = QLabel("WireTrace")
        name_font = QFont()
        name_font.setPointSize(26)
        name_font.setWeight(QFont.Weight.DemiBold)
        name_label.setFont(name_font)
        name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(name_label)

        layout.addSpacing(2)

        # Version
        self._version_label = QLabel()
        ver_font = QFont()
        ver_font.setPointSize(10)
        self._version_label.setFont(ver_font)
        self._version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._version_label)

        layout.addSpacing(6)

        # Description
        desc_label = QLabel("Professional Serial Data Monitor")
        desc_font = QFont()
        desc_font.setPointSize(10)
        desc_label.setFont(desc_font)
        desc_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(desc_label)

        layout.addStretch(3)

        # Progress bar (thin, smooth)
        self._progress = QProgressBar()
        self._progress.setRange(0, 1000)  # Higher range for smoother animation
        self._progress.setValue(0)
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(3)
        layout.addWidget(self._progress)

        layout.addSpacing(8)

        # Status message
        self._status_label = QLabel("Initializing...")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        status_font = QFont()
        status_font.setPointSize(9)
        self._status_label.setFont(status_font)
        layout.addWidget(self._status_label)

        layout.addStretch(1)

        # Copyright
        copyright_label = QLabel("© 2026 The Nuance Project")
        copyright_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cr_font = QFont()
        cr_font.setPointSize(8)
        copyright_label.setFont(cr_font)
        layout.addWidget(copyright_label)

    def show(self) -> None:
        self._center_on_screen()
        self._show_time = time.monotonic()
        super().show()

    def _center_on_screen(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            x = geo.x() + (geo.width() - self.WIDTH) // 2
            y = geo.y() + (geo.height() - self.HEIGHT) // 2
            self.move(x, y)

    def set_version(self, version: str) -> None:
        self._version_label.setText(f"v{version}")

    def set_progress(self, value: int, message: str = "") -> None:
        """Smoothly animate progress bar to target value (0-100).

        Internally maps 0-100 to 0-1000 for sub-percent granularity.
        Uses QPropertyAnimation for fluid motion.
        """
        target = min(1000, value * 10)

        if self._progress_anim and self._progress_anim.state() == QPropertyAnimation.State.Running:
            self._progress_anim.stop()

        self._progress_anim = QPropertyAnimation(self._progress, b"value")
        self._progress_anim.setDuration(350)
        self._progress_anim.setStartValue(self._progress.value())
        self._progress_anim.setEndValue(target)
        self._progress_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._progress_anim.start()

        if message:
            self._status_label.setText(message)

        self.repaint()
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()

    def finish_and_close(self, main_window) -> None:
        """Close splash with fade-out after minimum display time."""
        self.set_progress(100, "Ready")

        elapsed = time.monotonic() - self._show_time
        remaining_ms = max(0, int((self.MIN_DISPLAY_SECS - elapsed) * 1000))

        QTimer.singleShot(remaining_ms, lambda: self._fade_out(main_window))

    def _fade_out(self, main_window) -> None:
        self._fade_anim = QPropertyAnimation(self._opacity_effect, b"opacity")
        self._fade_anim.setDuration(self.FADE_OUT_MS)
        self._fade_anim.setStartValue(1.0)
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_anim.finished.connect(lambda: self._do_finish(main_window))
        self._fade_anim.start()

    def _do_finish(self, main_window) -> None:
        self.close()
        main_window.show()
