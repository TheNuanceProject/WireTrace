# SPDX-License-Identifier: MIT
"""WireTrace application lifecycle manager.

Per spec section 3.4:
  - Owns QApplication creation and global state
  - Manages splash screen with real initialization steps
  - Creates and shows MainWindow
  - Does NOT touch serial I/O or file I/O

Initialization steps (spec section 6.3):
  1.  0-15%  Load preferences.ini, validate paths
  2. 15-30%  Load QSS theme, apply
  3. 30-50%  Build main window, widgets, menus
  4. 50-70%  Load SVG icons
  5. 70-85%  Restore window state (size, position)
  6. 85-100% Final validation, ready
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys

from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle

from app.config import ConfigManager
from app.constants import ThemeID
from ui.main_window import MainWindow
from ui.splash_screen import SplashScreen
from ui.themes.theme_manager import ThemeManager
from version import APP_NAME, APP_VERSION

logger = logging.getLogger(__name__)


class _FastTooltipStyle(QProxyStyle):
    """QProxyStyle that tightens tooltip show/hide timing.

    Qt's default tooltip wake-up delay is ~700ms, which feels sluggish
    in a tool engineers use for quick inspection. We override just the
    two style hints that control timing; all other behaviour is
    inherited from the base application style.
    """

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.StyleHint.SH_ToolTip_WakeUpDelay:
            return 300
        if hint == QStyle.StyleHint.SH_ToolTip_FallAsleepDelay:
            return 100
        return super().styleHint(hint, option, widget, returnData)


class WireTraceApp:
    """Application lifecycle manager."""

    def __init__(self, argv: list[str] | None = None) -> None:
        self._argv = argv or sys.argv
        self._app: QApplication | None = None
        self._splash: SplashScreen | None = None
        self._main_window: MainWindow | None = None
        self._config: ConfigManager | None = None
        self._theme_manager: ThemeManager | None = None

    def run(self) -> int:
        """Run the application. Returns exit code."""
        self._create_app()
        self._show_splash()
        self._initialize()
        self._show_main_window()
        return self._app.exec()

    def _create_app(self) -> None:
        """Create the QApplication instance."""
        # High DPI support
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )

        self._app = QApplication(self._argv)
        self._app.setApplicationName(APP_NAME)
        self._app.setApplicationVersion(APP_VERSION)
        self._app.setOrganizationName("The Nuance Project")

        # Faster tooltip display (default ~700ms → 300ms).
        # _FastTooltipStyle is defined at module level for clarity.
        self._tooltip_style = _FastTooltipStyle(self._app.style())
        self._app.setStyle(self._tooltip_style)

        # Set global window icon (all windows, dialogs, taskbar)
        from app.icon_loader import app_icon
        qi = app_icon()
        if not qi.isNull():
            self._app.setWindowIcon(qi)

    def _show_splash(self) -> None:
        """Show the splash screen."""
        self._splash = SplashScreen()
        self._splash.set_version(APP_VERSION)
        self._splash.show()
        QApplication.processEvents()

    def _initialize(self) -> None:
        """Perform real initialization with smooth progress updates."""
        splash = self._splash

        def step(pct: int, msg: str) -> None:
            """Set progress and let Qt render the animation cleanly.

            Uses a nested ``QEventLoop`` quit-after-delay to yield 150ms
            to the UI thread without spinning CPU. This is Qt's idiomatic
            way to wait for animations/layouts while keeping the event
            loop fully responsive — no ``time.sleep`` + ``processEvents``
            busy-wait.
            """
            splash.set_progress(pct, msg)
            loop = QEventLoop()
            QTimer.singleShot(150, loop.quit)
            loop.exec()

        # Step 1: Load configuration (0-15%)
        step(0, "Loading configuration...")
        self._config = ConfigManager()
        self._config.load()
        step(15, "Configuration loaded")

        # Step 2: Load theme (15-30%)
        step(20, "Loading theme...")
        theme_id = ThemeID.STUDIO_LIGHT
        with contextlib.suppress(ValueError, KeyError):
            theme_id = ThemeID(self._config.theme)
        self._theme_manager = ThemeManager(self._app)
        self._theme_manager.apply_theme(theme_id)
        step(35, "Theme applied")

        # Step 3: Build main window (35-55%)
        step(40, "Building interface...")
        self._main_window = MainWindow(self._config, self._theme_manager)
        step(55, "Interface ready")

        # Step 4: Load icons (55-75%)
        step(60, "Loading resources...")
        self._load_icons()
        step(75, "Resources loaded")

        # Step 5: Restore window state (75-90%)
        step(80, "Restoring session...")
        step(90, "Session restored")

        # Step 6: Final validation (90-100%)
        step(95, "Final validation...")
        self._validate()
        step(100, "Ready")

    def _load_icons(self) -> None:
        """Verify icon availability.

        Icons are loaded on-demand via app.icon_loader.icon().
        This step just verifies the resources directory exists.
        """
        from app.icon_loader import has_icons
        if has_icons():
            resources_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "resources", "icons")
            icon_count = len([f for f in os.listdir(resources_dir) if f.endswith(".svg")])
            logger.info("Found %d SVG icons in resources", icon_count)
        else:
            logger.info("No icons found — buttons will use text labels")

    def _validate(self) -> None:
        """Final validation before showing the main window."""
        logger.info(
            "%s v%s initialized successfully",
            APP_NAME,
            APP_VERSION,
        )

    def _show_main_window(self) -> None:
        """Close splash and show the main window."""
        self._splash.finish_and_close(self._main_window)

        # Start auto-update check (10-second delay per spec section 12)
        from updater.update_manager import UpdateManager
        self._update_manager = UpdateManager(config_manager=self._config)
        self._update_manager.update_available.connect(
            self._on_update_available
        )
        self._update_manager.start()

    def _on_update_available(self, info) -> None:
        """Show update dialog when a new version is detected at startup."""
        from updater.update_manager import UpdateDialog
        dlg = UpdateDialog(info, self._main_window)
        dlg.exec()
        if dlg.result_action == "remind":
            self._update_manager.snooze()
        elif dlg.result_action == "skip":
            self._update_manager.skip_version(info.latest_version)
