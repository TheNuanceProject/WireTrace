# SPDX-License-Identifier: MIT
"""WireTrace main window — tab host, menu bar, welcome state, global shortcuts.

Per spec sections 3.4, 6.1-6.8:
  - Hosts DeviceTabs in a QTabWidget
  - Shows welcome state when no tabs are open
  - Full menu structure (File, View, Themes, Help)
  - Global keyboard shortcuts
  - Theme switching
  - Window state save/restore

Does NOT perform serial I/O or file I/O directly.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction, QFont, QKeySequence
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.config import ConfigManager
from app.constants import (
    DEFAULT_FONT_SIZE,
    FONT_SIZE_MAX,
    FONT_SIZE_MIN,
    FONT_SIZE_STEP,
    ThemeID,
    TimestampMode,
)
from ui.device_tab import DeviceTab
from ui.themes.theme_manager import ThemeManager
from ui.widgets.toast import Toast
from version import APP_DESCRIPTION, WINDOW_TITLE

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """WireTrace main window with tabbed device interface."""

    def __init__(
        self,
        config: ConfigManager,
        theme_manager: ThemeManager,
        parent=None,
    ) -> None:
        super().__init__(parent)

        self._config = config
        self._theme_manager = theme_manager

        self.setWindowTitle(WINDOW_TITLE)
        self.setMinimumSize(800, 600)

        self._setup_central_widget()
        self._setup_menu_bar()
        self._setup_shortcuts()
        self._setup_status_bar()
        self._restore_window_state()

        # Show welcome state initially
        self._update_view()

    # ══════════════════════════════════════════════════════════════════════
    # CENTRAL WIDGET
    # ══════════════════════════════════════════════════════════════════════

    def _setup_central_widget(self) -> None:
        """Stacked widget: welcome page (page 0) or tab widget (page 1)."""
        self._stacked = QStackedWidget()
        self.setCentralWidget(self._stacked)

        # Page 0: Welcome state (spec section 6.1)
        self._welcome_page = self._create_welcome_page()
        self._stacked.addWidget(self._welcome_page)

        # Page 1: Tab widget
        self._tab_widget = QTabWidget()
        self._tab_widget.setTabsClosable(True)
        self._tab_widget.setMovable(True)
        self._tab_widget.tabCloseRequested.connect(self._close_tab)
        self._tab_widget.currentChanged.connect(self._on_tab_changed)
        self._tab_widget.tabBar().setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self._tab_widget.tabBar().customContextMenuRequested.connect(
            self._on_tab_context_menu
        )
        self._stacked.addWidget(self._tab_widget)

        # "+" button in tab bar corner
        add_btn = QPushButton("+")
        add_btn.setObjectName("addTabBtn")
        add_btn.setToolTip("New Tab (Ctrl+T)")
        add_btn.setFixedSize(28, 28)
        add_btn.clicked.connect(self._on_new_tab)
        self._tab_widget.setCornerWidget(add_btn, Qt.Corner.TopLeftCorner)

    def _create_welcome_page(self) -> QWidget:
        """Welcome / first-launch page per spec section 6.1."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(16)

        title = QLabel("Welcome to WireTrace")
        title_font = QFont()
        title_font.setPointSize(20)
        title_font.setWeight(QFont.Weight.DemiBold)
        title.setFont(title_font)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        subtitle = QLabel(APP_DESCRIPTION)
        subtitle.setProperty("secondary", True)
        sub_font = QFont()
        sub_font.setPointSize(13)
        subtitle.setFont(sub_font)
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)

        connect_btn = QPushButton("  +  Connect a Device  ")
        connect_btn.setObjectName("welcomeConnectBtn")
        connect_btn.setMinimumSize(QSize(220, 44))
        btn_font = QFont()
        btn_font.setPointSize(13)
        connect_btn.setFont(btn_font)
        connect_btn.clicked.connect(self._on_new_tab)

        hint = QLabel("Select a serial port to start monitoring")
        hint.setProperty("secondary", True)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout.addStretch(2)

        # App icon (64x64, per spec section 6.1)
        from app.icon_loader import app_icon_pixmap
        pix = app_icon_pixmap(64)
        if not pix.isNull():
            icon_label = QLabel()
            icon_label.setPixmap(pix)
            icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(icon_label)
            layout.addSpacing(12)

        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(24)
        layout.addWidget(connect_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addSpacing(8)
        layout.addWidget(hint)
        layout.addStretch(3)

        return page

    def _update_view(self) -> None:
        """Switch between welcome page and tab widget."""
        if self._tab_widget.count() == 0:
            self._stacked.setCurrentWidget(self._welcome_page)
            self.setWindowTitle(WINDOW_TITLE)
        else:
            self._stacked.setCurrentWidget(self._tab_widget)

    # ══════════════════════════════════════════════════════════════════════
    # MENU BAR (spec section 6.7)
    # ══════════════════════════════════════════════════════════════════════

    def _setup_menu_bar(self) -> None:
        menu_bar = self.menuBar()

        # ── File ──
        file_menu = menu_bar.addMenu("&File")

        self._act_new_tab = file_menu.addAction("New Tab")
        self._act_new_tab.setShortcut(QKeySequence("Ctrl+T"))
        self._act_new_tab.triggered.connect(self._on_new_tab)

        self._act_close_tab = file_menu.addAction("Close Tab")
        self._act_close_tab.setShortcut(QKeySequence("Ctrl+W"))
        self._act_close_tab.triggered.connect(self._on_close_current_tab)

        file_menu.addSeparator()

        self._act_new_log = file_menu.addAction("New Log")
        self._act_new_log.setShortcut(QKeySequence("Ctrl+N"))
        self._act_new_log.triggered.connect(self._on_menu_new_log)

        self._act_quick_save = file_menu.addAction("Quick Save (.txt)")
        self._act_quick_save.setShortcut(QKeySequence("Ctrl+S"))
        self._act_quick_save.triggered.connect(self._on_menu_quick_save)

        self._act_export = file_menu.addAction("Export...")
        self._act_export.setShortcut(QKeySequence("Ctrl+E"))
        self._act_export.triggered.connect(self._on_menu_export)

        file_menu.addSeparator()

        self._act_prefs = file_menu.addAction("Preferences")
        self._act_prefs.setShortcut(QKeySequence("Ctrl+,"))
        self._act_prefs.triggered.connect(self._on_menu_preferences)

        file_menu.addSeparator()

        self._act_exit = file_menu.addAction("Exit")
        self._act_exit.setShortcut(QKeySequence("Ctrl+Q"))
        self._act_exit.triggered.connect(self.close)

        # ── View ──
        view_menu = menu_bar.addMenu("&View")

        self._act_font_up = view_menu.addAction("Font Size +")
        self._act_font_up.setShortcut(QKeySequence("Ctrl++"))
        self._act_font_up.triggered.connect(lambda: self._change_font_size(FONT_SIZE_STEP))

        self._act_font_down = view_menu.addAction("Font Size -")
        self._act_font_down.setShortcut(QKeySequence("Ctrl+-"))
        self._act_font_down.triggered.connect(lambda: self._change_font_size(-FONT_SIZE_STEP))

        self._act_font_reset = view_menu.addAction("Reset Font Size")
        self._act_font_reset.setShortcut(QKeySequence("Ctrl+0"))
        self._act_font_reset.triggered.connect(self._reset_font_size)

        view_menu.addSeparator()

        ts_menu = view_menu.addMenu("Timestamp Format")
        self._act_ts_absolute = ts_menu.addAction("Timestamp")
        self._act_ts_absolute.setCheckable(True)
        self._act_ts_absolute.setChecked(True)
        self._act_ts_absolute.triggered.connect(
            lambda: self._set_timestamp_mode(TimestampMode.ABSOLUTE)
        )
        self._act_ts_relative = ts_menu.addAction("Elapsed")
        self._act_ts_relative.setCheckable(True)
        self._act_ts_relative.triggered.connect(
            lambda: self._set_timestamp_mode(TimestampMode.RELATIVE)
        )

        view_menu.addSeparator()

        self._act_auto_scroll = view_menu.addAction("Auto-Scroll")
        self._act_auto_scroll.setCheckable(True)
        self._act_auto_scroll.setChecked(True)
        self._act_auto_scroll.triggered.connect(self._toggle_auto_scroll)

        dm_menu = view_menu.addMenu("Display Mode")
        self._act_dm_text = dm_menu.addAction("Text")
        self._act_dm_text.setCheckable(True)
        self._act_dm_text.setChecked(True)
        self._act_dm_text.triggered.connect(lambda: self._set_display_mode("text"))
        self._act_dm_hex = dm_menu.addAction("HEX")
        self._act_dm_hex.setCheckable(True)
        self._act_dm_hex.triggered.connect(lambda: self._set_display_mode("hex"))

        # ── Themes ──
        themes_menu = menu_bar.addMenu("&Themes")
        self._themes_menu = themes_menu

        for theme_id, display_name in self._theme_manager.available_themes():
            action = themes_menu.addAction(display_name)
            action.setCheckable(True)
            action.setChecked(theme_id == self._theme_manager.current_theme)
            action.setData(theme_id)
            action.triggered.connect(
                lambda checked, tid=theme_id: self._on_theme_selected(tid)
            )

        # ── Help ──
        help_menu = menu_bar.addMenu("&Help")

        self._act_user_guide = help_menu.addAction("User Guide")
        self._act_user_guide.setShortcut("F1")
        self._act_user_guide.triggered.connect(self._on_menu_user_guide)

        help_menu.addSeparator()

        self._act_check_updates = help_menu.addAction("Check for Updates...")
        self._act_check_updates.triggered.connect(self._on_menu_check_updates)

        help_menu.addSeparator()

        self._act_about = help_menu.addAction("About")
        self._act_about.triggered.connect(self._on_menu_about)

    def _setup_shortcuts(self) -> None:
        """Keyboard shortcuts per spec section 6.8."""
        shortcuts = [
            ("Ctrl+F", self._on_search),
            ("Ctrl+L", self._on_filter_focus),
            ("F3", self._on_search_next),
            ("Shift+F3", self._on_search_prev),
            ("Escape", self._on_escape),
        ]
        for key, handler in shortcuts:
            action = QAction(self)
            action.setShortcut(QKeySequence(key))
            action.triggered.connect(handler)
            self.addAction(action)

    def _setup_status_bar(self) -> None:
        self.statusBar().showMessage("Ready")

    # ══════════════════════════════════════════════════════════════════════
    # TAB MANAGEMENT
    # ══════════════════════════════════════════════════════════════════════

    def _on_new_tab(self) -> None:
        """Create a new device tab."""
        tab = DeviceTab(self._theme_manager, self._config, self)
        tab.title_changed.connect(
            lambda title, t=tab: self._on_tab_title_changed(t, title)
        )
        tab.timestamp_mode_changed.connect(self._sync_timestamp_menu)
        idx = self._tab_widget.addTab(tab, "New Tab")
        self._tab_widget.setCurrentIndex(idx)
        self._update_view()

    def _close_tab(self, index: int) -> None:
        """Close tab at the given index with confirmation."""
        tab = self._tab_widget.widget(index)
        if isinstance(tab, DeviceTab):
            if not tab.confirm_close():
                return
            tab.ordered_shutdown()

        self._tab_widget.removeTab(index)
        if tab:
            tab.deleteLater()
        self._update_view()

    def _on_close_current_tab(self) -> None:
        idx = self._tab_widget.currentIndex()
        if idx >= 0:
            self._close_tab(idx)

    def _on_tab_changed(self, index: int) -> None:
        """Update window title and sync View menu state when tab changes."""
        if index < 0:
            self.setWindowTitle(WINDOW_TITLE)
            return
        title = self._tab_widget.tabText(index)
        self.setWindowTitle(f"{WINDOW_TITLE} — {title}")

        # Sync View menu checkmarks to the current tab's state
        tab = self._current_tab()
        if tab:
            self._sync_timestamp_menu(tab._session.timestamp_mode)

    def _on_tab_title_changed(self, tab: DeviceTab, title: str) -> None:
        """Update tab text when DeviceTab emits title_changed."""
        idx = self._tab_widget.indexOf(tab)
        if idx >= 0:
            self._tab_widget.setTabText(idx, title)
            if idx == self._tab_widget.currentIndex():
                self.setWindowTitle(f"{WINDOW_TITLE} — {title}")

    def _on_tab_context_menu(self, pos) -> None:
        """Context menu on tab right-click."""
        tab_bar = self._tab_widget.tabBar()
        index = tab_bar.tabAt(pos)
        if index < 0:
            return
        menu = QMenu(self)
        rename_action = menu.addAction("Rename")
        rename_action.triggered.connect(lambda: self._rename_tab(index))
        menu.addSeparator()
        close_action = menu.addAction("Close")
        close_action.triggered.connect(lambda: self._close_tab(index))
        menu.exec(tab_bar.mapToGlobal(pos))

    def _rename_tab(self, index: int) -> None:
        """Inline rename of a tab via input dialog."""
        from PySide6.QtWidgets import QInputDialog
        current = self._tab_widget.tabText(index)
        new_name, ok = QInputDialog.getText(
            self, "Rename Tab", "Tab name:", text=current,
        )
        if ok and new_name.strip():
            self._tab_widget.setTabText(index, new_name.strip())
            if index == self._tab_widget.currentIndex():
                self.setWindowTitle(f"{WINDOW_TITLE} — {new_name.strip()}")

    def _current_tab(self) -> DeviceTab | None:
        """Return the active DeviceTab, or None."""
        tab = self._tab_widget.currentWidget()
        return tab if isinstance(tab, DeviceTab) else None

    # ══════════════════════════════════════════════════════════════════════
    # MENU HANDLERS
    # ══════════════════════════════════════════════════════════════════════

    def _on_menu_new_log(self) -> None:
        tab = self._current_tab()
        if tab and tab.is_connected:
            tab._on_log_on()

    def _on_menu_quick_save(self) -> None:
        """Quick save current tab's console content as .txt (Ctrl+S)."""
        tab = self._current_tab()
        if tab:
            tab.quick_save()

    def _on_menu_export(self) -> None:
        """Open export dialog for current tab (Ctrl+E)."""
        tab = self._current_tab()
        if tab:
            tab._on_export()

    def _on_menu_preferences(self) -> None:
        from ui.dialogs.preferences_dialog import PreferencesDialog
        dialog = PreferencesDialog(self._config, self._theme_manager, self)
        dialog.exec()

        # Apply theme from config (may have changed via Save or Reset)
        saved_theme = ThemeID(self._config.theme)
        if saved_theme != self._theme_manager.current_theme:
            self._theme_manager.apply_theme(saved_theme)

        # Sync theme menu checkmarks
        current = self._theme_manager.current_theme
        for action in self._themes_menu.actions():
            action.setChecked(action.data() == current)

        # Update all tab consoles with new tag colors and font settings
        family = self._config.font_family
        size = self._config.font_size
        for i in range(self._tab_widget.count()):
            tab = self._tab_widget.widget(i)
            if isinstance(tab, DeviceTab):
                tab.apply_theme()
                tab._console.set_font_family(family)
                tab._console.set_font_size(size)

    def _on_theme_selected(self, theme_id: ThemeID) -> None:
        """Apply selected theme and update menu checks."""
        self._theme_manager.apply_theme(theme_id)
        self._config.theme = theme_id.value
        for action in self._themes_menu.actions():
            action.setChecked(action.data() == theme_id)
        # Update all tab consoles with new tag colors
        for i in range(self._tab_widget.count()):
            tab = self._tab_widget.widget(i)
            if isinstance(tab, DeviceTab):
                tab.apply_theme()

    def _on_menu_check_updates(self) -> None:
        """Check for updates using background thread, then show dialog if available."""
        from updater.update_manager import UpdateDialog, UpdateManager
        from version import APP_VERSION

        Toast.info(self, "Checking for updates...")
        self.statusBar().showMessage("Checking for updates...", 0)

        # Keep reference alive during async check
        self._update_mgr = UpdateManager()

        def on_available(info):
            self.statusBar().showMessage("")
            dlg = UpdateDialog(info, self)
            dlg.exec()
            # Handle dialog result
            if dlg.result_action == "remind":
                self._update_mgr.snooze()
                Toast.info(self, "Update reminder snoozed for 24 hours")
            elif dlg.result_action == "skip":
                self._update_mgr.skip_version(info.latest_version)
                Toast.info(self, f"Version {info.latest_version} skipped")
            # "update" action is handled inside the dialog itself

        def on_not_available():
            Toast.success(self, f"You're running the latest version (v{APP_VERSION})")
            self.statusBar().showMessage(
                f"Up to date — v{APP_VERSION}", 5000)

        def on_failed(msg):
            Toast.error(self, "Unable to check for updates — please try again later")
            self.statusBar().showMessage("Update check failed", 5000)
            logger.warning("Update check failed: %s", msg)

        self._update_mgr.update_available.connect(on_available)
        self._update_mgr.update_not_available.connect(on_not_available)
        self._update_mgr.check_failed.connect(on_failed)
        self._update_mgr.check_now(silent=False)

    def _on_menu_about(self) -> None:
        from ui.dialogs.about_dialog import AboutDialog
        dialog = AboutDialog(self)
        dialog.exec()

    def _on_menu_user_guide(self) -> None:
        """Open the User Guide HTML file in the system default browser."""
        from pathlib import Path

        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        guide_path = Path(__file__).parent.parent / "resources" / "help" / "user_guide.html"
        if guide_path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(guide_path.resolve())))
        else:
            from ui.widgets.toast import Toast
            Toast.error(self, "User Guide not found")

    # ══════════════════════════════════════════════════════════════════════
    # SHORTCUT HANDLERS (delegate to current tab)
    # ══════════════════════════════════════════════════════════════════════

    def _on_search(self) -> None:
        tab = self._current_tab()
        if tab:
            tab.activate_search()

    def _on_filter_focus(self) -> None:
        tab = self._current_tab()
        if tab:
            tab.activate_filter()

    def _on_search_next(self) -> None:
        tab = self._current_tab()
        if tab:
            tab.search_next()

    def _on_search_prev(self) -> None:
        tab = self._current_tab()
        if tab:
            tab.search_prev()

    def _on_escape(self) -> None:
        tab = self._current_tab()
        if tab:
            tab.clear_search_filter()

    def _change_font_size(self, delta: int) -> None:
        new_size = max(FONT_SIZE_MIN, min(FONT_SIZE_MAX, self._config.font_size + delta))
        self._config.font_size = new_size
        for i in range(self._tab_widget.count()):
            tab = self._tab_widget.widget(i)
            if isinstance(tab, DeviceTab):
                tab._console.set_font_size(new_size)

    def _reset_font_size(self) -> None:
        self._config.font_size = DEFAULT_FONT_SIZE
        for i in range(self._tab_widget.count()):
            tab = self._tab_widget.widget(i)
            if isinstance(tab, DeviceTab):
                tab._console.set_font_size(DEFAULT_FONT_SIZE)

    def _set_timestamp_mode(self, mode: TimestampMode) -> None:
        """Set timestamp mode from View menu — syncs menu + tab + status bar."""
        self._sync_timestamp_menu(mode)
        tab = self._current_tab()
        if tab:
            tab._on_timestamp_toggled(mode)

    def _sync_timestamp_menu(self, mode: TimestampMode) -> None:
        """Sync View menu checkmarks to the given timestamp mode.

        Called from:
          - _set_timestamp_mode (View menu click)
          - DeviceTab.timestamp_mode_changed signal (status bar click)
          - _on_tab_changed (tab switch)
        """
        self._act_ts_absolute.setChecked(mode == TimestampMode.ABSOLUTE)
        self._act_ts_relative.setChecked(mode == TimestampMode.RELATIVE)

    def _toggle_auto_scroll(self) -> None:
        tab = self._current_tab()
        if tab:
            tab._console.auto_scroll = self._act_auto_scroll.isChecked()

    def _set_display_mode(self, mode: str) -> None:
        from app.constants import DisplayMode
        is_hex = mode == "hex"
        self._act_dm_text.setChecked(not is_hex)
        self._act_dm_hex.setChecked(is_hex)
        dm = DisplayMode.HEX if is_hex else DisplayMode.TEXT
        tab = self._current_tab()
        if tab:
            tab.set_display_mode(dm)

    # ══════════════════════════════════════════════════════════════════════
    # WINDOW STATE
    # ══════════════════════════════════════════════════════════════════════

    def _restore_window_state(self) -> None:
        """Restore window position and size from config."""
        self.resize(self._config.window_width, self._config.window_height)
        self.move(self._config.window_x, self._config.window_y)
        if self._config.maximized:
            self.showMaximized()

    def save_window_state(self) -> None:
        """Save current window geometry to config."""
        if not self.isMaximized():
            geo = self.geometry()
            self._config.window_width = geo.width()
            self._config.window_height = geo.height()
            self._config.window_x = geo.x()
            self._config.window_y = geo.y()
        self._config.maximized = self.isMaximized()

    def get_connected_ports(self) -> set[str]:
        """Return set of port names currently connected across all tabs."""
        ports = set()
        for i in range(self._tab_widget.count()):
            tab = self._tab_widget.widget(i)
            if isinstance(tab, DeviceTab) and tab.is_connected:
                ports.add(tab.port_name)
        return ports

    def shutdown_all_tabs(self) -> None:
        """Ordered shutdown of all tabs."""
        for i in range(self._tab_widget.count()):
            tab = self._tab_widget.widget(i)
            if isinstance(tab, DeviceTab):
                tab.ordered_shutdown()

    def closeEvent(self, event) -> None:
        """Handle window close with strategic confirmation.

        Flow:
          - No tabs / no connections → close silently
          - Connected devices (not logging) → confirm disconnect
          - Active logging → warn about data, confirm with emphasis
          - On confirm: ordered shutdown (flush → close → release)
          - On cancel: abort close, return to application
        """
        # Gather state across all tabs
        connected_ports: list[str] = []
        logging_ports: list[str] = []

        for i in range(self._tab_widget.count()):
            tab = self._tab_widget.widget(i)
            if isinstance(tab, DeviceTab) and tab.is_connected:
                connected_ports.append(tab.port_name)
                if tab.is_logging:
                    logging_ports.append(tab.port_name)

        # No active connections → close silently
        if not connected_ports:
            self._perform_shutdown()
            event.accept()
            return

        # Build confirmation dialog
        msg = QMessageBox(self)
        msg.setWindowTitle("Close WireTrace")
        msg.setIcon(QMessageBox.Icon.Warning)

        if logging_ports:
            # High risk — active logging
            n_log = len(logging_ports)
            port_list = ", ".join(logging_ports)
            msg.setText(
                f"Logging is active on {n_log} device(s): {port_list}"
            )
            msg.setInformativeText(
                "All log data will be flushed and saved before closing.\n\n"
                "Are you sure you want to exit?"
            )
        else:
            # Low risk — connected but not logging
            n_con = len(connected_ports)
            port_list = ", ".join(connected_ports)
            msg.setText(
                f"{n_con} device(s) connected: {port_list}"
            )
            msg.setInformativeText(
                "All connections will be closed.\n\n"
                "Are you sure you want to exit?"
            )

        cancel_btn = msg.addButton(
            "Cancel", QMessageBox.ButtonRole.RejectRole
        )
        exit_btn = msg.addButton(
            "Exit", QMessageBox.ButtonRole.AcceptRole
        )
        msg.setDefaultButton(cancel_btn)
        msg.exec()

        if msg.clickedButton() != exit_btn:
            event.ignore()
            return

        self._perform_shutdown()
        event.accept()

    def _perform_shutdown(self) -> None:
        """Save state and perform ordered shutdown of all tabs."""
        self.save_window_state()
        self._config.save()
        self.shutdown_all_tabs()
