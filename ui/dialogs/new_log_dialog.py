# SPDX-License-Identifier: GPL-3.0-or-later
"""The Start a log dialog: name, folder, files and notes of a new log."""

from __future__ import annotations

from PySide6.QtWidgets import QPlainTextEdit, QWidget

from app.constants import FILENAME_DEFAULT_NAME, ExportFormat
from ui.dialogs.common import ChoiceRow, action_button, button_row, dialog_layout
from ui.dialogs.file_target import FileTarget
from ui.formatting import format_size
from ui.themes import tokens
from ui.widgets.dot_grid import DotGridDialog
from ui.widgets.least_size import keep_least_size

_TEXT = ".txt"
_CSV = ".csv"
_FILES_HINT = "The text log holds every line. The CSV holds the readings as columns."


class NewLogDialog(DotGridDialog):
    """Collects what a log needs. Read the properties after it is accepted."""

    def __init__(
        self,
        port_name: str,
        default_directory: str,
        default_format: ExportFormat = ExportFormat.TXT,
        split_bytes: int = 0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Start a log")
        self.setModal(True)
        metrics = tokens.METRICS

        # The text log is the complete record and is always written, so
        # the choice is whether a CSV is written beside it.
        self._files = ChoiceRow(
            (("Text", ExportFormat.TXT), ("Text and CSV", ExportFormat.BOTH)),
            ExportFormat.TXT if default_format is ExportFormat.TXT else ExportFormat.BOTH,
        )
        self._target = FileTarget(
            default_name=FILENAME_DEFAULT_NAME,
            directory=default_directory,
            port_name=port_name,
            extensions=self._extensions,
            browse_title="Choose the log folder",
        )
        self._target.add_row("Files", self._files)
        self._notes = QPlainTextEdit()
        self._notes.setPlaceholderText("Optional. Written into the header of the file.")
        self._notes.setFixedHeight(metrics.notes_height)
        self._notes.setTabChangesFocus(True)
        self._notes.setAccessibleName("Notes")
        self._target.add_row("Notes", self._notes, tall=True)
        self._target.set_remark(
            f"{_FILES_HINT} A new file starts every {format_size(split_bytes)}."
            if split_bytes else _FILES_HINT
        )
        self._files.changed.connect(self._target.refresh)
        self._target.refresh()

        cancel = action_button("Cancel")
        cancel.clicked.connect(self.reject)
        self._start = action_button("Start log", primary=True)
        self._start.setDefault(True)
        self._start.clicked.connect(self.accept)

        layout = dialog_layout(self)
        layout.addWidget(self._target)
        layout.addStretch(1)
        layout.addLayout(button_row(cancel, self._start))
        keep_least_size(self, metrics.dialog_width)
        self._target.name_input.setFocus()

    # ── Results ──────────────────────────────────────────────────────────

    @property
    def log_name(self) -> str:
        return self._target.name

    @property
    def log_directory(self) -> str:
        return self._target.directory

    @property
    def export_format(self) -> ExportFormat:
        return self._files.value

    @property
    def description(self) -> str:
        return self._notes.toPlainText().strip()

    def preview_names(self) -> list[str]:
        return self._target.preview_names()

    def remark(self) -> str:
        return self._target.remark

    # ── Internal ─────────────────────────────────────────────────────────

    def _extensions(self) -> tuple[str, ...]:
        return (_TEXT,) if self._files.value is ExportFormat.TXT else (_TEXT, _CSV)
