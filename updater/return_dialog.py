# SPDX-License-Identifier: GPL-3.0-or-later
"""The window that returns to the previous version.

The release to return to is the one the signed manifest of the installed
release names, with the size and SHA-256 of each installer. It is
downloaded and verified as an update is, and installed the same way:
the Windows installer is run, or the running AppImage replaced. An
installation that cannot replace itself (a ``.deb``, a run from source,
macOS), or one for whose platform the earlier release has no installer,
is offered that release's page instead.

The window says what the return does before anything is done: whether
the settings kept from before the update are put back, that sessions
are closed as on exit, and that the newest version will be offered
again.
"""

from __future__ import annotations

import enum
import logging
from pathlib import Path

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QLabel, QWidget

from ui.dialogs.common import action_button, button_row, dialog_layout, heading, note
from ui.dialogs.messages import confirm
from ui.formatting import format_size
from ui.themes import tokens
from ui.widgets.dot_grid import DotGridDialog
from ui.widgets.least_size import keep_least_size
from ui.widgets.styling import plain_label
from updater.download import Opener
from updater.download_panel import DownloadPanel
from updater.install import InstallKind, install_kind, platform_key
from updater.manifest import PlatformRelease, Release
from version import APP_NAME, APP_VERSION

logger = logging.getLogger(__name__)


class ReturnAnswer(enum.Enum):
    """How the user left the window."""

    DISMISSED = "dismissed"
    RETURN = "return"
    RELEASE_PAGE = "release page"


def settings_sentence(version: str, kept: bool) -> str:
    """What the return does with the settings: puts back those kept from
    before the update that ``version`` made, if ``kept``."""
    if not kept:
        return "Your settings stay as they are."
    return (
        f"Your settings as they were in version {version} are put back. Saved commands, "
        "plot profiles, highlights and remembered ports are kept, and so are your "
        "choices about updates and usage statistics."
    )


class ReturnDialog(DotGridDialog):
    """Offers the return to one earlier release.

    Signals:
        return_requested(object, object):  The path of the verified
                                           installer of the earlier release,
                                           and what the manifest says it
                                           must be (a PlatformRelease).
    """

    return_requested = Signal(object, object)

    def __init__(
        self,
        release: Release,
        settings_kept: bool,
        opener: Opener | None,
        parent: QWidget | None = None,
    ) -> None:
        """``settings_kept`` is whether settings from before the update
        that ``release`` made are kept; ``opener`` is what requests go
        through, None for the network."""
        super().__init__(parent)
        self._release = release
        self._installer_release: PlatformRelease | None = release.platforms.get(platform_key())
        self._settings_kept = settings_kept
        self._opener = opener
        self._installer: Path | None = None
        self.answer = ReturnAnswer.DISMISSED

        self.setWindowTitle(f"Return to version {release.version}")
        keep_least_size(self, tokens.METRICS.dialog_width)
        self.setModal(True)
        self._build()

    @property
    def release(self) -> Release:
        return self._release

    @property
    def can_self_install(self) -> bool:
        """Whether this installation can download and install the release."""
        return install_kind() is not InstallKind.MANUAL and self._installer_release is not None

    # ── Building ─────────────────────────────────────────────────────────

    def _line(self, text: str) -> QLabel:
        label = plain_label(text)
        label.setWordWrap(True)
        return label

    def _build(self) -> None:
        version = self._release.version
        layout = dialog_layout(self)
        layout.addWidget(heading(f"Return to {APP_NAME} {version}", "pageTitle"))
        layout.addWidget(plain_label(f"You have version {APP_VERSION}."))

        if self.can_self_install:
            for text in (
                f"{APP_NAME} {version} is downloaded and verified, as an update is.",
                settings_sentence(version, self._settings_kept),
                f"Open connections and logs are closed, as when {APP_NAME} exits. "
                "Logs already written are kept.",
                f"{APP_NAME} {version} will offer the newest version again. "
                "\"Skip this version\" in that window stops it.",
            ):
                layout.addWidget(self._line(text))
            layout.addWidget(note(f"Download size: {format_size(self._installer_release.file_size)}"))
        else:
            layout.addWidget(note(
                f"This installation cannot replace itself. The release page of version "
                f"{version} has the download and the steps."
            ))

        self._download = DownloadPanel(f"version {version}", self._opener)
        self._download.ready.connect(self._show_ready)
        self._download.failed.connect(self._show_failed)
        self._download.cancelled.connect(self._show_offer)
        layout.addWidget(self._download)

        self._release_page = action_button("Open the release page")
        self._release_page.setVisible(False)
        self._release_page.clicked.connect(self._open_release_page)
        self._cancel = action_button("Cancel")
        self._cancel.clicked.connect(self._on_cancel)
        self._main = action_button("", primary=True)
        self._main.setDefault(True)
        self._main.clicked.connect(self._on_main)

        layout.addStretch(1)
        layout.addLayout(button_row(self._cancel, self._main, leading=self._release_page))
        # Only once every button is placed: one shown before would be a
        # window of its own for a moment.
        self._show_offer()

    # ── States ───────────────────────────────────────────────────────────

    def _show_offer(self) -> None:
        version = self._release.version
        self._main.setText(f"Return to {version}" if self.can_self_install else "Open the release page")
        self._main.setEnabled(True)
        self._cancel.setEnabled(True)
        self._release_page.setVisible(False)

    def _show_downloading(self) -> None:
        self._main.setText("Downloading...")
        self._main.setEnabled(False)
        self._release_page.setVisible(False)

    def _show_failed(self, _text: str) -> None:
        """The download failed, as the panel says: try again, or take the
        release page."""
        self._show_offer()
        self._main.setText("Try again")
        self._main.setFocus()
        self._release_page.setVisible(True)

    def _show_ready(self, installer: Path, _already_there: bool) -> None:
        self._installer = installer
        # The main button was disabled while downloading, which moved the
        # focus to Cancel; Enter must now take the step the window offers.
        self._main.setEnabled(True)
        self._main.setFocus()
        self._main.setText("Return and restart")
        self._release_page.setVisible(False)

    def status_text(self) -> str:
        return self._download.status_text()

    # ── Answers ──────────────────────────────────────────────────────────

    def _on_main(self) -> None:
        if self._installer is not None:
            self.answer = ReturnAnswer.RETURN
            self.accept()
            self.return_requested.emit(self._installer, self._installer_release)
        elif self.can_self_install:
            self._show_downloading()
            self._download.start(self._release, platform_key())
        else:
            self._open_release_page()

    def _on_cancel(self) -> None:
        """Cancel the download under way, or else close."""
        if self._download.cancel():
            self._cancel.setEnabled(False)
        else:
            self.reject()

    def _open_release_page(self) -> None:
        self.answer = ReturnAnswer.RELEASE_PAGE
        if QDesktopServices.openUrl(QUrl(self._release.release_page)):
            self.accept()
        else:
            self._download.say(f"The page could not be opened: {self._release.release_page}")

    # ── Closing ──────────────────────────────────────────────────────────

    def reject(self) -> None:
        """Close without returning, asking first if that abandons a download.

        The close button and the Esc key both arrive here.
        """
        if self._download.running:
            if not confirm(
                self, "Cancel download", "A download is in progress. Cancel it and close?",
                proceed="Cancel download", stay="Keep downloading",
            ):
                return
            self._download.cancel()
        self.answer = ReturnAnswer.DISMISSED
        super().reject()
