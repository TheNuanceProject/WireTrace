# SPDX-License-Identifier: MIT
"""WireTrace auto-update manager per spec section 12.

Production-grade update system:
  - Background check (QThread) — never blocks UI
  - Background download (QThread) — UI fully responsive during download
  - Cancellable download with partial file cleanup
  - Installer cache: reuses existing valid download (SHA-256 verified)
  - Temp file cleanup: removes stale WireTrace installers on startup
  - Close dialog always works (confirms if download in progress)
  - SHA-256 hash verification before install
  - HTTPS only with certificate validation

Download location:
  - Windows: %TEMP%/WireTrace-Setup-v{VER}.exe
  - macOS:   /tmp/WireTrace-v{VER}.dmg
  - Linux:   /tmp/WireTrace-v{VER}-x86_64.AppImage

Files are overwritten on re-download, not accumulated.
"""

from __future__ import annotations

import contextlib
import glob
import hashlib
import json
import logging
import os
import platform
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.request import Request, urlopen

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from version import APP_NAME, APP_VERSION, UPDATE_BASE_URL, UPDATE_JSON_FILE

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

STARTUP_DELAY_MS = 10_000  # 10 seconds after startup
SNOOZE_HOURS = 24
DOWNLOAD_CHUNK_SIZE = 65536  # 64KB chunks
STALE_FILE_MAX_AGE_DAYS = 7  # Clean up downloads older than 7 days


# ══════════════════════════════════════════════════════════════════════════════
# SECURITY
# ══════════════════════════════════════════════════════════════════════════════

def _require_https(url: str) -> None:
    """Raise ValueError if ``url`` is not an HTTPS URL.

    Defense-in-depth against a compromised or tampered update JSON.
    The update manifest is served over HTTPS, but the download URL
    inside it is attacker-controllable if the manifest is ever
    compromised. Refusing any non-HTTPS URL ensures a malicious
    manifest cannot redirect the download to an insecure scheme
    (``http://``, ``file://``, ``ftp://``) before SHA-256 verification
    would even have a chance to fail.
    """
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError(
            f"Refusing to fetch non-HTTPS URL: {url!r}"
        )



# ══════════════════════════════════════════════════════════════════════════════
# DATA
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class PlatformUpdate:
    """Update info for a single platform."""
    download_url: str = ""
    file_size: int = 0
    sha256_hash: str = ""


@dataclass
class UpdateInfo:
    """Parsed update JSON."""
    latest_version: str = ""
    release_notes: str = ""
    min_version: str = ""
    platform_update: PlatformUpdate | None = None


# ══════════════════════════════════════════════════════════════════════════════
# VERSION COMPARISON (Semantic Versioning — MAJOR.MINOR.PATCH)
# ══════════════════════════════════════════════════════════════════════════════

def parse_version(version_str: str) -> tuple[int, ...]:
    """Parse a MAJOR.MINOR.PATCH version string to a comparable tuple.

    Strips an optional leading 'v' (so 'v1.0.0' and '1.0.0' both work).
    Returns (0, 0, 0) if the string can't be parsed, which ensures the
    comparison logic treats malformed versions as lower than any real
    release and never triggers a spurious update prompt.
    """
    try:
        clean = version_str.strip().lstrip("vV")
        parts = clean.split(".")
        return tuple(int(p) for p in parts)
    except (ValueError, AttributeError):
        return (0, 0, 0)


def is_newer(remote: str, local: str) -> bool:
    """Return True if remote version is newer than local."""
    return parse_version(remote) > parse_version(local)


def get_current_platform() -> str:
    """Return platform key: 'windows', 'macos', or 'linux'."""
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    elif system == "windows":
        return "windows"
    return "linux"


# ══════════════════════════════════════════════════════════════════════════════
# TEMP FILE MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

def _get_download_path(url: str) -> str:
    """Return the deterministic local path for a download URL.

    Always uses the same filename for the same version, so repeated
    downloads overwrite rather than accumulate.
    """
    filename = url.rsplit("/", 1)[-1]
    return os.path.join(tempfile.gettempdir(), filename)


def _verify_cached_file(path: str, expected_hash: str) -> bool:
    """Return True if a cached file exists and its SHA-256 matches."""
    if not os.path.isfile(path):
        return False
    if not expected_hash:
        return False  # Can't verify — must re-download

    try:
        hasher = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                hasher.update(chunk)
        matches = hasher.hexdigest().lower() == expected_hash.lower()
        if matches:
            logger.info("Cached installer verified: %s", path)
        return matches
    except OSError:
        return False


