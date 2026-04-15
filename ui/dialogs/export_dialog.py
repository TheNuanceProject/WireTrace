# SPDX-License-Identifier: MIT
"""WireTrace Export dialog — snapshot current console content to file.

Unlike logging (which captures data in real-time as it arrives), export
takes the current console buffer and writes it to disk as a one-time dump.
Use this when you forgot to start logging, or want a separate copy.
"""

from __future__ import annotations

import os
from datetime import datetime

from PySide6.QtGui import QFont
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
    QVBoxLayout,
)

from app.constants import ExportFormat


class ExportDialog(QDialog):
    """Dialog for exporting current console content to file."""

    def __init__(
        self,
        default_directory: str = "",
        port_name: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export Console Data")
        self.setFixedWidth(520)
        self.setModal(True)

        self._port_name = port_name or "Unknown"
        self._default_directory = default_directory or os.path.expanduser("~")

        self._setup_ui()
        self._update_preview()

    # ── Public Properties ─────────────────────────────────────────────────

    @property
    def export_name(self) -> str:
        """User-entered export name (sanitized)."""
        return self._name_input.text().strip() or "Export"

    @property
    def export_directory(self) -> str:
        """Selected export directory."""
        return self._dir_input.text().strip() or self._default_directory

    @property
    def export_format(self) -> ExportFormat:
        """Selected export format."""
        if self._radio_csv.isChecked():
            return ExportFormat.CSV
        elif self._radio_both.isChecked():
            return ExportFormat.BOTH
        return ExportFormat.TXT

    # ── UI Setup ──────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(12)

        # ── Export Name
        name_group = QGroupBox("Export Name")
        name_layout = QHBoxLayout(name_group)
        name_layout.setContentsMargins(12, 16, 12, 12)

        name_lbl = QLabel("Name")
        nf = name_lbl.font()
        nf.setWeight(QFont.Weight.DemiBold)
        name_lbl.setFont(nf)

        self._name_input = QLineEdit()
        self._name_input.setFixedHeight(26)
        self._name_input.setPlaceholderText("Enter export name...")
        self._name_input.setText("Export")
        self._name_input.textChanged.connect(self._update_preview)

        name_layout.addWidget(name_lbl)
        name_layout.addWidget(self._name_input, 1)
        layout.addWidget(name_group)

        # ── Save Location
        dir_group = QGroupBox("Save Location")
        dir_layout = QHBoxLayout(dir_group)
        dir_layout.setContentsMargins(12, 16, 12, 12)

        self._dir_input = QLineEdit(self._default_directory)
        self._dir_input.setFixedHeight(26)
        self._dir_input.setMinimumWidth(200)
        self._dir_input.setReadOnly(True)
        self._dir_input.setPlaceholderText("Select a directory...")
        self._dir_input.textChanged.connect(self._update_preview)

        browse_btn = QPushButton("Browse")
        browse_btn.setObjectName("clearBtn")
        browse_btn.setFixedSize(72, 26)
        browse_btn.clicked.connect(self._on_browse)

        dir_layout.addWidget(self._dir_input, 1)
        dir_layout.addWidget(browse_btn)
        layout.addWidget(dir_group)

        # ── Export Format
        fmt_group = QGroupBox("Export Format")
        fmt_layout = QVBoxLayout(fmt_group)
        fmt_layout.setContentsMargins(12, 16, 12, 12)
        fmt_layout.setSpacing(6)

        self._radio_txt = QRadioButton("Text file (.txt)")
        self._radio_csv = QRadioButton("CSV file (.csv)")
        self._radio_both = QRadioButton("Both (.txt and .csv)")
        self._radio_txt.setChecked(True)

        self._fmt_group = QButtonGroup(self)
        self._fmt_group.addButton(self._radio_txt)
        self._fmt_group.addButton(self._radio_csv)
        self._fmt_group.addButton(self._radio_both)

        for radio in (self._radio_txt, self._radio_csv, self._radio_both):
            radio.toggled.connect(self._update_preview)
            fmt_layout.addWidget(radio)

        layout.addWidget(fmt_group)

        # ── File Preview
        preview_group = QGroupBox("File Preview")
        preview_layout = QVBoxLayout(preview_group)
        preview_layout.setContentsMargins(12, 16, 12, 12)
        preview_layout.setSpacing(2)

        self._preview_label = QLabel()
        pf = self._preview_label.font()
        pf.setPointSize(9)
        self._preview_label.setFont(pf)
        self._preview_label.setWordWrap(True)
        preview_layout.addWidget(self._preview_label)

        self._dir_preview_label = QLabel()
        self._dir_preview_label.setFont(pf)
        self._dir_preview_label.setProperty("secondary", True)
        self._dir_preview_label.setWordWrap(True)
        preview_layout.addWidget(self._dir_preview_label)

        layout.addWidget(preview_group)

        # ── Buttons (Cancel left, Export right — per project convention)
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("clearBtn")
        cancel_btn.setFixedHeight(28)
        cancel_btn.setMinimumWidth(80)
        cancel_btn.clicked.connect(self.reject)

        self._export_btn = QPushButton("Export")
        self._export_btn.setFixedHeight(28)
        self._export_btn.setMinimumWidth(100)
        self._export_btn.clicked.connect(self._on_export_clicked)

        btn_row.addWidget(cancel_btn)
        btn_row.addSpacing(6)
        btn_row.addWidget(self._export_btn)
        layout.addLayout(btn_row)

    # ── Callbacks ─────────────────────────────────────────────────────────

    def _on_browse(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Select Export Directory", self.export_directory
        )
        if directory:
            self._dir_input.setText(directory)

    def _on_export_clicked(self) -> None:
        name = self._name_input.text().strip()
        if not name:
            self._name_input.setFocus()
            return
        self.accept()

    def _update_preview(self) -> None:
        """Update the file preview with current selections."""
        name = self.export_name
        port_safe = self._port_name.replace("/", "_").replace("\\", "_")
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        base = f"{name}_{port_safe}_{timestamp}"

        fmt = self.export_format
        if fmt == ExportFormat.TXT:
            self._preview_label.setText(f"\U0001F4C4  {base}.txt")
        elif fmt == ExportFormat.CSV:
            self._preview_label.setText(f"\U0001F4C4  {base}.csv")
        else:
            self._preview_label.setText(
                f"\U0001F4C4  {base}.txt\n\U0001F4C4  {base}.csv"
            )

        directory = self.export_directory
        display_dir = "..." + directory[-47:] if len(directory) > 50 else directory
        self._dir_preview_label.setText(f"\U0001F4C1  {display_dir}")
