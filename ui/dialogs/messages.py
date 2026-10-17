# SPDX-License-Identifier: GPL-3.0-or-later
"""The small dialogs that ask or tell one thing.

They are built here, with the shared button, so that their buttons follow
the same size rule as every other button of the application. The standard
boxes of the toolkit make their own buttons, which do not.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLabel, QLineEdit, QMessageBox, QWidget

from ui.dialogs.common import action_button, button_row, dialog_layout
from ui.themes import tokens
from ui.widgets.button import Button
from ui.widgets.least_size import keep_least_size
from ui.widgets.styling import plain_label, set_primary

_CANCEL = "Cancel"
_OK = "OK"


def _show_as_written(box: QMessageBox) -> None:
    """Show the texts of ``box`` exactly as they are, never as markup:
    they may name a file, a port or a profile."""
    box.setTextFormat(Qt.TextFormat.PlainText)
    for label in box.findChildren(QLabel):
        label.setTextFormat(Qt.TextFormat.PlainText)


def confirm(
    parent: QWidget | None,
    title: str,
    text: str,
    *,
    proceed: str,
    stay: str = _CANCEL,
    detail: str = "",
) -> bool:
    """Ask before something that cannot be undone. True means go ahead.

    ``stay`` is the safe answer: it is the main button, and Enter and
    Escape both choose it.
    """
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setText(text)
    box.setInformativeText(detail)
    _show_as_written(box)
    stay_button = Button(stay)
    proceed_button = Button(proceed)
    box.addButton(stay_button, QMessageBox.ButtonRole.RejectRole)
    box.addButton(proceed_button, QMessageBox.ButtonRole.AcceptRole)
    set_primary(stay_button)
    box.setDefaultButton(stay_button)
    box.setEscapeButton(stay_button)
    box.exec()
    return box.clickedButton() is proceed_button


def tell(
    parent: QWidget | None,
    title: str,
    text: str,
    *,
    problem: bool = True,
) -> None:
    """Say one thing that needs reading before going on.

    ``problem`` is False for a plain piece of information.
    """
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Icon.Warning if problem else QMessageBox.Icon.Information)
    box.setText(text)
    _show_as_written(box)
    button = Button(_OK)
    box.addButton(button, QMessageBox.ButtonRole.AcceptRole)
    set_primary(button)
    box.setDefaultButton(button)
    box.setEscapeButton(button)
    box.exec()


class TextPrompt(QDialog):
    """Asks for one line of text."""

    def __init__(
        self, title: str, label: str, text: str = "", parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        keep_least_size(self, tokens.METRICS.dialog_prompt_width)

        self._input = QLineEdit(text)
        self._input.selectAll()
        caption = plain_label(label)
        caption.setBuddy(self._input)
        caption.setWordWrap(True)

        cancel = action_button(_CANCEL)
        cancel.clicked.connect(self.reject)
        accept = action_button(_OK, primary=True)
        accept.setDefault(True)
        accept.clicked.connect(self.accept)

        layout = dialog_layout(self)
        layout.addWidget(caption)
        layout.addWidget(self._input)
        layout.addLayout(button_row(cancel, accept))

    def text(self) -> str:
        return self._input.text()


def ask_text(parent: QWidget | None, title: str, label: str, text: str = "") -> str | None:
    """Ask for one line of text. None means the question was cancelled."""
    prompt = TextPrompt(title, label, text, parent)
    if prompt.exec() != QDialog.DialogCode.Accepted:
        return None
    return prompt.text()
