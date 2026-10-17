# SPDX-License-Identifier: GPL-3.0-or-later
"""WireTrace application lifecycle manager.

Responsibilities:
  - Owns QApplication creation and global state
  - Shows the splash screen and names each start-up step on it
  - Creates and shows MainWindow
  - Removes session recordings left behind by a previous run
  - Does not touch serial I/O or log files

Start-up order:
  1. Create the application, register the bundled fonts
  2. Load preferences and apply the theme, so that every window,
     the splash screen included, is drawn in it
  3. Show the splash screen
  4. Remove stale session recordings, build the main window
  5. Show the main window
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle, QStyleFactory

from app.config import ConfigManager
from app.constants import (
    SPOOL_DIRECTORY_NAME,
    TOOLTIP_FALL_ASLEEP_MS,
    TOOLTIP_WAKE_UP_MS,
    get_cache_dir,
)
from app.main_thread_gc import MainThreadCollector
from core.session_spool import SessionSpool
from ui.main_window import MainWindow
from ui.splash_screen import SplashScreen
from ui.themes.fonts import register_bundled_fonts
from ui.themes.theme_manager import BASE_STYLE, ThemeManager
from ui.widgets.console_accessibility import install_console_accessibility
from updater.install import hold_installer_mutex, installer_update_choice
from version import APP_AUTHOR, APP_NAME, APP_VERSION

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
            return TOOLTIP_WAKE_UP_MS
        if hint == QStyle.StyleHint.SH_ToolTip_FallAsleepDelay:
            return TOOLTIP_FALL_ASLEEP_MS
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
        self._collector: MainThreadCollector | None = None

    def run(self) -> int:
        """Run the application. Returns exit code."""
        self._create_app()
        self._load_preferences_and_theme()
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
        self._app.setOrganizationName(APP_AUTHOR)
        self._collector = MainThreadCollector(self._app)
        self._app.aboutToQuit.connect(self._collector.stop)
        hold_installer_mutex()

        # One style on every operating system, showing tooltips sooner
        # than Qt's default; see _FastTooltipStyle.
        self._tooltip_style = _FastTooltipStyle(QStyleFactory.create(BASE_STYLE))
        self._app.setStyle(self._tooltip_style)

        # Set global window icon (all windows, dialogs, taskbar)
        from app.icon_loader import app_icon
        qi = app_icon()
        if not qi.isNull():
            self._app.setWindowIcon(qi)

    def _load_preferences_and_theme(self) -> None:
        """Load preferences and apply the theme before any window is shown."""
        register_bundled_fonts()
        install_console_accessibility()
        self._config = ConfigManager()
        self._config.load()
        self._theme_manager = ThemeManager(self._app)
        self._theme_manager.apply_preference(self._config.theme)

    def _show_splash(self) -> None:
        """Show the splash screen."""
        self._splash = SplashScreen()
        self._splash.set_version(APP_VERSION)
        self._splash.show()
        QApplication.processEvents()

    def _initialize(self) -> None:
        """Run the start-up steps, naming each on the splash screen."""
        steps: tuple[tuple[str, Callable[[], None]], ...] = (
            # Session recordings left behind by a run that did not exit
            # cleanly (crash, power loss) are removed here.
            ("Removing old session recordings", self._remove_stale_recordings),
            ("Building the window", self._build_main_window),
        )
        for index, (message, run) in enumerate(steps):
            self._splash.show_step(message, index / len(steps))
            run()
        self._splash.show_step("Ready", 1.0)
        logger.info("%s v%s initialized successfully", APP_NAME, APP_VERSION)

    @staticmethod
    def _remove_stale_recordings() -> None:
        SessionSpool.remove_stale(os.path.join(get_cache_dir(), SPOOL_DIRECTORY_NAME))

    def _build_main_window(self) -> None:
        self._main_window = MainWindow(self._config, self._theme_manager)
        # Closing the window shuts every tab down. This covers an exit
        # that never closes it, such as the end of the desktop session.
        self._app.aboutToQuit.connect(self._main_window.shutdown_all_tabs)

    def _show_main_window(self) -> None:
        """Close splash and show the main window."""
        self._splash.finish_and_close(self._main_window)

        self._take_over_installer_choice()
        self._main_window.updates.start()
        self._main_window.usage.start()
        self._main_window.ask_about_usage_statistics()

    def _take_over_installer_choice(self) -> None:
        """Apply, once, what was chosen in the Windows installer about
        update checks. After that the setting in Preferences is the only
        one that counts.

        Only a user who has no preferences yet takes the choice over.
        Someone updating from an earlier version keeps the setting they
        already have, whatever the installer's box showed.
        """
        if self._config.installer_choice_read:
            return
        choice = installer_update_choice()
        if choice is not None and self._config.first_run:
            self._config.check_updates_on_startup = choice
        self._config.installer_choice_read = True
        self._config.save()
