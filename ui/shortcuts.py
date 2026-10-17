# SPDX-License-Identifier: GPL-3.0-or-later
"""Every keyboard shortcut of the application, defined once.

Menus, tooltips and the shortcut list in the Help menu all read from
this table, so a shortcut shown anywhere is the one that works.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.constants import SAVED_COMMAND_SHORTCUTS


@dataclass(frozen=True)
class Shortcut:
    """One action that can be reached from the keyboard."""

    keys: str           # in Qt's portable form; Ctrl is Command on macOS
    description: str
    group: str


_FILE, _EDIT, _VIEW, _DEVICE, _HELP = "File", "Edit", "View", "Device", "Help"


def saved_command_name(number: int) -> str:
    """The name, in the table, of the shortcut of saved command ``number``."""
    return f"saved_command_{number}"


SHORTCUTS: dict[str, Shortcut] = {
    "new_tab": Shortcut("Ctrl+T", "New tab", _FILE),
    "close_tab": Shortcut("Ctrl+W", "Close tab", _FILE),
    "start_log": Shortcut("Ctrl+N", "Start log", _FILE),
    "export": Shortcut("Ctrl+S", "Export session", _FILE),
    "preferences": Shortcut("Ctrl+,", "Preferences", _FILE),
    "quit": Shortcut("Ctrl+Q", "Exit", _FILE),
    "copy": Shortcut("Ctrl+C", "Copy", _EDIT),
    "copy_messages": Shortcut("Ctrl+Shift+C", "Copy without timestamps", _EDIT),
    "select_all": Shortcut("Ctrl+A", "Select all", _EDIT),
    "find": Shortcut("Ctrl+F", "Find", _EDIT),
    "find_next": Shortcut("F3", "Find next", _EDIT),
    "find_previous": Shortcut("Shift+F3", "Find previous", _EDIT),
    "filter": Shortcut("Ctrl+L", "Filter lines", _EDIT),
    "close_find": Shortcut("Esc", "Close Find and clear the filter", _EDIT),
    "plot": Shortcut("Ctrl+P", "Live plot", _VIEW),
    "text_larger": Shortcut("Ctrl++", "Larger text", _VIEW),
    "text_smaller": Shortcut("Ctrl+-", "Smaller text", _VIEW),
    "text_reset": Shortcut("Ctrl+0", "Reset text size", _VIEW),
    "refresh_ports": Shortcut("F5", "Refresh ports", _DEVICE),
    "rename_tab": Shortcut("F2", "Rename tab", _DEVICE),
    **{
        saved_command_name(number): Shortcut(
            f"Ctrl+{number}", f"Put saved command {number} in the command box", _DEVICE,
        )
        for number in range(1, SAVED_COMMAND_SHORTCUTS + 1)
    },
    "user_guide": Shortcut("F1", "User guide", _HELP),
}


#: Further key combinations that do the same as the one in the table.
ALTERNATE_KEYS: dict[str, tuple[str, ...]] = {
    # On most keyboards "+" needs Shift; the same key without it is "=".
    "text_larger": ("Ctrl+=",),
}


def keys(name: str) -> str:
    """The key combination of an action, in Qt's portable form."""
    return SHORTCUTS[name].keys


def native_keys(name: str) -> str:
    """The key combination as this platform writes it."""
    from PySide6.QtGui import QKeySequence

    return QKeySequence(SHORTCUTS[name].keys).toString(
        QKeySequence.SequenceFormat.NativeText
    )


def menu_text(text: str, name: str) -> str:
    """Text for an entry of a context menu that shows the action's keys.

    The keys are shown only: the entry of the main menu is what makes
    them work, and a second entry claiming the same keys would make
    them ambiguous.
    """
    return f"{text}\t{native_keys(name)}"


def with_keys(text: str, name: str) -> str:
    """Tooltip text followed by the action's shortcut: "Find (Ctrl+F)"."""
    return f"{text} ({native_keys(name)})"
