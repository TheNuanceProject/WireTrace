# SPDX-License-Identifier: GPL-3.0-or-later
"""The Edit saved commands dialog: add, change, remove and order them."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.constants import SAVED_COMMAND_SHORTCUTS, SAVED_COMMANDS_MAX
from app.saved_commands import clean
from ui import shortcuts
from ui.dialogs.common import action_button, button_row, dialog_layout, note
from ui.themes import tokens
from ui.themes.fonts import monospace_font
from ui.widgets.dot_grid import DotGridDialog
from ui.widgets.least_size import keep_least_size

_NEW_COMMAND = "new command"


class SavedCommandsDialog(DotGridDialog):
    """Edits a copy of the list. Read ``commands()`` after it is accepted."""

    def __init__(self, commands: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit saved commands")
        self.setModal(True)
        metrics = tokens.METRICS

        self._list = QListWidget()
        self._list.setFont(monospace_font(tokens.TYPOGRAPHY.console_family))
        self._list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._list.setAccessibleName("Saved commands")
        for command in commands:
            self._add_item(command)
        self._list.currentRowChanged.connect(self._refresh_buttons)
        self._list.model().rowsInserted.connect(self._refresh_buttons)
        self._list.model().rowsRemoved.connect(self._refresh_buttons)

        self._add = action_button("Add")
        self._add.clicked.connect(self._on_add)
        self._remove = action_button("Remove")
        self._remove.clicked.connect(self._on_remove)
        self._up = action_button("Move up")
        self._up.clicked.connect(lambda: self._move(-1))
        self._down = action_button("Move down")
        self._down.clicked.connect(lambda: self._move(1))
        side = QVBoxLayout()
        side.setSpacing(metrics.space_2)
        for button in (self._add, self._remove, self._up, self._down):
            side.addWidget(button)
        side.addStretch(1)

        body = QHBoxLayout()
        body.setSpacing(metrics.space_3)
        body.addWidget(self._list, 1)
        body.addLayout(side)

        cancel = action_button("Cancel")
        cancel.clicked.connect(self.reject)
        save = action_button("Save", primary=True)
        save.setDefault(True)
        save.clicked.connect(self.accept)

        first = shortcuts.native_keys(shortcuts.saved_command_name(1))
        last = shortcuts.native_keys(shortcuts.saved_command_name(SAVED_COMMAND_SHORTCUTS))
        layout = dialog_layout(self)
        layout.addWidget(note(
            f"Double-click a command to change it. The first {SAVED_COMMAND_SHORTCUTS} "
            f"have the shortcuts {first} to {last}. At most {SAVED_COMMANDS_MAX} are kept."
        ))
        layout.addLayout(body, 1)
        layout.addLayout(button_row(cancel, save))
        keep_least_size(self, metrics.dialog_width, metrics.dialog_short_height)
        self._refresh_buttons()

    # ── Result ───────────────────────────────────────────────────────────

    def commands(self) -> list[str]:
        """The list as edited: trimmed, without blanks or repeats."""
        return clean(self._list.item(row).text() for row in range(self._list.count()))

    # ── Internal ─────────────────────────────────────────────────────────

    def _add_item(self, text: str) -> QListWidgetItem:
        item = QListWidgetItem(text)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        self._list.addItem(item)
        return item

    def _on_add(self) -> None:
        item = self._add_item(_NEW_COMMAND)
        self._list.setCurrentItem(item)
        self._list.editItem(item)

    def _on_remove(self) -> None:
        row = self._list.currentRow()
        if row >= 0:
            self._list.takeItem(row)

    def _move(self, step: int) -> None:
        row = self._list.currentRow()
        target = row + step
        if row < 0 or not 0 <= target < self._list.count():
            return
        self._list.insertItem(target, self._list.takeItem(row))
        self._list.setCurrentRow(target)

    def _refresh_buttons(self, *_: object) -> None:
        row = self._list.currentRow()
        count = self._list.count()
        self._add.setEnabled(count < SAVED_COMMANDS_MAX)
        self._remove.setEnabled(row >= 0)
        self._up.setEnabled(row > 0)
        self._down.setEnabled(0 <= row < count - 1)
