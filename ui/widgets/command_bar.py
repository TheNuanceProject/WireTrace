# SPDX-License-Identifier: GPL-3.0-or-later
"""The command row: type a command, choose its line ending, send it.

Enter sends. The drop-down keeps the commands sent in this session.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QMenu,
    QWidget,
)

from app.constants import (
    COMMAND_HISTORY_LIMIT,
    DEFAULT_LINE_ENDING,
    SAVED_COMMAND_SHORTCUTS,
    LineEnding,
)
from app.saved_commands import SavedCommands
from core.records import COMMAND_PREFIX
from ui import shortcuts
from ui.themes import tokens
from ui.themes.fonts import monospace_font
from ui.widgets.button import Button
from ui.widgets.styling import plain_label, set_primary

#: Name of each line ending as shown in the selector.
LINE_ENDING_NAMES: dict[LineEnding, str] = {
    LineEnding.NONE: "None",
    LineEnding.LF: "LF",
    LineEnding.CR: "CR",
    LineEnding.CRLF: "CR LF",
}


class CommandBar(QFrame):
    """Command input with history, a line-ending selector and Send.

    Signals:
        command_sent(str):         A command to send, without line ending.
        line_ending_changed(obj):  The ``LineEnding`` the user chose.
        edit_saved_requested():    The user asked to edit the saved commands.
        saved_command_chosen():    A saved command was put in the box.
    """

    command_sent = Signal(str)
    line_ending_changed = Signal(object)
    edit_saved_requested = Signal()
    saved_command_chosen = Signal()

    def __init__(
        self, saved_commands: SavedCommands | None = None, parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._saved_commands = saved_commands
        self.setObjectName("commandBar")
        metrics = tokens.METRICS
        self.setFixedHeight(metrics.toolbar_height)
        mono = monospace_font(tokens.TYPOGRAPHY.console_family)

        prompt = plain_label(COMMAND_PREFIX)
        prompt.setObjectName("commandPrompt")
        prompt.setFont(mono)

        self._input = QComboBox()
        self._input.setEditable(True)
        self._input.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._input.setFont(mono)
        self._input.lineEdit().setPlaceholderText("Send a command")
        self._input.lineEdit().returnPressed.connect(self._on_send)
        self._input.setAccessibleName("Command")

        self._ending = QComboBox()
        self._ending.setFixedWidth(metrics.line_ending_field_width)
        self._ending.setToolTip("What is sent after the command")
        self._ending.setAccessibleName("Line ending")
        for ending, name in LINE_ENDING_NAMES.items():
            self._ending.addItem(name, ending)
        self.set_line_ending(DEFAULT_LINE_ENDING)
        self._ending.currentIndexChanged.connect(
            lambda: self.line_ending_changed.emit(self.line_ending)
        )

        # Saved commands. The menu is filled each time it opens, so it
        # shows what any tab or the edit dialog has saved since.
        self._saved = Button("Saved")
        self._saved.setToolTip("Commands saved to send again. Choosing one puts it in the box.")
        self._saved_menu = QMenu(self)
        self._saved_menu.aboutToShow.connect(self._fill_saved_menu)
        self._saved.setMenu(self._saved_menu)
        self._saved.setVisible(saved_commands is not None)

        self._send = Button("Send")
        self._send.setToolTip("Send the command (Enter)")
        set_primary(self._send)
        self._send.clicked.connect(self._on_send)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(metrics.space_3, 0, metrics.space_3, 0)
        layout.setSpacing(metrics.space_2)
        layout.addWidget(prompt)
        layout.addWidget(self._input, 1)
        layout.addWidget(self._saved)
        layout.addWidget(self._ending)
        layout.addWidget(self._send)

    # ── Public API ───────────────────────────────────────────────────────

    @property
    def line_ending(self) -> LineEnding:
        return self._ending.currentData()

    def set_line_ending(self, ending: LineEnding) -> None:
        index = self._ending.findData(ending)
        if index >= 0:
            self._ending.setCurrentIndex(index)

    @property
    def text(self) -> str:
        return self._input.currentText()

    def set_text(self, text: str) -> None:
        """Put a command in the box, ready to be sent with Enter."""
        self._input.setCurrentText(text)
        self._input.lineEdit().setFocus()

    def set_sending_enabled(self, enabled: bool) -> None:
        """Commands can be sent only while a device is connected."""
        self._input.setEnabled(enabled)
        self._saved.setEnabled(enabled)
        self._send.setEnabled(enabled)

    def saved_menu_texts(self) -> list[str]:
        """The entries of the Saved menu as it would open now."""
        self._fill_saved_menu()
        return [
            action.text() for action in self._saved_menu.actions() if not action.isSeparator()
        ]

    def history(self) -> list[str]:
        return [self._input.itemText(index) for index in range(self._input.count())]

    # ── Internal ─────────────────────────────────────────────────────────

    def _fill_saved_menu(self) -> None:
        menu = self._saved_menu
        menu.clear()
        store = self._saved_commands
        if store is None:
            return
        for number, command in enumerate(store.all(), start=1):
            text = command.replace("&", "&&")
            if number <= SAVED_COMMAND_SHORTCUTS:
                text = shortcuts.menu_text(text, shortcuts.saved_command_name(number))
            menu.addAction(text, lambda c=command: self._choose_saved(c))
        if not menu.isEmpty():
            menu.addSeparator()
        save = menu.addAction("Save the command in the box", self._save_current)
        save.setEnabled(store.can_add(self.text))
        menu.addAction("Edit saved commands...", self.edit_saved_requested.emit)

    def _choose_saved(self, command: str) -> None:
        self.set_text(command)
        self.saved_command_chosen.emit()

    def _save_current(self) -> None:
        if self._saved_commands is not None:
            self._saved_commands.add(self.text)

    def _on_send(self) -> None:
        text = self._input.currentText().strip()
        if not text:
            return
        existing = self._input.findText(text)
        if existing >= 0:
            self._input.removeItem(existing)
        self._input.insertItem(0, text)
        while self._input.count() > COMMAND_HISTORY_LIMIT:
            self._input.removeItem(self._input.count() - 1)
        self._input.setCurrentText("")
        self.command_sent.emit(text)
