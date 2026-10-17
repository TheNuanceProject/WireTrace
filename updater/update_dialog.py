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
from pathlib import Path

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QProgressBar, QTextEdit, QWidget

from ui.dialogs.common import action_button, button_row, dialog_layout, heading, note, tighten
from ui.dialogs.messages import confirm
from ui.formatting import format_size
from ui.themes import tokens
from ui.widgets.dot_grid import DotGridDialog
from ui.widgets.least_size import keep_least_size
from ui.widgets.styling import plain_label
from updater.background import Cancelled, Task
from updater.download import (
    DownloadCancelled,
    DownloadError,
    download,
    is_verified_file,
    remove_other_downloads,
    update_cache_dir,
)
from updater.install import InstallKind, install_kind, platform_key, user_agent
from updater.manifest import ReleaseManifest, is_newer
from updater.update_manager import UpdateManager
from version import APP_NAME, APP_VERSION

_PERCENT = 100


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
        self._task: Task | None = None
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

        self._progress = QProgressBar()
        self._progress.setRange(0, _PERCENT)
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(metrics.progress_height)
        self._progress.setVisible(False)
        layout.addWidget(self._progress)
        self._status = note()
        self._status.setVisible(False)
        layout.addWidget(self._status)

        self._skip = action_button("Skip this version")
        self._skip.clicked.connect(self._on_skip)
        self._remind = action_button("Remind later")
        self._remind.clicked.connect(self._on_remind)
        self._cancel = action_button("Cancel")
        self._cancel.setVisible(False)
        self._cancel.clicked.connect(self._on_cancel)
        self._main = action_button("", primary=True)
        self._main.setDefault(True)
        self._main.clicked.connect(self._on_main)
        self._show_offer()

        leading = QWidget()
        row = QHBoxLayout(leading)
        tighten(row)
        for button in (self._skip, self._remind, self._cancel):
            row.addWidget(button)
        layout.addStretch(1)
        layout.addLayout(button_row(self._main, leading=leading))

    # ── States ───────────────────────────────────────────────────────────

    def _show_offer(self) -> None:
        self._main.setText("Update now" if self.can_self_update else "Open the release page")
        self._main.setEnabled(True)
        self._skip.setVisible(True)
        self._remind.setVisible(True)
        self._cancel.setVisible(False)

    def _show_downloading(self) -> None:
        self._main.setText("Downloading...")
        self._main.setEnabled(False)
        self._skip.setVisible(False)
        self._remind.setVisible(False)
        self._cancel.setVisible(True)
        self._cancel.setEnabled(True)
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._say("Connecting to the server...")

    def _show_ready(self, installer: Path, *, already_there: bool) -> None:
        self._installer = installer
        self._progress.setValue(_PERCENT)
        self._progress.setVisible(True)
        self._say(
            "The installer was already downloaded and is verified" if already_there
            else "Download complete and verified"
        )
        self._main.setText("Install and restart")
        self._main.setEnabled(True)
        self._skip.setVisible(False)
        self._remind.setVisible(False)
        self._cancel.setVisible(False)

    def _say(self, text: str) -> None:
        self._status.setText(text)
        self._status.setVisible(True)

    def status_text(self) -> str:
        return self._status.text() if not self._status.isHidden() else ""

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
            self._start_download()
        else:
            self.answer = UpdateAnswer.RELEASE_PAGE
            if QDesktopServices.openUrl(QUrl(self._manifest.release_page)):
                self.accept()
            else:
                self._say(f"The page could not be opened: {self._manifest.release_page}")

    # ── Download ─────────────────────────────────────────────────────────

    def _start_download(self) -> None:
        release = self._release
        try:
            destination = update_cache_dir() / release.file_name
            if is_verified_file(destination, release.file_size, release.sha256):
                self._show_ready(destination, already_there=True)
                return
            remove_other_downloads()
        except DownloadError as exc:
            self._on_failed(str(exc))
            return
        url = self._manifest.download_url(platform_key())
        headers = {"User-Agent": user_agent()}

        def work(task: Task) -> Path:
            try:
                return download(
                    url, destination, release.file_size, release.sha256, headers,
                    progress=task.progress.emit, cancelled=task.is_cancelled,
                    opener=self._manager.opener,
                )
            except DownloadCancelled:
                raise Cancelled from None

        task = Task(work, (DownloadError,), self)
        task.progress.connect(self._on_progress)
        task.succeeded.connect(self._on_downloaded)
        task.failed.connect(self._on_failed)
        task.cancelled.connect(self._on_cancelled)
        self._task = task
        self._show_downloading()
        task.start()

    def _on_downloaded(self, path: Path) -> None:
        self._show_ready(path, already_there=False)

    def _on_progress(self, received: int, total: int) -> None:
        percent = received * _PERCENT // total
        self._progress.setValue(percent)
        self._say(f"Downloading... {format_size(received)} of {format_size(total)} ({percent}%)")

    def _on_failed(self, reason: str) -> None:
        self._show_offer()
        self._progress.setVisible(False)
        self._say(f"The update could not be downloaded: {reason}")

    def _on_cancelled(self) -> None:
        self._show_offer()
        self._progress.setVisible(False)
        self._status.setVisible(False)

    def _on_cancel(self) -> None:
        if self._task is not None and self._task.running:
            self._cancel.setEnabled(False)
            self._say("Cancelling the download...")
            self._task.cancel()

    # ── Closing ──────────────────────────────────────────────────────────

    def reject(self) -> None:
        """Close without an answer, asking first if that abandons a download.

        The close button and the Esc key both arrive here.
        """
        if self._task is not None and self._task.running:
            if not confirm(
                self, "Cancel download", "A download is in progress. Cancel it and close?",
                proceed="Cancel download", stay="Keep downloading",
            ):
                return
            self._task.cancel()
        super().reject()
