# SPDX-License-Identifier: GPL-3.0-or-later
"""The About dialog: what WireTrace is, who makes it, where it lives."""

from __future__ import annotations

import logging

from PySide6.QtCore import QSize, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QWidget

from app.icon_loader import app_icon_pixmap
from app.licenses import LicensesNotFoundError
from ui.dialogs.common import action_button, button_row, dialog_layout, heading, note, rule
from ui.dialogs.messages import tell
from ui.themes import tokens
from ui.themes.theme_manager import ThemeManager
from ui.widgets.dot_grid import DotGridDialog
from ui.widgets.styling import plain_label
from version import (
    APP_AUTHOR,
    APP_DEVELOPER,
    APP_LICENSE,
    APP_LICENSE_URL,
    APP_NAME,
    APP_REPOSITORY_URL,
    APP_SUPPORT_URL,
    APP_VERSION,
    APP_WEBSITE,
    COPYRIGHT,
)

logger = logging.getLogger(__name__)

_SUPPORT_TEXT = (
    f"{APP_NAME} is free and open source. If it saves you time, you can support the work."
)


#: What the licence asks an interactive program to state.
_LEGAL_NOTICE = f"{APP_NAME} is free software and comes with no warranty."


def _without_scheme(url: str) -> str:
    return url.split("://", 1)[-1]


class AboutDialog(DotGridDialog):
    """Product name and version, the people behind it, and its links."""

    def __init__(
        self, theme_manager: ThemeManager | None = None, parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, glow=True)
        self.setWindowTitle(f"About {APP_NAME}")
        self.setModal(True)
        metrics = tokens.METRICS

        layout = dialog_layout(self)

        logo = app_icon_pixmap(metrics.logo_medium)
        if not logo.isNull():
            picture = plain_label()
            picture.setPixmap(logo)
            picture.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            layout.addWidget(picture)

        name = heading(APP_NAME, "displayTitle")
        name.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(name)
        layout.addWidget(note(f"Version {APP_VERSION}", centred=True))
        layout.addWidget(rule())

        # Links are coloured when the theme changes, so the labels are kept.
        self._links: list[tuple[QLabel, str, str]] = []
        facts = QGridLayout()
        facts.setHorizontalSpacing(metrics.space_6)
        facts.setVerticalSpacing(metrics.space_2)
        facts.setColumnStretch(1, 1)
        rows: tuple[tuple[str, str, str], ...] = (
            ("Developer", APP_DEVELOPER, ""),
            ("Company", APP_AUTHOR, ""),
            ("Website", APP_WEBSITE, APP_WEBSITE),
            ("Source", _without_scheme(APP_REPOSITORY_URL), APP_REPOSITORY_URL),
            ("Licence", APP_LICENSE, APP_LICENSE_URL),
        )
        self._facts: list[tuple[str, str]] = []
        for row, (label, text, url) in enumerate(rows):
            facts.addWidget(heading(label), row, 0)
            value = plain_label(text)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
            if url:
                # A link is markup, written by _colour_links.
                value.setTextFormat(Qt.TextFormat.RichText)
                value.setOpenExternalLinks(True)
                self._links.append((value, text, url))
            facts.addWidget(value, row, 1)
            self._facts.append((label, text))
        layout.addLayout(facts)
        layout.addWidget(note(_LEGAL_NOTICE, small=True))
        self._colour_links()
        if theme_manager is not None:
            theme_manager.theme_changed.connect(self._colour_links)

        self.support_button = None
        if APP_SUPPORT_URL:
            layout.addWidget(rule())
            support = QHBoxLayout()
            support.setSpacing(metrics.space_4)
            support.addWidget(note(_SUPPORT_TEXT), 1)
            self.support_button = action_button("Support")
            self.support_button.clicked.connect(
                lambda: QDesktopServices.openUrl(QUrl(APP_SUPPORT_URL))
            )
            support.addWidget(self.support_button, 0, Qt.AlignmentFlag.AlignVCenter)
            layout.addLayout(support)

        layout.addStretch(1)
        self.licenses_button = action_button("Third-party licences...")
        self.licenses_button.clicked.connect(self._show_licenses)
        close = action_button("Close", primary=True)
        close.clicked.connect(self.accept)
        layout.addLayout(button_row(
            self.licenses_button, close, leading=note(COPYRIGHT, small=True, wrap=False),
        ))

        # Its size is fixed: what its content asks for, and never less
        # than a form dialog's floor.
        self.setFixedSize(self.sizeHint().expandedTo(QSize(metrics.dialog_width, 0)))

    def _show_licenses(self) -> None:
        from ui.dialogs.licenses_dialog import LicensesDialog

        try:
            dialog = LicensesDialog(self)
        except LicensesNotFoundError as exc:
            logger.error("%s", exc)
            tell(
                self, "Third-party licences",
                f"The licence texts are not installed. Please reinstall {APP_NAME}.",
            )
            return
        dialog.exec()

    def facts(self) -> list[tuple[str, str]]:
        """Each line of the dialog as (label, text)."""
        return list(self._facts)

    def _colour_links(self, *_: object) -> None:
        colour = tokens.palette().accent_strong
        for label, text, url in self._links:
            label.setText(f'<a href="{url}" style="color: {colour};">{text}</a>')