def cleanup_stale_downloads() -> None:
    """Remove old WireTrace installer files from temp directory.

    Called once at startup. Removes WireTrace-Setup-*.exe,
    WireTrace-*.dmg, WireTrace-*.AppImage older than 7 days.
    """
    temp = tempfile.gettempdir()
    patterns = [
        os.path.join(temp, "WireTrace-Setup-*.exe"),
        os.path.join(temp, "WireTrace-*.dmg"),
        os.path.join(temp, "WireTrace-*.AppImage"),
    ]
    cutoff = datetime.now().timestamp() - (STALE_FILE_MAX_AGE_DAYS * 86400)
    removed = 0

    for pattern in patterns:
        for path in glob.glob(pattern):
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass

    if removed:
        logger.info("Cleaned up %d stale installer file(s) from temp", removed)


# ══════════════════════════════════════════════════════════════════════════════
# CHECK WORKER (runs in QThread — never blocks UI)
# ══════════════════════════════════════════════════════════════════════════════

class _CheckWorker(QThread):
    """Background thread for update check (network I/O)."""

    finished = Signal(object)  # UpdateInfo or None
    failed = Signal(str)       # error message

    def run(self) -> None:
        try:
            if not UPDATE_BASE_URL:
                logger.info("Update URL not configured — skipping update check")
                self.finished.emit(None)
                return

            url = f"{UPDATE_BASE_URL}/{UPDATE_JSON_FILE}"
            _require_https(url)
            logger.info("Checking for updates at %s", url)

            req = Request(url)
            req.add_header("User-Agent", f"{APP_NAME}/{APP_VERSION}")

            with urlopen(req, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8"))

            info = UpdateInfo(
                latest_version=data.get("latest_version", ""),
                release_notes=data.get("release_notes", ""),
                min_version=data.get("min_version", ""),
            )

            plat_key = get_current_platform()
            platforms = data.get("platforms", {})
            if plat_key in platforms:
                pd = platforms[plat_key]
                info.platform_update = PlatformUpdate(
                    download_url=pd.get("download_url", ""),
                    file_size=pd.get("file_size", 0),
                    sha256_hash=pd.get("sha256_hash", ""),
                )

            self.finished.emit(info)

        except Exception as e:
            logger.warning("Update check failed: %s", e)
            self.failed.emit(str(e))


# ══════════════════════════════════════════════════════════════════════════════
# DOWNLOAD WORKER (runs in QThread — cancellable)
# ══════════════════════════════════════════════════════════════════════════════

class _DownloadWorker(QThread):
    """Background thread for downloading an update installer.

    Supports cancellation via request_cancel(). On cancel:
      - Download stops at the next chunk boundary
      - Partial file is deleted
      - cancelled signal is emitted
    """

    progress = Signal(int, int)    # (bytes_downloaded, total_bytes)
    finished = Signal(str)         # path to downloaded file
    failed = Signal(str)           # error message
    cancelled = Signal()           # download was cancelled

    def __init__(self, info: UpdateInfo, dest_path: str, parent=None) -> None:
        super().__init__(parent)
        self._info = info
        self._dest_path = dest_path
        self._cancel_requested = False

    def request_cancel(self) -> None:
        """Request cancellation. Thread-safe (checked at chunk boundaries)."""
        self._cancel_requested = True

    def run(self) -> None:
        try:
            pu = self._info.platform_update
            if not pu or not pu.download_url:
                self.failed.emit("No download URL available for this platform")
                return

            url = pu.download_url
            _require_https(url)

            logger.info("Downloading update from %s", url)
            req = Request(url)
            req.add_header("User-Agent", f"{APP_NAME}/{APP_VERSION}")

            with urlopen(req, timeout=60) as response:
                total = pu.file_size or int(
                    response.headers.get("Content-Length", 0)
                )
                downloaded = 0
                hasher = hashlib.sha256()

                with open(self._dest_path, "wb") as f:
                    while True:
                        # Check for cancellation at each chunk boundary
                        if self._cancel_requested:
                            logger.info("Download cancelled by user")
                            break

                        chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                        if not chunk:
                            break
                        f.write(chunk)
                        hasher.update(chunk)
                        downloaded += len(chunk)
                        self.progress.emit(downloaded, total)

            # Handle cancellation — clean up partial file
            if self._cancel_requested:
                self._cleanup_partial()
                self.cancelled.emit()
                return

            # Verify SHA-256 (spec section 11)
            if pu.sha256_hash:
                actual = hasher.hexdigest()
                if actual.lower() != pu.sha256_hash.lower():
                    self._cleanup_partial()
                    self.failed.emit(
                        "Download verification failed — the file may be "
                        "corrupted. Please try again."
                    )
                    return
                logger.info("SHA-256 verified: %s", actual[:16] + "...")

            self.finished.emit(self._dest_path)

        except Exception as e:
            logger.error("Download failed: %s", e)
            self._cleanup_partial()
            self.failed.emit(str(e))

    def _cleanup_partial(self) -> None:
        """Remove the partially downloaded file."""
        try:
            if os.path.exists(self._dest_path):
                os.remove(self._dest_path)
                logger.info("Removed partial download: %s", self._dest_path)
        except OSError:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# UPDATE MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class UpdateManager(QObject):
    """Manages automatic update checks.

    Signals:
        update_available(UpdateInfo):  New version found.
        update_not_available():        Current version is latest.
        check_failed(str):             Network/parse error.
    """

    update_available = Signal(object)
    update_not_available = Signal()
    check_failed = Signal(str)

    def __init__(self, config_manager=None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._config = config_manager
        self._startup_timer: QTimer | None = None
        self._check_worker: _CheckWorker | None = None

        # Snooze/skip state
        self._skipped_version: str = ""
        self._snoozed_until: datetime | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the update manager with a 10-second delay.

        Also cleans up stale installer files from temp.
        """
        # Clean up old downloads
        cleanup_stale_downloads()

        if self._config and not self._config.check_updates_on_startup:
            logger.info("Update checks disabled in preferences")
            return

        self._startup_timer = QTimer(self)
        self._startup_timer.setSingleShot(True)
        self._startup_timer.timeout.connect(self._on_startup_check)
        self._startup_timer.start(STARTUP_DELAY_MS)
        logger.info(
            "Update check scheduled in %d seconds",
            STARTUP_DELAY_MS // 1000,
        )

    def stop(self) -> None:
        """Stop the update manager."""
        if self._startup_timer:
            self._startup_timer.stop()
            self._startup_timer = None

    # ── Public API ────────────────────────────────────────────────────────

    def check_now(self, silent: bool = False) -> None:
        """Perform an update check in a background thread.

        Results delivered via signals: update_available,
        update_not_available, or check_failed.
        """
        if self._check_worker and self._check_worker.isRunning():
            return

        self._check_worker = _CheckWorker(self)
        self._check_worker.finished.connect(
            lambda info: self._on_check_result(info, silent)
        )
        self._check_worker.failed.connect(
            lambda msg: self._on_check_error(msg, silent)
        )
        self._check_worker.start()

    def snooze(self) -> None:
        """Snooze update reminder for 24 hours."""
        self._snoozed_until = datetime.now() + timedelta(hours=SNOOZE_HOURS)
        logger.info("Update snoozed until %s", self._snoozed_until)

    def skip_version(self, version: str) -> None:
        """Skip a specific version — don't show again."""
        self._skipped_version = version
        logger.info("Skipped update version: %s", version)

    # ── Internal ──────────────────────────────────────────────────────────

    def _on_check_result(self, info: UpdateInfo, silent: bool) -> None:
        if not info or not info.latest_version:
            if not silent:
                self.check_failed.emit("Invalid update response from server")
            return

        if not is_newer(info.latest_version, APP_VERSION):
            logger.info(
                "No update (current: %s, remote: %s)",
                APP_VERSION, info.latest_version,
            )
            if not silent:
                self.update_not_available.emit()
            return

        if info.latest_version == self._skipped_version:
            logger.info("Version %s was skipped by user", info.latest_version)
            if not silent:
                self.update_not_available.emit()
            return

        logger.info(
            "Update available: %s → %s", APP_VERSION, info.latest_version
        )
        self.update_available.emit(info)

    def _on_check_error(self, msg: str, silent: bool) -> None:
        if not silent:
            self.check_failed.emit(msg)

    @Slot()
    def _on_startup_check(self) -> None:
        if self._snoozed_until and datetime.now() < self._snoozed_until:
            logger.info("Update check snoozed until %s", self._snoozed_until)
            return
        self.check_now(silent=True)

    @staticmethod
    def launch_installer(installer_path: str) -> None:
        """Launch the downloaded installer and close the application."""
        if not os.path.exists(installer_path):
            logger.error("Installer not found: %s", installer_path)
            return

        system = platform.system().lower()
        try:
            if system == "windows":
                os.startfile(installer_path)  # type: ignore[attr-defined]
            elif system == "darwin":
                subprocess.Popen(["open", installer_path])
            else:
                subprocess.Popen(["xdg-open", installer_path])

            QApplication.quit()
        except Exception as e:
            logger.error("Failed to launch installer: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# UPDATE DIALOG — self-contained download + install UI
# ══════════════════════════════════════════════════════════════════════════════

class UpdateDialog(QDialog):
    """Dialog shown when an update is available.

    Features:
      - Checks for cached installer before downloading (SHA-256 verified)
      - Downloads in background thread — UI stays fully responsive
      - Cancel button stops download and cleans up partial file
      - Close button always works (confirms if download in progress)
      - Progress bar with MB and percentage display
      - Install & Restart launches installer and quits application

    Three initial options:
      - Update Now: download (or use cache) → verify → install
      - Remind Later: snooze for 24 hours
      - Skip This Version: don't show again for this version
    """

    def __init__(self, info: UpdateInfo, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Update Available")
        self.setFixedWidth(480)
        self.setModal(True)

        self._info = info
        self._result_action = "dismiss"
        self._download_worker: _DownloadWorker | None = None
        self._downloaded_path: str | None = None

        self._setup_ui()

    @property
    def result_action(self) -> str:
        """Return 'update', 'remind', 'skip', or 'dismiss'."""
        return self._result_action

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 20, 24, 20)

        # Title
        title = QLabel(
            f"<b style='font-size:14px;'>{APP_NAME} v"
            f"{self._info.latest_version} is available</b>"
        )
        layout.addWidget(title)

        current = QLabel(f"You are currently running v{APP_VERSION}.")
        layout.addWidget(current)

        # Release notes
        if self._info.release_notes:
            notes_label = QLabel("<b>Release Notes</b>")
            layout.addWidget(notes_label)

            notes = QTextEdit()
            notes.setPlainText(self._info.release_notes)
            notes.setReadOnly(True)
            notes.setMaximumHeight(100)
            layout.addWidget(notes)

        # File size
        if self._info.platform_update and self._info.platform_update.file_size:
            size_mb = self._info.platform_update.file_size / (1024 * 1024)
            size_label = QLabel(f"Download size: {size_mb:.1f} MB")
            size_label.setProperty("secondary", True)
            layout.addWidget(size_label)

        # Progress bar (hidden initially)
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setFixedHeight(20)
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        # Status label (hidden initially)
        self._status = QLabel("")
        self._status.setProperty("secondary", True)
        self._status.setVisible(False)
        layout.addWidget(self._status)

        layout.addSpacing(4)

        # Buttons — horizontal row
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)

        self._skip_btn = QPushButton("Skip This Version")
        self._skip_btn.setObjectName("clearBtn")
        self._skip_btn.clicked.connect(self._on_skip)

        self._remind_btn = QPushButton("Remind Later")
        self._remind_btn.setObjectName("clearBtn")
        self._remind_btn.clicked.connect(self._on_remind)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setObjectName("clearBtn")
        self._cancel_btn.setVisible(False)
        self._cancel_btn.clicked.connect(self._on_cancel)

        self._update_btn = QPushButton("  Update Now  ")
        self._update_btn.setDefault(True)
        self._update_btn.clicked.connect(self._on_update)

        btn_layout.addWidget(self._skip_btn)
        btn_layout.addWidget(self._remind_btn)
        btn_layout.addWidget(self._cancel_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(self._update_btn)

        layout.addLayout(btn_layout)

    # ── Button Handlers ───────────────────────────────────────────────────

    def _on_update(self) -> None:
        """Start downloading the update (or use cached installer)."""
        self._result_action = "update"

        pu = self._info.platform_update
        if not pu or not pu.download_url:
            self._show_status("No download available for this platform")
            return

        dest_path = _get_download_path(pu.download_url)

        # Check for cached installer (skip download if SHA-256 matches)
        if _verify_cached_file(dest_path, pu.sha256_hash):
            self._downloaded_path = dest_path
            self._show_download_complete(from_cache=True)
            return

        # Start download — switch to download UI state
        self._set_downloading_state(True)
        self._show_status("Connecting to server...")

        self._download_worker = _DownloadWorker(
            self._info, dest_path, self
        )
        self._download_worker.progress.connect(self._on_download_progress)
        self._download_worker.finished.connect(self._on_download_finished)
        self._download_worker.failed.connect(self._on_download_failed)
        self._download_worker.cancelled.connect(self._on_download_cancelled)
        self._download_worker.start()

    def _on_remind(self) -> None:
        self._result_action = "remind"
        self.accept()

    def _on_skip(self) -> None:
        self._result_action = "skip"
        self.accept()

    def _on_cancel(self) -> None:
        """Cancel an in-progress download."""
        if self._download_worker and self._download_worker.isRunning():
            self._show_status("Cancelling download...")
            self._cancel_btn.setEnabled(False)
            self._download_worker.request_cancel()
        else:
            self._set_downloading_state(False)

    # ── Download Signal Handlers ──────────────────────────────────────────

    @Slot(int, int)
    def _on_download_progress(self, downloaded: int, total: int) -> None:
        if total > 0:
            pct = int((downloaded / total) * 100)
            self._progress.setValue(pct)
            mb_done = downloaded / (1024 * 1024)
            mb_total = total / (1024 * 1024)
            self._status.setText(
                f"Downloading... {mb_done:.1f} / {mb_total:.1f} MB ({pct}%)"
            )
        else:
            mb_done = downloaded / (1024 * 1024)
            self._status.setText(f"Downloading... {mb_done:.1f} MB")

    @Slot(str)
    def _on_download_finished(self, path: str) -> None:
        self._downloaded_path = path
        self._show_download_complete(from_cache=False)

    @Slot(str)
    def _on_download_failed(self, error: str) -> None:
        self._set_downloading_state(False)
        self._show_status(f"Download failed: {error}")

    @Slot()
    def _on_download_cancelled(self) -> None:
        self._set_downloading_state(False)
        self._progress.setVisible(False)
        self._status.setVisible(False)

    def _on_install(self) -> None:
        """Launch the downloaded installer and close the application."""
        if not self._downloaded_path or not os.path.exists(
            self._downloaded_path
        ):
            self._show_status("Installer file not found — please retry")
            return

        self._show_status("Launching installer...")
        QApplication.processEvents()
        UpdateManager.launch_installer(self._downloaded_path)

    # ── UI State Management ───────────────────────────────────────────────

    def _set_downloading_state(self, downloading: bool) -> None:
        """Switch between initial state and downloading state."""
        self._progress.setVisible(downloading)
        self._progress.setValue(0)
        self._status.setVisible(downloading)

        # Downloading: show Cancel, hide Skip/Remind, disable Update
        self._cancel_btn.setVisible(downloading)
        self._cancel_btn.setEnabled(downloading)
        self._skip_btn.setVisible(not downloading)
        self._remind_btn.setVisible(not downloading)

        if downloading:
            self._update_btn.setEnabled(False)
            self._update_btn.setText("Downloading...")
        else:
            self._update_btn.setEnabled(True)
            self._update_btn.setText("  Update Now  ")
            # Reconnect button to update action (may have been swapped)
            with contextlib.suppress(RuntimeError):
                self._update_btn.clicked.disconnect()
            self._update_btn.clicked.connect(self._on_update)

    def _show_download_complete(self, from_cache: bool) -> None:
        """Show the Install & Restart state."""
        self._progress.setVisible(True)
        self._progress.setValue(100)
        self._status.setVisible(True)

        if from_cache:
            self._status.setText("Installer already downloaded — verified ✓")
        else:
            self._status.setText("Download complete — verified ✓")

        self._cancel_btn.setVisible(False)
        self._skip_btn.setVisible(False)
        self._remind_btn.setVisible(False)

        self._update_btn.setText("  Install && Restart  ")
        self._update_btn.setEnabled(True)
        with contextlib.suppress(RuntimeError):
            self._update_btn.clicked.disconnect()
        self._update_btn.clicked.connect(self._on_install)

    def _show_status(self, text: str) -> None:
        """Show a status message."""
        self._status.setText(text)
        self._status.setVisible(True)

    # ── Close Handling ────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        """Allow closing — cancel download if in progress."""
        if self._download_worker and self._download_worker.isRunning():
            reply = QMessageBox.question(
                self,
                "Cancel Download?",
                "A download is in progress. Cancel and close?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return

            # Cancel the download and wait for thread to finish
            self._download_worker.request_cancel()
            self._download_worker.wait(3000)

        event.accept()
