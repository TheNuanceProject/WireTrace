# SPDX-License-Identifier: MIT
"""New Log Session dialog per spec section 6.9.

Collects: log name, save directory, export format, optional description.
Shows file preview. Returns results via properties after accept.
"""

from __future__ import annotations

import os
import re
from datetime import datetime

from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QTextEdit,
    QVBoxLayout,
)

from app.constants import (
    FILENAME_DEFAULT_NAME,
    FILENAME_MAX_LENGTH,
    FILENAME_TIMESTAMP_FORMAT,
    ExportFormat,
)


class NewLogDialog(QDialog):
    """Dialog for starting a new log session."""

    def __init__(
        self,
        port_name: str = "",
        default_directory: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("New Log Session")
        self.setMinimumWidth(520)
        self.setModal(True)

        self._port_name = port_name
        self._default_directory = default_directory or os.path.expanduser("~")

        self._setup_ui()
        self._connect_signals()
        self._update_preview()

    # ── Properties (read after accept) ────────────────────────────────────

    @property
    def log_name(self) -> str:
        name = self._name_input.text().strip()
        return name if name else FILENAME_DEFAULT_NAME

    @property
    def log_directory(self) -> str:
        return self._dir_input.text().strip() or self._default_directory

    @property
    def export_format(self) -> ExportFormat:
        if self._radio_csv.isChecked():
            return ExportFormat.CSV
        elif self._radio_both.isChecked():
            return ExportFormat.BOTH
        return ExportFormat.TXT

    @property
    def description(self) -> str:
        return self._desc_input.toPlainText().strip()

    # ── UI Setup ──────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Log Name
        name_group = QGroupBox("Log Name")
        name_layout = QHBoxLayout(name_group)
        name_layout.addWidget(QLabel("Name:"))
        self._name_input = QLineEdit()
        self._name_input.setPlaceholderText("Enter session name...")
        self._name_input.setMaxLength(FILENAME_MAX_LENGTH)
        name_layout.addWidget(self._name_input)
        layout.addWidget(name_group)

        # Save Location
        dir_group = QGroupBox("Save Location")
        dir_layout = QHBoxLayout(dir_group)
        dir_layout.addWidget(QLabel("Directory:"))
        self._dir_input = QLineEdit(self._default_directory)
        dir_layout.addWidget(self._dir_input)
        self._browse_btn = QPushButton("Browse...")
        self._browse_btn.clicked.connect(self._on_browse)
        dir_layout.addWidget(self._browse_btn)
        layout.addWidget(dir_group)

        # Export Format
        fmt_group = QGroupBox("Export Format")
        fmt_layout = QVBoxLayout(fmt_group)
        self._radio_txt = QRadioButton("Text file (.txt) — Default")
        self._radio_csv = QRadioButton("CSV file (.csv) — Structured data")
        self._radio_both = QRadioButton("Both (.txt and .csv) — Complete export")
        self._radio_txt.setChecked(True)

        self._fmt_group = QButtonGroup(self)
        self._fmt_group.addButton(self._radio_txt)
        self._fmt_group.addButton(self._radio_csv)
        self._fmt_group.addButton(self._radio_both)

        fmt_layout.addWidget(self._radio_txt)
        fmt_layout.addWidget(self._radio_csv)
        fmt_layout.addWidget(self._radio_both)
        layout.addWidget(fmt_group)

        # File Preview
        preview_group = QGroupBox("File Preview")
        preview_layout = QVBoxLayout(preview_group)
        self._preview_file = QLabel()
        self._preview_dir = QLabel()
        self._preview_file.setProperty("secondary", True)
        self._preview_dir.setProperty("secondary", True)
        preview_layout.addWidget(self._preview_file)
        preview_layout.addWidget(self._preview_dir)
        layout.addWidget(preview_group)

        # Description
        desc_group = QGroupBox("Description (Optional)")
        desc_layout = QVBoxLayout(desc_group)
        self._desc_input = QTextEdit()
        self._desc_input.setMaximumHeight(80)
        self._desc_input.setPlaceholderText("Optional notes about this session...")
        desc_layout.addWidget(self._desc_input)
        layout.addWidget(desc_group)

        # Buttons — Cancel left, primary action right (industry standard)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("clearBtn")
        cancel_btn.setFixedHeight(28)
        cancel_btn.setMinimumWidth(80)
        cancel_btn.clicked.connect(self.reject)

        self._start_btn = QPushButton("Start Logging")
        self._start_btn.setFixedHeight(28)
        self._start_btn.setMinimumWidth(110)
        self._start_btn.clicked.connect(self.accept)

        btn_row.addWidget(cancel_btn)
        btn_row.addSpacing(6)
        btn_row.addWidget(self._start_btn)
        layout.addLayout(btn_row)

    def _connect_signals(self) -> None:
        self._name_input.textChanged.connect(self._update_preview)
        self._dir_input.textChanged.connect(self._update_preview)
        self._fmt_group.buttonClicked.connect(self._update_preview)

    def _update_preview(self) -> None:
        """Update the file preview label."""
        name = self.log_name
        safe_name = self._sanitize(name)
        port_safe = self._port_name.replace("/", "_")
        timestamp = datetime.now().strftime(FILENAME_TIMESTAMP_FORMAT)
        base = f"{safe_name}_{port_safe}_{timestamp}"

        fmt = self.export_format
        if fmt == ExportFormat.TXT:
            self._preview_file.setText(f"  {base}.txt")
        elif fmt == ExportFormat.CSV:
            self._preview_file.setText(f"  {base}.csv")
        else:
            self._preview_file.setText(f"  {base}.txt\n  {base}.csv")

        self._preview_dir.setText(f"  {self.log_directory}")

    def _on_browse(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Select Log Directory", self.log_directory
        )
        if directory:
            self._dir_input.setText(directory)

    @staticmethod
    def _sanitize(name: str) -> str:
        if not name or not name.strip():
            return FILENAME_DEFAULT_NAME
        s = name.strip().replace(" ", "_")
        s = re.sub(r'[<>:"/\\|?*]', '', s)
        return s[:FILENAME_MAX_LENGTH] if len(s) > FILENAME_MAX_LENGTH else (s or FILENAME_DEFAULT_NAME)
