# SPDX-License-Identifier: GPL-3.0-or-later
"""Downloading one installer in a window: its progress, and what became
of it.

The update window and the window that returns to the previous version
both download an installer this way. It goes to the application's own
folder and is checked against the size and SHA-256 of the signed
manifest; the word "verified" appears only after that check has passed.
An installer already there and verified is not downloaded again.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QProgressBar, QVBoxLayout, QWidget

from ui.dialogs.common import note, tighten
from ui.formatting import format_size
from ui.themes import tokens
from updater.background import Cancelled, Task
from updater.download import (
    DownloadCancelled,
    DownloadError,
    DownloadRejected,
    DownloadStorageError,
    DownloadUnreachable,
    Opener,
    download,
    is_verified_file,
    remove_other_downloads,
    update_cache_dir,
)
from updater.install import user_agent
from updater.manifest import Release

logger = logging.getLogger(__name__)

_PERCENT = 100

#: What a failed download says: what happened and what to do, with the
#: release page as the way out. ``{what}`` is what is being downloaded,
#: as the window names it. The technical cause is logged.
DOWNLOAD_UNREACHABLE = (
    "The download server could not be reached. Check the internet connection and "
    "try again, or get {what} from the release page."
)
DOWNLOAD_REJECTED = (
    "The download did not match the release, so it was not kept. Try again, or get "
    "{what} from the release page."
)
DOWNLOAD_STORAGE = (
    "The download could not be saved on this computer. Check that the disk has free "
    "space, or get {what} from the release page."
)
DOWNLOAD_FAILED = "{what} could not be downloaded. Try again, or get {what} from the release page."

DOWNLOADED = "Download complete and verified"
ALREADY_DOWNLOADED = "The installer was already downloaded and is verified"


def _sentence(template: str, what: str) -> str:
    """``template`` with ``what`` in it, starting with a capital letter."""
    text = template.format(what=what)
    return text[:1].upper() + text[1:]


class DownloadPanel(QWidget):
    """The progress bar and the line of status of one download.

    Signals:
        ready(object, bool):  The path of the verified installer, and
                              whether it was already there.
        failed(str):          What the user is told: what happened and
                              what to do.
        cancelled():          The download stopped because it was asked to.
    """

    ready = Signal(object, bool)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, what: str, opener: Opener | None, parent: QWidget | None = None) -> None:
        """``what`` names what is downloaded in what the user is told
        ("the update", "version 2.1.0"); ``opener`` is what requests go
        through, None for the network."""
        super().__init__(parent)
        self._what = what
        self._opener = opener
        self._task: Task | None = None

        layout = QVBoxLayout(self)
        tighten(layout)
        self._progress = QProgressBar()
        self._progress.setRange(0, _PERCENT)
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(tokens.METRICS.progress_height)
        self._progress.setVisible(False)
        layout.addWidget(self._progress)
        self._status = note()
        self._status.setVisible(False)
        layout.addWidget(self._status)

    @property
    def running(self) -> bool:
        return self._task is not None and self._task.running

    def message(self, template: str) -> str:
        """``template``, one of the ``DOWNLOAD_*`` sentences, as this
        download says it."""
        return _sentence(template, self._what)

    def _plain(self, error: DownloadError) -> str:
        """What the user is told of ``error``."""
        if isinstance(error, DownloadUnreachable):
            return self.message(DOWNLOAD_UNREACHABLE)
        if isinstance(error, DownloadRejected):
            return self.message(DOWNLOAD_REJECTED)
        if isinstance(error, DownloadStorageError):
            return self.message(DOWNLOAD_STORAGE)
        return self.message(DOWNLOAD_FAILED)

    # ── Showing ──────────────────────────────────────────────────────────

    def say(self, text: str) -> None:
        self._status.setText(text)
        self._status.setVisible(True)

    def status_text(self) -> str:
        return self._status.text() if not self._status.isHidden() else ""

    def clear(self) -> None:
        """Show nothing: no progress and no status."""
        self._progress.setVisible(False)
        self._status.setVisible(False)

    # ── Downloading ──────────────────────────────────────────────────────

    def start(self, release: Release, platform: str) -> None:
        """Download the installer ``release`` names for ``platform``, unless
        a verified copy is already there."""
        installer = release.platforms[platform]
        try:
            destination = update_cache_dir() / installer.file_name
            if is_verified_file(destination, installer.file_size, installer.sha256):
                self._show_ready(destination, already_there=True)
                return
            remove_other_downloads()
        except DownloadError as exc:
            logger.warning("The installer could not be downloaded: %s", exc)
            self._on_failed(self._plain(exc))
            return
        url = release.download_url(platform)
        headers = {"User-Agent": user_agent()}
        opener = self._opener
        plain = self._plain

        def work(task: Task) -> Path:
            try:
                return download(
                    url, destination, installer.file_size, installer.sha256, headers,
                    progress=task.progress.emit, cancelled=task.is_cancelled, opener=opener,
                )
            except DownloadCancelled:
                raise Cancelled from None
            except DownloadError as exc:
                logger.warning("The installer could not be downloaded: %s", exc)
                raise DownloadError(plain(exc)) from exc

        task = Task(work, (DownloadError,), self)
        task.progress.connect(self._on_progress)
        task.succeeded.connect(lambda path: self._show_ready(path, already_there=False))
        task.failed.connect(self._on_failed)
        task.cancelled.connect(self._on_cancelled)
        self._task = task
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self.say("Connecting to the server...")
        task.start()

    def cancel(self) -> bool:
        """Ask a running download to stop. Returns whether one was running."""
        if not self.running:
            return False
        self.say("Cancelling the download...")
        self._task.cancel()
        return True

    def _show_ready(self, installer: Path, *, already_there: bool) -> None:
        self._progress.setValue(_PERCENT)
        self._progress.setVisible(True)
        self.say(ALREADY_DOWNLOADED if already_there else DOWNLOADED)
        self.ready.emit(installer, already_there)

    def _on_progress(self, received: int, total: int) -> None:
        percent = received * _PERCENT // total
        self._progress.setValue(percent)
        self.say(f"Downloading... {format_size(received)} of {format_size(total)} ({percent}%)")

    def _on_failed(self, reason: str) -> None:
        """``reason`` is what the user is told: a plain sentence for a
        download that failed, or the task's own words for a failure it did
        not expect, which are not shown."""
        known = {
            self.message(template)
            for template in (DOWNLOAD_UNREACHABLE, DOWNLOAD_REJECTED, DOWNLOAD_STORAGE, DOWNLOAD_FAILED)
        }
        text = reason if reason in known else self.message(DOWNLOAD_FAILED)
        self._progress.setVisible(False)
        self.say(text)
        self.failed.emit(text)

    def _on_cancelled(self) -> None:
        self.clear()
        self.cancelled.emit()
