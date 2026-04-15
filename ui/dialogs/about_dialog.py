# SPDX-License-Identifier: MIT
"""WireTrace About dialog — professional product information."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from version import (
    APP_AUTHOR,
    APP_DEVELOPER,
    APP_NAME,
    APP_VERSION,
    APP_WEBSITE,
)


class AboutDialog(QDialog):
    """Professional About dialog with structured product information."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        self.setFixedSize(440, 360)
        self.setModal(True)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 20)
        layout.setSpacing(0)

        # App icon (48x48)
        from app.icon_loader import app_icon_pixmap
        pix = app_icon_pixmap(48)
        if not pix.isNull():
            icon_label = QLabel()
            icon_label.setPixmap(pix)
            icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(icon_label)
            layout.addSpacing(10)

        # App name
        name = QLabel(APP_NAME)
        nf = QFont()
        nf.setPointSize(22)
        nf.setWeight(QFont.Weight.Bold)
        name.setFont(nf)
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Version
        ver = QLabel(f"Version {APP_VERSION}")
        ver.setProperty("secondary", True)
        vf = ver.font()
        vf.setPointSize(11)
        ver.setFont(vf)
        ver.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout.addWidget(name)
        layout.addSpacing(4)
        layout.addWidget(ver)
        layout.addSpacing(18)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        layout.addWidget(sep)
        layout.addSpacing(14)

        # Info grid
        info_items = [
            ("Developer", APP_DEVELOPER),
            ("Company", APP_AUTHOR),
            (
                "Website",
                f'<a href="{APP_WEBSITE}" style="color: #1976D2; '
                f'text-decoration: none;">{APP_WEBSITE}</a>',
            ),
            ("License", "MIT"),
        ]

        for label_text, value_text in info_items:
            row = QHBoxLayout()
            row.setSpacing(12)
            lbl = QLabel(label_text)
            lf = lbl.font()
            lf.setWeight(QFont.Weight.DemiBold)
            lf.setPointSize(10)
            lbl.setFont(lf)
            lbl.setFixedWidth(80)

            val = QLabel(value_text)
            val.setTextFormat(Qt.TextFormat.RichText)
            val.setOpenExternalLinks(True)
            valf = val.font()
            valf.setPointSize(10)
            val.setFont(valf)

            row.addWidget(lbl)
            row.addWidget(val, 1)
            layout.addLayout(row)
            layout.addSpacing(4)

        layout.addStretch()
        layout.addSpacing(8)

        # Copyright
        copy_lbl = QLabel(
            f"\u00A9 2026 {APP_AUTHOR}"
        )
        copy_lbl.setProperty("secondary", True)
        cf = copy_lbl.font()
        cf.setPointSize(9)
        copy_lbl.setFont(cf)
        copy_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(copy_lbl)

        layout.addSpacing(12)

        # Close button (outlined)
        close_btn = QPushButton("Close")
        close_btn.setObjectName("clearBtn")
        close_btn.setFixedSize(80, 28)
        close_btn.clicked.connect(self.close)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)
