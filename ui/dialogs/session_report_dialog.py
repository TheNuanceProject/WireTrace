# SPDX-License-Identifier: GPL-3.0-or-later
"""Shows a session's end-of-test report, to read, copy or save."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QPlainTextEdit, QWidget

from ui.dialogs.common import action_button, button_row, dialog_layout, note
from ui.themes import tokens
from ui.themes.fonts import monospace_font
from ui.tour.targets import Target, mark
from ui.widgets.dot_grid import DotGridDialog
from ui.widgets.least_size import keep_least_size
from ui.widgets.toast import Toast


class SessionReportDialog(DotGridDialog):
    """The report as text. ``save`` is called with the dialog and the text
    when Save is chosen; it asks where and says how it went."""

    def __init__(
        self, report: str, save: Callable[[QWidget, str], None], parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Session report")
        metrics = tokens.METRICS

        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        self._text.setFont(monospace_font(tokens.TYPOGRAPHY.console_family))
        self._text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._text.setAccessibleName("Session report")
        self._text.setPlainText(report)

        copy = mark(action_button("Copy"), Target.REPORT_ACTIONS)
        copy.clicked.connect(self._copy)
        save_button = mark(action_button("Save..."), Target.REPORT_ACTIONS)
        save_button.clicked.connect(lambda: save(self, self.text()))
        close = action_button("Close", primary=True)
        close.setDefault(True)
        close.clicked.connect(self.accept)

        layout = dialog_layout(self)
        layout.addWidget(note(
            "Counted as each line arrived, so the figures are exact even when the screen "
            "skipped lines."
        ))
        layout.addWidget(self._text, 1)
        layout.addLayout(button_row(save_button, close, leading=copy))
        keep_least_size(self)
        self.resize(metrics.dialog_medium_width, metrics.dialog_tall_height)

    def text(self) -> str:
        return self._text.toPlainText()

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.text())
        Toast.success(self, "Report copied")
