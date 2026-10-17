# SPDX-License-Identifier: GPL-3.0-or-later
"""Makes the console readable by assistive technology.

The console draws its own text, so a screen reader cannot read it the
way it reads a standard text box. This module describes the console to
the platform's accessibility layer: its role, its name, and the lines
currently on screen as its value.
"""

from __future__ import annotations

from PySide6.QtCore import QObject
from PySide6.QtGui import QAccessible, QAccessibleInterface
from PySide6.QtWidgets import QAccessibleWidget

from ui.widgets.console_view import ConsoleView

#: Interfaces handed to Qt. Qt does not take ownership of an object
#: created in Python, so they are kept alive here, one per console.
_interfaces: dict[int, QAccessibleInterface] = {}
_installed = False


class _ConsoleAccessible(QAccessibleWidget):
    """Accessibility description of one console."""

    def __init__(self, console: ConsoleView) -> None:
        super().__init__(console, QAccessible.Role.Terminal, console.accessibleName())
        self._console = console

    def text(self, kind: QAccessible.Text) -> str:
        if kind == QAccessible.Text.Value:
            return "\n".join(self._console.visible_lines())
        return super().text(kind)


def _create(_class_name: str, target: QObject) -> QAccessibleInterface | None:
    if not isinstance(target, ConsoleView):
        return None
    key = id(target)
    interface = _interfaces.get(key)
    if interface is None:
        interface = _ConsoleAccessible(target)
        _interfaces[key] = interface
        target.destroyed.connect(lambda: _interfaces.pop(key, None))
    return interface


def install_console_accessibility() -> None:
    """Register the console's description with Qt. Safe to call more than once."""
    global _installed
    if not _installed:
        QAccessible.installFactory(_create)
        _installed = True
