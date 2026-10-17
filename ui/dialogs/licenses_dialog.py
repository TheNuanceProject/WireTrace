# SPDX-License-Identifier: GPL-3.0-or-later
"""Shows the licences of the third-party work the application contains."""

from __future__ import annotations

from PySide6.QtWidgets import QPlainTextEdit, QWidget

from app.licenses import load_third_party_licenses
from ui.dialogs.common import action_button, button_row, dialog_layout, note
from ui.themes import tokens
from ui.themes.fonts import monospace_font
from ui.widgets.dot_grid import DotGridDialog
from ui.widgets.least_size import keep_least_size
from version import APP_NAME


class LicensesDialog(DotGridDialog):
    """The licence texts, to read and to copy from.

    Raises:
        LicensesNotFoundError: the texts are missing from the installation.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Third-party licences")
        metrics = tokens.METRICS

        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        self._text.setFont(monospace_font(tokens.TYPOGRAPHY.console_family))
        self._text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._text.setAccessibleName("Licence texts")
        self._text.setPlainText(load_third_party_licenses())

        close = action_button("Close", primary=True)
        close.clicked.connect(self.accept)

        layout = dialog_layout(self)
        layout.addWidget(note(
            f"{APP_NAME} contains the work of others. Each is listed with the licence it "
            "is used under."
        ))
        layout.addWidget(self._text, 1)
        layout.addLayout(button_row(close))
        # The least is what the content needs; it opens at the design size.
        keep_least_size(self)
        self.resize(metrics.dialog_wide_width, metrics.dialog_tall_height)

    def text(self) -> str:
        return self._text.toPlainText()
