# SPDX-License-Identifier: GPL-3.0-or-later
"""The part the Start log and Export dialogs share: where the files go
and what they will be called."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from app.constants import FILENAME_MAX_LENGTH, FILENAME_PREVIEW_INTERVAL_MS
from app.file_names import output_base_name
from ui.dialogs.common import action_button, heading, note
from ui.themes import tokens
from ui.themes.fonts import monospace_font
from ui.widgets.styling import plain_label, plain_tooltip


class FileTarget(QWidget):
    """Name and folder fields, and a preview of the files to be written.

    The preview carries the time of day, so it is brought up to date
    every second while the dialog is open.

    Signals:
        changed():  The name or the folder was edited.
    """

    changed = Signal()

    def __init__(
        self,
        *,
        default_name: str,
        directory: str,
        port_name: str,
        extensions: Callable[[], tuple[str, ...]],
        browse_title: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        metrics = tokens.METRICS
        self._default_name = default_name
        self._default_directory = directory
        self._port_name = port_name
        self._extensions = extensions
        self._browse_title = browse_title

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText(default_name)
        self.name_input.setMaxLength(FILENAME_MAX_LENGTH)
        self.name_input.setAccessibleName("Name")
        self.directory_input = QLineEdit(directory)
        self.directory_input.setAccessibleName("Folder")
        self._browse = action_button("Browse")
        self._browse.clicked.connect(self._on_browse)

        self._fields = QGridLayout()
        self._fields.setContentsMargins(0, 0, 0, 0)
        self._fields.setHorizontalSpacing(metrics.space_3)
        self._fields.setVerticalSpacing(metrics.space_3)
        self._fields.setColumnStretch(1, 1)
        self._fields.addWidget(heading("Name"), 0, 0)
        self._fields.addWidget(self.name_input, 0, 1, 1, 2)
        self._fields.addWidget(heading("Folder"), 1, 0)
        self._fields.addWidget(self.directory_input, 1, 1)
        self._fields.addWidget(self._browse, 1, 2)

        self._preview = plain_label()
        self._preview.setObjectName("filePreview")
        self._preview.setFont(monospace_font(tokens.TYPOGRAPHY.console_family))
        self._preview.setWordWrap(True)
        self._remark = note(small=True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(metrics.space_3)
        layout.addLayout(self._fields)
        layout.addWidget(self._preview)
        layout.addWidget(self._remark)

        self.name_input.textChanged.connect(self._on_edited)
        self.directory_input.textChanged.connect(self._on_edited)
        self._clock = QTimer(self)
        self._clock.setInterval(FILENAME_PREVIEW_INTERVAL_MS)
        self._clock.timeout.connect(self.refresh)
        self._clock.start()
        self.refresh()

    # ── Public API ───────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return self.name_input.text().strip() or self._default_name

    @property
    def directory(self) -> str:
        return self.directory_input.text().strip() or self._default_directory

    def add_row(self, label: str, widget: QWidget, *, tall: bool = False) -> None:
        """Add a labelled field under Name and Folder.

        The label of a ``tall`` field sits level with its first line.
        """
        row = self._fields.rowCount()
        name = heading(label)
        if tall:
            name.setContentsMargins(0, tokens.METRICS.space_2, 0, 0)
            self._fields.addWidget(name, row, 0, Qt.AlignmentFlag.AlignTop)
        else:
            self._fields.addWidget(name, row, 0)
        self._fields.addWidget(widget, row, 1, 1, 2)

    @property
    def remark(self) -> str:
        return self._remark.text()

    def set_remark(self, text: str) -> None:
        """Set the line under the preview. Empty hides it."""
        self._remark.setText(text)
        self._remark.setVisible(bool(text))

    def preview_names(self) -> list[str]:
        """The file names shown, one per file."""
        return self._preview.text().split("\n") if self._preview.text() else []

    def refresh(self) -> None:
        """Bring the previewed file names up to date."""
        base = output_base_name(self.name, self._port_name, datetime.now())
        self._preview.setText("\n".join(f"{base}{ext}" for ext in self._extensions()))
        self._preview.setToolTip(plain_tooltip(os.path.join(self.directory, base)))

    # ── Internal ─────────────────────────────────────────────────────────

    def _on_edited(self) -> None:
        self.refresh()
        self.changed.emit()

    def _on_browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, self._browse_title, self.directory)
        if chosen:
            self.directory_input.setText(chosen)
