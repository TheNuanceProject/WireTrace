# SPDX-License-Identifier: GPL-3.0-or-later
"""The window that offers an update and, if chosen, downloads it.

An installation that can update itself is offered "Update now": the
installer is downloaded to the application's own folder, checked against
the size and SHA-256 of the signed manifest, and only then offered for
installing. The word "verified" appears only after that check has passed.

An installation that cannot update itself (a ``.deb``, a run from source,
macOS, or a version too old to update directly) is offered the release
page instead of a download it could not use.
"""

from __future__ import annotations

import enum
import logging
from pathlib import Path

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QTextEdit, QWidget

from ui.dialogs.common import action_button, button_row, dialog_layout, heading, note, tighten
from ui.dialogs.messages import confirm
from ui.formatting import format_size
from ui.themes import tokens
from ui.widgets.dot_grid import DotGridDialog
from ui.widgets.least_size import keep_least_size
from ui.widgets.styling import plain_label
from updater.download_panel import DownloadPanel
from updater.install import InstallKind, install_kind, platform_key
from updater.manifest import ReleaseManifest, is_newer
from updater.update_manager import UpdateManager
from version import APP_NAME, APP_VERSION

logger = logging.getLogger(__name__)

#: How the download is named in what a failed one says.
UPDATE_DOWNLOAD = "the update"


class UpdateAnswer(enum.Enum):
    """How the user left the update window."""

    DISMISSED = "dismissed"
    SKIPPED = "skipped"
    SNOOZED = "snoozed"
    INSTALL = "install"
    RELEASE_PAGE = "release page"


class UpdateDialog(DotGridDialog):
    """Offers one release.

    Signals:
        install_requested(object, object):  The path of a verified installer
                                            the user chose to install now,
                                            and what the manifest says it
                                            must be (a PlatformRelease).
    """

    install_requested = Signal(object, object)

    def __init__(
        self, manifest: ReleaseManifest, manager: UpdateManager, parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._manifest = manifest
        self._manager = manager
        self._release = manifest.platforms.get(platform_key())
        self._installer: Path | None = None
        self.answer = UpdateAnswer.DISMISSED

        self.setWindowTitle("Update available")
        keep_least_size(self, tokens.METRICS.dialog_width)
        self.setModal(True)
        self._build()

    @property
    def can_self_update(self) -> bool:
        """Whether this installation can download and install the release."""
        return (
            install_kind() is not InstallKind.MANUAL
            and self._release is not None
            and not is_newer(self._manifest.min_version, APP_VERSION)
        )

    # ── Building ─────────────────────────────────────────────────────────

    def _build(self) -> None:
        metrics = tokens.METRICS
        layout = dialog_layout(self)
        layout.addWidget(heading(f"{APP_NAME} {self._manifest.version} is available", "pageTitle"))
        layout.addWidget(plain_label(f"You have version {APP_VERSION}."))

        if self._manifest.release_notes:
            layout.addWidget(heading("Release notes"))
            notes = QTextEdit()
            notes.setPlainText(self._manifest.release_notes)
            notes.setReadOnly(True)
            notes.setAccessibleName("Release notes")
            notes.setMaximumHeight(metrics.sample_height)
            layout.addWidget(notes)

        if self.can_self_update:
            layout.addWidget(note(f"Download size: {format_size(self._release.file_size)}"))
        else:
            layout.addWidget(note(
                "This installation cannot update itself. The release page has the "
                "download and the steps."
            ))

        self._download = DownloadPanel(UPDATE_DOWNLOAD, self._manager.opener)
        self._download.ready.connect(self._show_ready)
        self._download.failed.connect(self._show_failed)
        self._download.cancelled.connect(self._show_offer)
        layout.addWidget(self._download)

        self._skip = action_button("Skip this version")
        self._skip.clicked.connect(self._on_skip)
        self._remind = action_button("Remind later")
        self._remind.clicked.connect(self._on_remind)
        self._cancel = action_button("Cancel")
        self._cancel.setVisible(False)
        self._cancel.clicked.connect(self._on_cancel)
        self._release_page = action_button("Open the release page")
        self._release_page.setVisible(False)
        self._release_page.clicked.connect(self._open_release_page)
        self._main = action_button("", primary=True)
        self._main.setDefault(True)
        self._main.clicked.connect(self._on_main)

        leading = QWidget()
        row = QHBoxLayout(leading)
        tighten(row)
        for button in (self._skip, self._release_page, self._remind, self._cancel):
            row.addWidget(button)
        layout.addStretch(1)
        layout.addLayout(button_row(self._main, leading=leading))
        # Only once every button is placed: one shown before would be a
        # window of its own for a moment.
        self._show_offer()

    # ── States ───────────────────────────────────────────────────────────

    def _show_offer(self) -> None:
        self._main.setText("Update now" if self.can_self_update else "Open the release page")
        self._main.setEnabled(True)
        self._skip.setVisible(True)
        self._remind.setVisible(True)
        self._cancel.setVisible(False)
        self._release_page.setVisible(False)

    def _show_failed(self, _text: str) -> None:
        """The download failed, as the panel says: try again, or take the
        release page."""
        self._show_offer()
        self._main.setText("Try again")
        self._main.setFocus()
        self._skip.setVisible(False)
        self._release_page.setVisible(True)

    def _show_downloading(self) -> None:
        self._main.setText("Downloading...")
        self._main.setEnabled(False)
        self._skip.setVisible(False)
        self._remind.setVisible(False)
        self._cancel.setVisible(True)
        self._cancel.setEnabled(True)

    def _show_ready(self, installer: Path, _already_there: bool) -> None:
        self._installer = installer
        # The main button was disabled while downloading, which moved the
        # focus to Cancel; Enter must now take the step the window offers.
        self._main.setEnabled(True)
        self._main.setFocus()
        self._main.setText("Install and restart")
        self._skip.setVisible(False)
        self._remind.setVisible(False)
        self._cancel.setVisible(False)
        self._release_page.setVisible(False)

    def status_text(self) -> str:
        return self._download.status_text()

    # ── Answers ──────────────────────────────────────────────────────────

    def _on_skip(self) -> None:
        self._manager.skip_version(self._manifest.version)
        self.answer = UpdateAnswer.SKIPPED
        self.accept()

    def _on_remind(self) -> None:
        self._manager.snooze()
        self.answer = UpdateAnswer.SNOOZED
        self.accept()

    def _on_main(self) -> None:
        if self._installer is not None:
            self.answer = UpdateAnswer.INSTALL
            self.accept()
            self.install_requested.emit(self._installer, self._release)
        elif self.can_self_update:
            self._show_downloading()
            self._download.start(self._manifest, platform_key())
        else:
            self._open_release_page()

    def _open_release_page(self) -> None:
        self.answer = UpdateAnswer.RELEASE_PAGE
        if QDesktopServices.openUrl(QUrl(self._manifest.release_page)):
            self.accept()
        else:
            self._download.say(f"The page could not be opened: {self._manifest.release_page}")

    def _on_cancel(self) -> None:
        if self._download.cancel():
            self._cancel.setEnabled(False)

    # ── Closing ──────────────────────────────────────────────────────────

    def reject(self) -> None:
        """Close without an answer, asking first if that abandons a download.

        The close button and the Esc key both arrive here.
        """
        if self._download.running:
            if not confirm(
                self, "Cancel download", "A download is in progress. Cancel it and close?",
                proceed="Cancel download", stay="Keep downloading",
            ):
                return
            self._download.cancel()
        super().reject()
