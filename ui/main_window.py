# SPDX-License-Identifier: GPL-3.0-or-later
"""WireTrace main window: the tabs and the menus.

Responsibilities:
  - One tab per device, each with a status dot and a name that can be
    changed in place
  - Menus organised by task (File, Edit, View, Device, Help); every
    shortcut comes from ``ui.shortcuts``
  - Keeping the menus in step with the tab in front
  - Saving and restoring the window's place on screen
  - An orderly shutdown of every tab on exit

This module does not touch serial I/O or log files.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPoint, QSize, Qt, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QCloseEvent,
    QDesktopServices,
    QGuiApplication,
    QKeySequence,
    QMouseEvent,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QLineEdit,
    QMainWindow,
    QMenu,
    QTabBar,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.config import ConfigManager
from app.constants import (
    DEFAULT_FONT_SIZE,
    FONT_SIZE_MAX,
    FONT_SIZE_MIN,
    FONT_SIZE_STEP,
    PORT_TAB_NAME_MAX,
    SAVED_COMMAND_SHORTCUTS,
    UPDATE_SNOOZE_HOURS,
    USAGE_QUESTION_MAX_SHOWINGS,
    DisplayMode,
    TimestampMode,
)
from core.device_pipeline import TransportFactory
from core.transport import open_serial_transport
from ui import shortcuts
from ui.device_tab import NEW_TAB_TITLE, DeviceTab
from ui.dialogs.messages import confirm, tell
from ui.formatting import format_count
from ui.themes import tokens
from ui.themes.icons import bind_icon
from ui.themes.theme_manager import ThemeManager, normalise_preference
from ui.widgets import banner as banners
from ui.widgets.banner import BannerAction, BannerArea
from ui.widgets.button import Button
from ui.widgets.command_bar import LINE_ENDING_NAMES
from ui.widgets.least_size import keep_least_size
from ui.widgets.status_dot import state_icon
from ui.widgets.styling import plain_tooltip, set_compact
from ui.widgets.toast import Toast
from ui.window_geometry import visible_geometry
from updater.install import UpdateApplyError, launch_update, prepare_update
from updater.manifest import PlatformRelease, ReleaseManifest
from updater.update_dialog import UpdateAnswer, UpdateDialog
from updater.update_manager import UpdateManager
from updater.usage import Feature, Problem, UsageStatistics
from version import APP_ISSUES_URL, APP_NAME, APP_PRIVACY_URL, APP_VERSION, WINDOW_TITLE

logger = logging.getLogger(__name__)

#: The key of the bar that asks about usage statistics.
_BANNER_USAGE = "usage"


class TabBar(QTabBar):
    """Tab bar with a New tab button after the last tab and renaming in place.

    Signals:
        new_tab_requested():  The New tab button was pressed.
        renamed(int, str):  A tab was given a new name.
    """

    new_tab_requested = Signal()
    renamed = Signal(int, str)

    def __init__(self, parent: QTabWidget | None = None) -> None:
        super().__init__(parent)
        metrics = tokens.METRICS

        self._add = Button(parent=self, compact=True)
        self._add.setObjectName("flatIconButton")
        self._add.setToolTip(shortcuts.with_keys("New tab", "new_tab"))
        self._add.setAccessibleName("New tab")
        bind_icon(self._add, "add_tab", "text_secondary")
        self._add.clicked.connect(self.new_tab_requested)

        self._editor = QLineEdit(self)
        self._editor.setObjectName("tabNameEditor")
        set_compact(self._editor)
        self._editor.setMaxLength(PORT_TAB_NAME_MAX)
        self._editor.setPlaceholderText("Tab name")
        self._editor.setToolTip("Enter to rename, Esc to cancel")
        self._editor.setAccessibleName("Tab name")
        self._editor.hide()
        self._editor.installEventFilter(self)
        self._editing = -1

        # Set last: each of these makes Qt lay the tabs out, which
        # places the button created above.
        self.setExpanding(False)
        self.setDrawBase(False)
        self.setElideMode(Qt.TextElideMode.ElideRight)
        self.setIconSize(QSize(metrics.status_dot, metrics.status_dot))

    # ── New tab button ───────────────────────────────────────────────────

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        return QSize(hint.width() + self._add.width() + 2 * tokens.METRICS.space_2, hint.height())

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        return QSize(hint.width() + self._add.width() + 2 * tokens.METRICS.space_2, hint.height())

    def tabLayoutChange(self) -> None:
        super().tabLayoutChange()
        self._place_add_button()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._place_add_button()

    def _place_add_button(self) -> None:
        """Put the button after the last tab, inside the bar."""
        space = tokens.METRICS.space_2
        left = self.tabRect(self.count() - 1).right() + space if self.count() else space
        # When there are more tabs than fit, Qt shows two scroll arrows
        # at the end of the bar; the button goes just before them.
        limit = self.width()
        for arrow in self.findChildren(QToolButton):
            if arrow.isVisible():
                limit = min(limit, arrow.x())
        left = max(space, min(left, limit - self._add.width() - space))
        self._add.move(left, (self.height() - self._add.height()) // 2)
        self._add.raise_()

    # ── Renaming ─────────────────────────────────────────────────────────

    @property
    def editing(self) -> bool:
        return self._editing >= 0

    def start_rename(self, index: int, current_name: str) -> None:
        """Put a text box over tab ``index`` holding its name."""
        if not 0 <= index < self.count():
            return
        self.finish_rename(keep=False)
        space = tokens.METRICS.space_1
        self._editing = index
        self._editor.setGeometry(self.tabRect(index).adjusted(space, space, -space, -space))
        self._editor.setText(current_name)
        self._editor.selectAll()
        self._editor.show()
        self._editor.raise_()
        self._editor.setFocus()

    def finish_rename(self, *, keep: bool) -> None:
        index, self._editing = self._editing, -1
        if index < 0:
            return
        self._editor.hide()
        if keep:
            self.renamed.emit(index, self._editor.text().strip())

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._editor and self.editing:
            kind = event.type()
            if kind == QEvent.Type.ShortcutOverride:
                # Esc is also a shortcut of the window. While a name is
                # being typed it belongs to the text box.
                if event.key() == Qt.Key.Key_Escape:
                    event.accept()
                    return True
            elif kind == QEvent.Type.KeyPress:
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    self.finish_rename(keep=True)
                    return True
                if event.key() == Qt.Key.Key_Escape:
                    self.finish_rename(keep=False)
                    return True
            elif kind == QEvent.Type.FocusOut:
                self.finish_rename(keep=True)
        return super().eventFilter(watched, event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        index = self.tabAt(event.position().toPoint())
        if index >= 0 and event.button() == Qt.MouseButton.LeftButton:
            self.parent().rename_requested.emit(index)
        else:
            super().mouseDoubleClickEvent(event)


class TabWidget(QTabWidget):
    """The tabs of the main window, using ``TabBar``."""

    rename_requested = Signal(int)

    def __init__(self, parent: QMainWindow | None = None) -> None:
        super().__init__(parent)
        self.bar = TabBar(self)
        self.setTabBar(self.bar)
        self.setTabsClosable(True)
        self.setMovable(True)
        self.setDocumentMode(True)


class MainWindow(QMainWindow):
    """The WireTrace window: device tabs under a menu bar."""

    def __init__(
        self,
        config: ConfigManager,
        theme_manager: ThemeManager,
        parent: QMainWindow | None = None,
        transport_factory: TransportFactory = open_serial_transport,
        updates: UpdateManager | None = None,
        usage: UsageStatistics | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._theme_manager = theme_manager
        self._transport_factory = transport_factory
        self._updates = updates if updates is not None else UpdateManager(config, self)
        self._updates.update_available.connect(self._on_update_available)
        self._updates.up_to_date.connect(
            lambda: self._notify(Toast.success, f"{APP_NAME} {APP_VERSION} is the latest version")
        )
        self._updates.check_failed.connect(self._on_update_check_failed)
        self._usage = usage if usage is not None else UsageStatistics(config, self)
        self._exit_confirmed = False

        self.setWindowTitle(WINDOW_TITLE)
        keep_least_size(self, tokens.METRICS.window_min_width, tokens.METRICS.window_min_height)

        self._setup_tabs()
        self._setup_menus()
        self._restore_window_state()

        self._theme_manager.theme_changed.connect(self._refresh_tab_icons)
        self.new_tab()

    # ── Tabs ─────────────────────────────────────────────────────────────

    def _setup_tabs(self) -> None:
        self._tabs = TabWidget(self)
        self._tabs.tabCloseRequested.connect(self.close_tab)
        self._tabs.currentChanged.connect(self._on_current_tab_changed)
        self._tabs.rename_requested.connect(self.rename_tab)
        bar = self._tabs.bar
        bar.new_tab_requested.connect(self.new_tab)
        bar.renamed.connect(self._on_tab_renamed)
        bar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        bar.customContextMenuRequested.connect(self._show_tab_menu)

        # Messages that concern the whole window sit above the tabs.
        self._window_banners = BannerArea()
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._window_banners)
        layout.addWidget(self._tabs, 1)
        self.setCentralWidget(central)

    def tabs(self) -> list[DeviceTab]:
        """Every tab, left to right.

        While a tab is being closed, Qt has already taken its page away
        but still counts it on the tab bar, and shows the next page in
        between; a tab without a page is not counted.
        """
        pages = (self._tabs.widget(index) for index in range(self._tabs.count()))
        return [page for page in pages if page is not None]

    def current_tab(self) -> DeviceTab | None:
        return self._tabs.currentWidget()

    def new_tab(self) -> DeviceTab:
        """Open a tab on its first screen and bring it to the front."""
        tab = DeviceTab(self._theme_manager, self._config, self, self._transport_factory)
        for signal in (
            tab.title_changed,
            tab.connection_changed,
            tab.link_state_changed,
            tab.timestamp_mode_changed,
            tab.display_mode_changed,
            tab.following_changed,
            tab.line_ending_changed,
            tab.plot_visibility_changed,
        ):
            signal.connect(lambda *_args, t=tab: self._on_tab_state_changed(t))
        tab.usage_noted.connect(self._note_usage)
        tab.line_rate_measured.connect(self._usage.note_line_rate)
        tab.connection_changed.connect(
            lambda connected: self._usage.note_connection() if connected else None
        )
        index = self._tabs.addTab(tab, tab.title)
        self._show_tab_state(tab)
        self._tabs.setCurrentIndex(index)
        if self._tabs.count() > 1:
            self._usage.note_feature(Feature.TABS)
        return tab

    def close_tab(self, index: int) -> None:
        """Close a tab, asking first if that would end a log or an export.

        The window always has a tab: closing the last one opens a new one.
        """
        tab = self._tabs.widget(index)
        if tab is None or not tab.confirm_close():
            return
        self._tabs.bar.finish_rename(keep=False)
        tab.ordered_shutdown()
        self._tabs.removeTab(index)
        tab.deleteLater()
        if self._tabs.count() == 0:
            self.new_tab()

    def close_current_tab(self) -> None:
        self.close_tab(self._tabs.currentIndex())

    def rename_tab(self, index: int) -> None:
        """Let the user type a name for the tab, on the tab itself."""
        tab = self._tabs.widget(index)
        if tab is not None:
            self._tabs.bar.start_rename(index, tab.custom_name)

    def rename_current_tab(self) -> None:
        self.rename_tab(self._tabs.currentIndex())

    def _on_tab_renamed(self, index: int, name: str) -> None:
        tab = self._tabs.widget(index)
        if tab is not None:
            tab.rename(name)

    def _show_tab_menu(self, position: QPoint) -> None:
        bar = self._tabs.bar
        index = bar.tabAt(position)
        if index < 0:
            return
        menu = QMenu(self)
        menu.addAction(
            shortcuts.menu_text("Rename", "rename_tab"), lambda: self.rename_tab(index),
        )
        menu.addSeparator()
        menu.addAction(
            shortcuts.menu_text("Close", "close_tab"), lambda: self.close_tab(index),
        )
        menu.exec(bar.mapToGlobal(position))
        menu.deleteLater()

    def _show_tab_state(self, tab: DeviceTab) -> None:
        """Bring the tab's dot, name and tooltip up to date."""
        index = self._tabs.indexOf(tab)
        if index < 0:
            return
        self._tabs.setTabIcon(index, state_icon(tab.link_state, self.devicePixelRatioF()))
        self._tabs.setTabText(index, tab.title)
        self._tabs.setTabToolTip(index, plain_tooltip(tab.title))

    def _refresh_tab_icons(self) -> None:
        for tab in self.tabs():
            self._show_tab_state(tab)

    def _on_tab_state_changed(self, tab: DeviceTab) -> None:
        self._show_tab_state(tab)
        if tab is self.current_tab():
            self._sync_with_current_tab()

    def _on_current_tab_changed(self, _index: int) -> None:
        self._tabs.bar.finish_rename(keep=True)
        self._sync_with_current_tab()

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.DevicePixelRatioChange:
            # The window moved to a screen with another scaling.
            self._refresh_tab_icons()
        return super().event(event)

    # ── Menus ────────────────────────────────────────────────────────────

    def _add_action(
        self,
        menu: QMenu,
        text: str,
        run: Callable[[], None],
        shortcut: str = "",
        *,
        checkable: bool = False,
        role: QAction.MenuRole = QAction.MenuRole.NoRole,
    ) -> QAction:
        """Add an entry to ``menu``. ``shortcut`` is a name in ``ui.shortcuts``."""
        action = menu.addAction(text)
        # Without a role, macOS moves entries into its application menu
        # by guessing from their text.
        action.setMenuRole(role)
        action.setCheckable(checkable)
        if shortcut:
            action.setShortcuts([
                QKeySequence(keys)
                for keys in (shortcuts.keys(shortcut), *shortcuts.ALTERNATE_KEYS.get(shortcut, ()))
            ])
            self._shortcut_actions[shortcut] = action
        action.triggered.connect(lambda _checked=False: run())
        return action

    def _add_choices(
        self,
        menu: QMenu,
        choices: list[tuple[str, object]],
        choose: Callable[[object], None],
    ) -> dict[object, QAction]:
        """Add entries of which exactly one is ticked."""
        group = QActionGroup(menu)
        group.setExclusive(True)
        actions: dict[object, QAction] = {}
        for text, value in choices:
            action = self._add_action(menu, text, lambda v=value: choose(v), checkable=True)
            group.addAction(action)
            actions[value] = action
        return actions

    def _setup_menus(self) -> None:
        self._shortcut_actions: dict[str, QAction] = {}
        bar = self.menuBar()
        self._setup_file_menu(bar.addMenu("&File"))
        self._setup_edit_menu(bar.addMenu("&Edit"))
        self._setup_view_menu(bar.addMenu("&View"))
        self._setup_device_menu(bar.addMenu("&Device"))
        self._setup_help_menu(bar.addMenu("&Help"))

    def shortcut_actions(self) -> dict[str, QAction]:
        """The action behind each name in ``ui.shortcuts``."""
        return dict(self._shortcut_actions)

    def _setup_file_menu(self, menu: QMenu) -> None:
        add = self._add_action
        add(menu, "New tab", self.new_tab, "new_tab")
        add(menu, "Close tab", self.close_current_tab, "close_tab")
        menu.addSeparator()
        self._act_start_log = add(menu, "Start log...", self._for_tab(DeviceTab.start_log), "start_log")
        self._act_stop_log = add(menu, "Stop log", self._for_tab(DeviceTab.stop_log))
        self._act_log_folder = add(menu, "Open log folder", self._for_tab(DeviceTab.open_log_folder))
        menu.addSeparator()
        self._act_export = add(
            menu, "Export session...", self._for_tab(DeviceTab.export_session), "export",
        )
        menu.addSeparator()
        add(
            menu, "Preferences...", self._open_preferences, "preferences",
            role=QAction.MenuRole.PreferencesRole,
        )
        menu.addSeparator()
        add(menu, "Exit", self.close, "quit", role=QAction.MenuRole.QuitRole)

    def _setup_edit_menu(self, menu: QMenu) -> None:
        add = self._add_action
        self._act_copy = add(menu, "Copy", self._copy, "copy")
        self._act_copy_messages = add(
            menu, "Copy without timestamps", self._copy_messages, "copy_messages",
        )
        self._act_select_all = add(menu, "Select all", self._select_all, "select_all")
        menu.addSeparator()
        self._act_find = add(menu, "Find...", self._for_tab(DeviceTab.activate_search), "find")
        self._act_find_next = add(menu, "Find next", self._for_tab(DeviceTab.search_next), "find_next")
        self._act_find_previous = add(
            menu, "Find previous", self._for_tab(DeviceTab.search_prev), "find_previous",
        )
        self._act_filter = add(menu, "Filter lines", self._for_tab(DeviceTab.activate_filter), "filter")
        self._act_close_find = add(
            menu, "Close Find and clear the filter",
            self._for_tab(DeviceTab.clear_search_filter), "close_find",
        )
        menu.addSeparator()
        self._act_clear = add(menu, "Clear console", self._for_tab(DeviceTab.clear_console))
        # Copy and Select all are switched off in the open menu when
        # there is nothing for them to act on, and back on when it
        # closes so that their shortcuts always reach the handler.
        menu.aboutToShow.connect(self._sync_edit_menu)
        menu.aboutToHide.connect(self._release_edit_menu)

    def _setup_view_menu(self, menu: QMenu) -> None:
        add = self._add_action
        self._theme_actions = self._add_choices(
            menu.addMenu("Theme"),
            [(name, preference) for preference, name in self._theme_manager.available_preferences()],
            self._choose_theme,
        )
        self._theme_actions[self._theme_manager.preference].setChecked(True)
        menu.addSeparator()

        self._timestamp_actions = self._add_choices(
            menu.addMenu("Timestamps"),
            [("Time of day", TimestampMode.ABSOLUTE), ("Since previous line", TimestampMode.RELATIVE)],
            lambda mode: self._for_tab(DeviceTab.set_timestamp_mode, mode)(),
        )
        self._display_menu = menu.addMenu("Display")
        self._display_actions = self._add_choices(
            self._display_menu,
            [("Text", DisplayMode.TEXT), ("HEX", DisplayMode.HEX)],
            lambda mode: self._for_tab(DeviceTab.set_display_mode, mode)(),
        )
        self._act_follow = add(menu, "Follow new lines", self._toggle_following, checkable=True)
        self._act_hide_blank = add(menu, "Hide blank lines", self._toggle_hide_blank, checkable=True)
        self._act_hide_blank.setChecked(self._config.hide_blank_lines)
        menu.addSeparator()

        self._act_plot = add(menu, "Live plot", self._for_tab(DeviceTab.toggle_plot), "plot", checkable=True)
        self._act_configure_plot = add(
            menu, "Configure plot...", self._for_tab(DeviceTab.open_plot_config_dialog),
        )
        menu.addSeparator()

        add(menu, "Larger text", lambda: self._change_text_size(FONT_SIZE_STEP), "text_larger")
        add(menu, "Smaller text", lambda: self._change_text_size(-FONT_SIZE_STEP), "text_smaller")
        add(menu, "Reset text size", self._reset_text_size, "text_reset")

    def _setup_device_menu(self, menu: QMenu) -> None:
        add = self._add_action
        self._act_connect = add(menu, "Connect", self._for_tab(DeviceTab.connect_device))
        self._act_disconnect = add(menu, "Disconnect", self._for_tab(DeviceTab.disconnect_device))
        self._act_refresh = add(
            menu, "Refresh ports", self._for_tab(DeviceTab.refresh_ports), "refresh_ports",
        )
        menu.addSeparator()
        self._line_ending_actions = self._add_choices(
            menu.addMenu("Line ending for commands"),
            [(name, ending) for ending, name in LINE_ENDING_NAMES.items()],
            lambda ending: self._for_tab(DeviceTab.set_line_ending, ending)(),
        )
        # The shortcuts of the saved commands belong to the window, not
        # to menu entries: the menu lists whatever is saved when it
        # opens, while a shortcut must work whether or not it has been.
        for number in range(1, SAVED_COMMAND_SHORTCUTS + 1):
            name = shortcuts.saved_command_name(number)
            action = QAction(self)
            action.setShortcut(QKeySequence(shortcuts.keys(name)))
            fill = self._for_tab(DeviceTab.fill_saved_command, number)
            action.triggered.connect(lambda _checked=False, run=fill: run())
            self.addAction(action)
            self._shortcut_actions[name] = action
        self._saved_menu = menu.addMenu("Saved commands")
        self._saved_menu.aboutToShow.connect(self._fill_saved_menu)
        self._fill_saved_menu()
        menu.addSeparator()
        add(menu, "Rename tab", self.rename_current_tab, "rename_tab")

    def _fill_saved_menu(self) -> None:
        """List the saved commands as they are now."""
        menu = self._saved_menu
        menu.clear()
        tab = self.current_tab()
        commands = tab.saved_commands.all() if tab is not None else []
        for number, command in enumerate(commands, start=1):
            text = command.replace("&", "&&")
            if number <= SAVED_COMMAND_SHORTCUTS:
                text = shortcuts.menu_text(text, shortcuts.saved_command_name(number))
            self._add_action(menu, text, self._for_tab(DeviceTab.fill_saved_command, number))
        if commands:
            menu.addSeparator()
        self._add_action(
            menu, "Edit saved commands...", self._for_tab(DeviceTab.edit_saved_commands),
        )

    def _setup_help_menu(self, menu: QMenu) -> None:
        add = self._add_action
        add(menu, "User guide", self._open_user_guide, "user_guide")
        add(menu, "Keyboard shortcuts", self._show_shortcuts)
        menu.addSeparator()
        self._act_update = add(menu, "Check for updates...", self.check_for_updates)
        add(menu, "Report a problem", self._report_problem)
        menu.addSeparator()
        add(menu, f"About {APP_NAME}", self._show_about, role=QAction.MenuRole.AboutRole)

    def _for_tab(self, method: Callable[..., None], *args: object) -> Callable[[], None]:
        """A menu handler that calls ``method`` on the tab in front."""
        def run() -> None:
            tab = self.current_tab()
            if tab is not None:
                method(tab, *args)
        return run

    # ── Keeping the menus in step with the tab in front ──────────────────

    def _sync_with_current_tab(self) -> None:
        tab = self.current_tab()
        if tab is None:
            return
        connected, logging, session = tab.is_connected, tab.is_logging, tab.has_session

        self._act_start_log.setEnabled(connected and not logging)
        self._act_stop_log.setEnabled(logging)
        self._act_export.setEnabled(session)

        for action in (
            self._act_find, self._act_find_next, self._act_find_previous,
            self._act_filter, self._act_close_find, self._act_clear,
            self._act_follow, self._act_copy_messages,
        ):
            action.setEnabled(session)
        for action in self._timestamp_actions.values():
            action.setEnabled(session)
        for action in self._line_ending_actions.values():
            action.setEnabled(session)
        self._display_menu.setEnabled(connected)
        self._act_plot.setEnabled(connected)

        self._act_connect.setEnabled(not connected)
        self._act_disconnect.setEnabled(connected)
        self._act_refresh.setEnabled(not connected)

        self._timestamp_actions[tab.session.timestamp_mode].setChecked(True)
        self._display_actions[tab.session.display_mode].setChecked(True)
        self._line_ending_actions[tab.line_ending].setChecked(True)
        self._act_follow.setChecked(tab.following)
        self._act_plot.setChecked(tab.is_plot_visible())

        title = tab.title
        self.setWindowTitle(WINDOW_TITLE if title == NEW_TAB_TITLE else f"{title} - {WINDOW_TITLE}")

    # ── Edit ─────────────────────────────────────────────────────────────

    @staticmethod
    def _focused_text_field() -> QLineEdit | None:
        """The text box the user is typing in, if any."""
        widget = QApplication.focusWidget()
        if isinstance(widget, QLineEdit):
            return widget
        if isinstance(widget, QComboBox) and widget.isEditable():
            return widget.lineEdit()
        return None

    def _copy(self) -> None:
        field = self._focused_text_field()
        tab = self.current_tab()
        if field is not None:
            field.copy()
        elif tab is not None:
            tab.console.copy()

    def _copy_messages(self) -> None:
        tab = self.current_tab()
        if tab is not None:
            tab.console.copy_messages()

    def _select_all(self) -> None:
        field = self._focused_text_field()
        tab = self.current_tab()
        if field is not None:
            field.selectAll()
        elif tab is not None and tab.has_session:
            tab.console.select_all()

    def _sync_edit_menu(self) -> None:
        field = self._focused_text_field()
        tab = self.current_tab()
        console = tab.console if tab is not None and tab.has_session else None
        selected = console is not None and console.has_selection
        if field is not None:
            self._act_copy.setEnabled(field.hasSelectedText())
            self._act_select_all.setEnabled(bool(field.text()))
        else:
            self._act_copy.setEnabled(selected)
            self._act_select_all.setEnabled(console is not None and console.visible_count > 0)
        self._act_copy_messages.setEnabled(selected)

    def _release_edit_menu(self) -> None:
        tab = self.current_tab()
        self._act_copy.setEnabled(True)
        self._act_select_all.setEnabled(True)
        self._act_copy_messages.setEnabled(tab is not None and tab.has_session)

    # ── View ─────────────────────────────────────────────────────────────

    def _choose_theme(self, preference: str) -> None:
        self._theme_manager.apply_preference(preference)
        self._config.theme = self._theme_manager.preference
        self._theme_actions[self._theme_manager.preference].setChecked(True)

    def _toggle_following(self) -> None:
        tab = self.current_tab()
        if tab is not None:
            tab.set_following(self._act_follow.isChecked())

    def _toggle_hide_blank(self) -> None:
        self._config.hide_blank_lines = self._act_hide_blank.isChecked()
        self._apply_preferences_to_tabs()

    def _change_text_size(self, change: int) -> None:
        size = max(FONT_SIZE_MIN, min(FONT_SIZE_MAX, self._config.font_size + change))
        if size != self._config.font_size:
            self._config.font_size = size
            self._apply_preferences_to_tabs()

    def _reset_text_size(self) -> None:
        self._config.font_size = DEFAULT_FONT_SIZE
        self._apply_preferences_to_tabs()

    def _apply_preferences_to_tabs(self) -> None:
        for tab in self.tabs():
            tab.apply_preferences()

    def _open_preferences(self) -> None:
        from ui.dialogs.preferences_dialog import PreferencesDialog

        dialog = PreferencesDialog(
            self._config, self._theme_manager, self,
            open_plot_config=self._for_tab(DeviceTab.open_plot_config_dialog),
        )
        sharing = self._config.usage_statistics
        saved = dialog.exec() == PreferencesDialog.DialogCode.Accepted

        # The dialog may have changed any of these, by Save or by
        # Restore defaults, which takes effect even if it is then cancelled.
        theme = normalise_preference(self._config.theme)
        if theme != self._theme_manager.preference:
            self._theme_manager.apply_preference(theme)
        self._theme_actions[self._theme_manager.preference].setChecked(True)
        self._act_hide_blank.setChecked(self._config.hide_blank_lines)
        self._apply_preferences_to_tabs()
        if self._config.usage_statistics != sharing:
            self._usage.apply_setting()
        if saved:
            self._notify(Toast.success, "Preferences saved")

    # ── Help ─────────────────────────────────────────────────────────────

    def _notify(
        self,
        show: Callable[..., Toast],
        message: str,
        action_label: str = "",
        action: Callable[[], None] | None = None,
    ) -> None:
        """Show a toast over the tab in front, with a button if one is given."""
        tab = self.current_tab()
        if tab is not None:
            show(tab, message, action_label, action)

    def _open_user_guide(self) -> None:
        """Open the user guide in the system's browser."""
        from app.help_loader import UserGuideError, UserGuideNotFoundError, prepare_user_guide

        try:
            path = prepare_user_guide()
        except UserGuideNotFoundError:
            logger.exception("User guide source not found")
            self._notify(Toast.error, f"The user guide is not installed. Please reinstall {APP_NAME}.")
            return
        except UserGuideError:
            logger.exception("User guide could not be filled in")
            self._notify(Toast.error, f"The user guide is damaged. Please reinstall {APP_NAME}.")
            return
        except OSError:
            logger.exception("Failed to write the user guide to the cache")
            self._notify(Toast.error, "The user guide could not be prepared. Check the disk permissions.")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            self._notify(Toast.error, "No browser is set up to open the user guide.")

    def _show_shortcuts(self) -> None:
        from ui.dialogs.shortcuts_dialog import ShortcutsDialog

        ShortcutsDialog(self).exec()

    def _report_problem(self) -> None:
        if not QDesktopServices.openUrl(QUrl(APP_ISSUES_URL)):
            self._notify(Toast.warning, f"The page could not be opened: {APP_ISSUES_URL}")

    def _show_about(self) -> None:
        from ui.dialogs.about_dialog import AboutDialog

        AboutDialog(self._theme_manager, self).exec()

    # ── Usage statistics ─────────────────────────────────────────────────

    @property
    def usage(self) -> UsageStatistics:
        return self._usage

    def _note_usage(self, item: object) -> None:
        if isinstance(item, Feature):
            self._usage.note_feature(item)
        elif isinstance(item, Problem):
            self._usage.note_problem(item)

    def ask_about_usage_statistics(self) -> None:
        """Ask, once, whether usage statistics may be shared.

        The question is a bar above the tabs that blocks nothing. It is
        shown on a later start once more only if the window was closed
        without an answer; after that the setting is in Preferences.
        """
        config = self._config
        if (
            config.usage_question_answered
            or config.usage_question_shown >= USAGE_QUESTION_MAX_SHOWINGS
        ):
            return
        config.usage_question_shown += 1
        config.save()
        banner = self._window_banners.show_banner(
            _BANNER_USAGE, banners.INFO,
            f"Help improve {APP_NAME}?",
            "Share usage statistics, which you can switch off and delete at any time. Nothing your devices send is ever included.",
            (
                BannerAction("What is sent", self._show_privacy_policy),
                BannerAction("Share", lambda: self._answer_usage_question(share=True)),
                BannerAction("No thanks", lambda: self._answer_usage_question(share=False)),
            ),
        )
        banner.dismissed.connect(lambda: self._answer_usage_question(share=False))

    def _answer_usage_question(self, *, share: bool) -> None:
        self._config.usage_question_answered = True
        self._window_banners.clear(_BANNER_USAGE)
        self._usage.set_enabled(share)
        if share:
            self._notify(
                Toast.success,
                "Thank you. Usage statistics are shared; you can change this in Preferences.",
            )

    def _show_privacy_policy(self) -> None:
        if not QDesktopServices.openUrl(QUrl(APP_PRIVACY_URL)):
            self._notify(Toast.warning, f"The page could not be opened: {APP_PRIVACY_URL}")

    # ── Updates ──────────────────────────────────────────────────────────

    @property
    def updates(self) -> UpdateManager:
        return self._updates

    def check_for_updates(self) -> None:
        """Offer the update already found, or look for one now."""
        found = self._updates.found
        if found is not None:
            self._offer_update(found)
            return
        self._notify(Toast.info, "Checking for updates")
        self._updates.check_now()

    def _on_update_available(self, manifest: ReleaseManifest, asked: bool) -> None:
        """Offer a newer release.

        A check the user asked for opens the update window. One made in
        the background is announced by a message, so that a window never
        opens over work in progress; the Help menu keeps the offer.
        """
        self._act_update.setText(f"Update to {manifest.version}...")
        if asked:
            self._offer_update(manifest)
        else:
            self._notify(
                Toast.info, f"{APP_NAME} {manifest.version} is available",
                "See what is new", lambda: self._offer_update(manifest),
            )

    def _offer_update(self, manifest: ReleaseManifest) -> None:
        dialog = UpdateDialog(manifest, self._updates, self)
        dialog.install_requested.connect(self.install_update)
        dialog.exec()
        if dialog.answer is UpdateAnswer.SNOOZED:
            self._notify(
                Toast.info,
                f"You will be reminded about this update in {UPDATE_SNOOZE_HOURS} hours",
            )
        elif dialog.answer is UpdateAnswer.SKIPPED:
            self._notify(
                Toast.info,
                f"Version {manifest.version} will not be announced again. "
                "It stays in the Help menu.",
            )

    def _on_update_check_failed(self, reason: str) -> None:
        self._notify(Toast.error, f"The update check failed: {reason}")

    def install_update(self, installer: Path, release: PlatformRelease) -> None:
        """Install a verified download and close. ``release`` is what the
        signed manifest says the download must be.

        The part that can fail is done while the window is still open.
        Sessions are then closed as on any exit, asking first if that
        would end a connection, a log or an export.
        """
        if not self._may_exit():
            return
        try:
            prepare_update(installer, release)
        except UpdateApplyError as exc:
            logger.error("The update could not be put in place: %s", exc)
            self._notify(Toast.error, f"The update could not be installed: {exc}")
            return
        self._exit_confirmed = True
        self.close()
        try:
            launch_update(installer, release)
        except UpdateApplyError as exc:
            logger.error("The update could not be started: %s", exc)
            tell(None, "Update", f"The update could not be started: {exc}\n\n{installer}")
        QApplication.quit()

    # ── Window state ─────────────────────────────────────────────────────────

    def _restore_window_state(self) -> None:
        """Restore window geometry from config, clamped to a live screen.

        Guards against a saved position that is no longer visible, such as a
        coordinate on an external monitor that has since been disconnected.
        Without this, the window is placed off-screen and shows only as a
        taskbar entry. If the saved geometry is not visible on any connected
        screen, the window is centred on the primary screen.
        """
        screens = [
            (g.x(), g.y(), g.width(), g.height())
            for g in (s.availableGeometry() for s in QGuiApplication.screens())
        ]
        primary = None
        ps = QGuiApplication.primaryScreen()
        if ps is not None:
            g = ps.availableGeometry()
            primary = (g.x(), g.y(), g.width(), g.height())

        x, y, w, h = visible_geometry(
            (
                self._config.window_x,
                self._config.window_y,
                self._config.window_width,
                self._config.window_height,
            ),
            screens,
            (self.minimumWidth(), self.minimumHeight()),
            primary,
        )
        self.resize(w, h)
        self.move(x, y)
        if self._config.maximized:
            self.showMaximized()

    def save_window_state(self) -> None:
        """Save current window geometry to config.

        Skips saving while minimized or maximized: in those states
        ``geometry()`` does not reflect a usable restore position (on
        Windows a minimized window reports -32000). The maximized flag is
        always recorded so the state is restored on next launch.
        """
        if not self.isMaximized() and not self.isMinimized():
            geo = self.geometry()
            self._config.window_width = geo.width()
            self._config.window_height = geo.height()
            self._config.window_x = geo.x()
            self._config.window_y = geo.y()
        self._config.maximized = self.isMaximized()


    # ── Closing ──────────────────────────────────────────────────────────

    def get_connected_ports(self) -> set[str]:
        """The ports that are open in any tab."""
        return {tab.port_name for tab in self.tabs() if tab.is_connected}

    def shutdown_all_tabs(self) -> None:
        """Close every connection and wait until every session is on disk."""
        self._usage.stop()
        for tab in self.tabs():
            tab.ordered_shutdown()

    def closeEvent(self, event: QCloseEvent) -> None:
        """Ask before closing would end a connection, a log or an export."""
        if not self._exit_confirmed and not self._may_exit():
            event.ignore()
            return
        self._updates.stop()
        self.save_window_state()
        self._config.save()
        self.shutdown_all_tabs()
        event.accept()

    def _may_exit(self) -> bool:
        """Whether the window may close: at once if nothing is running,
        otherwise only if the user agrees."""
        tabs = self.tabs()
        connected = [tab.port_name for tab in tabs if tab.is_connected]
        logging_ports = [tab.port_name for tab in tabs if tab.is_logging]
        exporting = any(tab.is_exporting for tab in tabs)
        if not connected and not exporting:
            return True
        return self._confirm_exit(connected, logging_ports, exporting)

    def _confirm_exit(self, connected: list[str], logging_ports: list[str], exporting: bool) -> bool:
        details: list[str] = []
        if logging_ports:
            text = (
                f"{format_count(len(logging_ports), 'device is', 'devices are')} "
                f"still logging: {', '.join(logging_ports)}"
            )
            details.append(
                "Exiting stops every log and closes every connection. "
                "Everything received so far is written to the files first."
            )
        elif connected:
            text = (
                f"{format_count(len(connected), 'device is', 'devices are')} "
                f"connected: {', '.join(connected)}"
            )
            details.append("Exiting closes every connection.")
        else:
            text = "An export is still being written"
        if exporting:
            details.append("Exiting now cancels the export and removes the unfinished file.")
        return confirm(
            self, f"Exit {APP_NAME}", text, detail="\n\n".join(details), proceed="Exit",
        )
