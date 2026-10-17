# SPDX-License-Identifier: GPL-3.0-or-later
"""The Export session dialog: save the session to a file.

Logging writes lines as they arrive, from the moment it is started.
Export writes the whole session so far, from the connection (or the
last Clear) to now, including lines the console no longer holds.
"""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

from app.constants import ExportFormat
from ui.dialogs.common import ChoiceRow, action_button, button_row, dialog_layout, note
from ui.dialogs.file_target import FileTarget
from ui.themes import tokens
from ui.widgets.dot_grid import DotGridDialog
from ui.widgets.least_size import keep_least_size

DEFAULT_EXPORT_NAME = "Export"

_EXTENSIONS: dict[ExportFormat, tuple[str, ...]] = {
    ExportFormat.TXT: (".txt",),
    ExportFormat.CSV: (".csv",),
    ExportFormat.BOTH: (".txt", ".csv"),
}
_EXPLANATION = (
    "Saves everything received since you connected, or since the last Clear. "
    "The device keeps being read while the file is written."
)


class ExportDialog(DotGridDialog):
    """Collects what an export needs. Read the properties after it is accepted."""

    def __init__(
        self,
        default_directory: str,
        port_name: str,
        default_format: ExportFormat = ExportFormat.TXT,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export session")
        self.setModal(True)

        self._files = ChoiceRow(
            (("Text", ExportFormat.TXT), ("CSV", ExportFormat.CSV), ("Both", ExportFormat.BOTH)),
            default_format,
        )
        self._target = FileTarget(
            default_name=DEFAULT_EXPORT_NAME,
            directory=default_directory,
            port_name=port_name,
            extensions=lambda: _EXTENSIONS[self._files.value],
            browse_title="Choose the export folder",
        )
        self._target.add_row("Files", self._files)
        self._target.set_remark("The folder and the files chosen are remembered for next time.")
        self._files.changed.connect(self._target.refresh)
        self._target.refresh()

        cancel = action_button("Cancel")
        cancel.clicked.connect(self.reject)
        self._export = action_button("Export", primary=True)
        self._export.setDefault(True)
        self._export.clicked.connect(self.accept)

        layout = dialog_layout(self)
        layout.addWidget(note(_EXPLANATION))
        layout.addWidget(self._target)
        layout.addStretch(1)
        layout.addLayout(button_row(cancel, self._export))
        keep_least_size(self, tokens.METRICS.dialog_width)
        self._target.name_input.setFocus()

    # ── Results ──────────────────────────────────────────────────────────

    @property
    def export_name(self) -> str:
        return self._target.name

    @property
    def export_directory(self) -> str:
        return self._target.directory

    @property
    def export_format(self) -> ExportFormat:
        return self._files.value

    def preview_names(self) -> list[str]:
        return self._target.preview_names()
