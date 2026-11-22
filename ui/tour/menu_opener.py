# SPDX-License-Identifier: GPL-3.0-or-later
"""Opens a menu of the menu bar for the tour, with the entry a step is
about chosen, as if the user had gone to it.

The menus open as they do for the user, drawn by the system's style,
and stay open while the step is shown. They take every key and click
while they are open, so the tour reads them here: Esc, the arrow keys
and Enter do what they do on the tour's card, and a click on the card
is passed to it; nothing in the menu can be chosen by accident.

A menu bar the system draws itself (macOS, and some Linux desktops)
cannot be opened for the user without stopping everything else until
it closes, so there the step only says where the entry is
(``where_words``).
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QAction, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QMenu, QMenuBar, QWidget

from version import APP_NAME

#: The roles of the entries macOS moves into the application's own menu.
_APPLICATION_MENU_ROLES = frozenset({
    QAction.MenuRole.PreferencesRole,
    QAction.MenuRole.AboutRole,
    QAction.MenuRole.AboutQtRole,
    QAction.MenuRole.QuitRole,
    QAction.MenuRole.ApplicationSpecificRole,
})

_MOUSE = frozenset({
    QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick,
    QEvent.Type.MouseMove, QEvent.Type.Wheel, QEvent.Type.Enter, QEvent.Type.Leave,
    QEvent.Type.HoverEnter, QEvent.Type.HoverMove, QEvent.Type.HoverLeave,
})
_KEYS = frozenset({QEvent.Type.KeyPress, QEvent.Type.KeyRelease, QEvent.Type.ShortcutOverride})


def entry_name(action: QAction) -> str:
    """An entry's name as the menu shows it, without its keys, its
    mnemonic mark or the dots of an entry that opens a window."""
    text = action.text().split("\t", 1)[0]
    return text.replace("&&", "\0").replace("&", "").replace("\0", "&").removesuffix("...")


def menu_path(bar: QMenuBar, action: QAction) -> list[QMenu] | None:
    """The menus to open, from the menu bar down, to show ``action``: the
    menus that hold it, and the menu it opens if it opens one. None if
    ``action`` is not in ``bar``."""
    holders: list[QMenu] = []
    current = action
    while bar not in current.associatedObjects():
        menus = [holder for holder in current.associatedObjects() if isinstance(holder, QMenu)]
        if not menus:
            return None
        holders.insert(0, menus[0])
        current = menus[0].menuAction()
    opened = action.menu()
    if isinstance(opened, QMenu):
        holders.append(opened)
    return holders


def where_words(bar: QMenuBar, action: QAction) -> str:
    """Where ``action`` is, in words, for a menu bar the system draws."""
    if action.menuRole() in _APPLICATION_MENU_ROLES:
        return f"It is in the {APP_NAME} menu, at the top of the screen."
    path = menu_path(bar, action) or []
    names = [entry_name(menu.menuAction()) for menu in path]
    if action.menu() is None:
        names.append(entry_name(action))
    if len(names) == 1:
        return f"It is the {names[0]} menu, at the top of the screen."
    return f"It is at {' > '.join(names)}, in the menu bar at the top of the screen."


def opens_in_window(bar: QMenuBar) -> bool:
    """Whether the menus of ``bar`` are drawn in the window, where the
    tour can open them."""
    return bar.isVisible() and not bar.isNativeMenuBar()


class MenuOpener(QObject):
    """The menus that show one entry, opened over ``window``.

    Signals:
        next_requested():   Enter or the right arrow was pressed.
        back_requested():   The left arrow was pressed.
        close_requested():  Esc was pressed.
        pressed(QPoint):    A button of the mouse went down outside the
                            menus, at this point of the screen.
        released(QPoint):   It came up again, at this point.
        shown():            One of the menus has opened.
    """

    next_requested = Signal()
    back_requested = Signal()
    close_requested = Signal()
    pressed = Signal(QPoint)
    released = Signal(QPoint)
    shown = Signal()

    def __init__(self, window: QWidget, bar: QMenuBar, action: QAction) -> None:
        super().__init__(window)
        self._window = window
        self._bar = bar
        self._action = action
        self._menus = menu_path(bar, action) or []
        self._closing = False
        #: Whether the system closed the menus while the step was shown,
        #: as it does when another application is brought forward.
        self._closed_by_system = False
        window.installEventFilter(self)
        for menu in self._menus:
            menu.aboutToHide.connect(self._on_hide)

    @property
    def menus(self) -> tuple[QMenu, ...]:
        return tuple(self._menus)

    def open(self) -> bool:
        """Open the menus and choose the entry. Returns whether they are open."""
        if not self._menus:
            return False
        self._closed_by_system = False
        for menu in self._menus:
            menu.installEventFilter(self)
        top = self._menus[0]
        self._bar.setActiveAction(top.menuAction())
        for holder, inner in zip(self._menus, self._menus[1:], strict=False):
            holder.setActiveAction(inner.menuAction())
        if self._action.menu() is None:
            self._menus[-1].setActiveAction(self._action)
        return self.is_open()

    def is_open(self) -> bool:
        return bool(self._menus) and all(menu.isVisible() for menu in self._menus)

    def title_rect(self, host: QWidget) -> QRect | None:
        """The title of the outermost menu in the menu bar, in ``host``'s
        coordinates."""
        if not self._menus or not host.isAncestorOf(self._bar):
            return None
        geometry = self._bar.actionGeometry(self._menus[0].menuAction())
        if not geometry.isValid():
            return None
        return geometry.translated(self._bar.mapTo(host, QPoint(0, 0)))

    def area(self, host: QWidget) -> QRect | None:
        """Where the open menus are, in ``host``'s coordinates."""
        area = QRect()
        for menu in self._menus:
            if menu.isVisible():
                area = area.united(QRect(host.mapFromGlobal(menu.pos()), menu.size()))
        return area if area.isValid() else None

    def close(self) -> None:
        """Close the menus and stop reading their keys and clicks."""
        self._closing = True
        self._window.removeEventFilter(self)
        for menu in reversed(self._menus):
            menu.aboutToHide.disconnect(self._on_hide)
            menu.removeEventFilter(self)
            if menu.isVisible():
                menu.hide()
        self._menus = []
        self.deleteLater()

    def _on_hide(self) -> None:
        if not self._closing:
            self._closed_by_system = True
            for menu in self._menus:
                menu.removeEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        kind = event.type()
        if watched is self._window:
            # Back to the window: the menus the system closed open again.
            if kind == QEvent.Type.WindowActivate and self._closed_by_system and not self._closing:
                self.open()
            return False
        if kind == QEvent.Type.Show:
            self.shown.emit()
            return False
        if kind in _KEYS:
            if kind == QEvent.Type.KeyPress and isinstance(event, QKeyEvent):
                self._key(event)
            event.accept()
            return True
        if kind in _MOUSE:
            if isinstance(event, QMouseEvent) and not self._in_menus(event.globalPosition().toPoint()):
                if kind == QEvent.Type.MouseButtonPress:
                    self.pressed.emit(event.globalPosition().toPoint())
                elif kind == QEvent.Type.MouseButtonRelease:
                    self.released.emit(event.globalPosition().toPoint())
            return True
        return False

    def _key(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.close_requested.emit()
        elif key == Qt.Key.Key_Left:
            self.back_requested.emit()
        elif key in (Qt.Key.Key_Right, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.next_requested.emit()

    def _in_menus(self, point: QPoint) -> bool:
        return any(menu.isVisible() and menu.geometry().contains(point) for menu in self._menus)
