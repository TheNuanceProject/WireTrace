# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests of updates with real Qt: when a check is made, what it sends,
what is remembered, the update window, and installing from the main
window. The server is a stand-in; nothing here touches the network."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
)

import app.application as application_module
import ui.dialogs.about_dialog as about_module
import ui.main_window as main_window_module
import updater.install as install
import updater.update_dialog as update_dialog
import updater.update_manager as update_manager
from app.application import WireTraceApp
from app.config import ConfigManager
from app.constants import (
    UPDATE_CHECK_INTERVAL_HOURS,
    UPDATE_CHECK_STARTUP_DELAY_S,
    UPDATE_FIRST_CHECK_VALUE,
    UPDATE_LAST_CHECK_HEADER,
    UPDATE_SERVER_DAY_HEADER,
    UPDATE_SERVER_DAY_TOLERANCE,
    UPDATE_SNOOZE_HOURS,
    ThemeID,
)
from app.licenses import LicensesNotFoundError, load_third_party_licenses
from tests.update_helpers import FakeServer, SigningKey, release, signed
from tests_gui.support import LoopbackPort, wait_until
from ui.dialogs.about_dialog import AboutDialog
from ui.dialogs.licenses_dialog import LicensesDialog
from ui.main_window import MainWindow
from ui.themes.theme_manager import ThemeManager
from ui.widgets.connect_page import ConnectPage
from ui.widgets.toast import Toast
from updater.download import update_cache_dir
from updater.install import InstallKind, UpdateApplyError, user_agent
from updater.manifest import PlatformRelease, ReleaseManifest
from updater.update_dialog import UpdateAnswer, UpdateDialog
from updater.update_manager import UpdateManager
from version import APP_NAME, APP_VERSION, UPDATE_MANIFEST_URL

INSTALLER = b"installer bytes " * 4096
_RELEASE = PlatformRelease(
    file_name="installer", file_size=len(INSTALLER), sha256=hashlib.sha256(INSTALLER).hexdigest(),
)
NOW = datetime(2026, 3, 14, 9, 30, tzinfo=timezone.utc)
#: The number the server gives the day of ``NOW``: whole days since 1970.
_TODAY = int(NOW.timestamp()) // 86_400


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class Outcome:
    """What a manager announced."""

    def __init__(self, manager: UpdateManager) -> None:
        self.available: list[tuple[ReleaseManifest, bool]] = []
        self.up_to_date = 0
        self.failed: list[str] = []
        manager.update_available.connect(lambda manifest, asked: self.available.append((manifest, asked)))
        manager.up_to_date.connect(lambda: setattr(self, "up_to_date", self.up_to_date + 1))
        manager.check_failed.connect(self.failed.append)

    @property
    def answered(self) -> bool:
        return bool(self.available or self.up_to_date or self.failed)


@pytest.fixture
def key() -> SigningKey:
    return SigningKey()


@pytest.fixture
def server(key: SigningKey) -> FakeServer:
    """A server that offers version 9.9.9 for both platforms."""
    fake = FakeServer()
    manifest = release("9.9.9", content=INSTALLER)
    fake.pages[UPDATE_MANIFEST_URL] = signed(manifest, key)
    for platform_key in manifest.platforms:
        fake.pages[manifest.download_url(platform_key)] = INSTALLER
    return fake


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def manager(qapp, config: ConfigManager, server: FakeServer, key: SigningKey, clock: Clock):
    return UpdateManager(config, opener=server, public_keys=(key.public_base64,), now=clock)


def _check(qapp: QApplication, manager: UpdateManager) -> Outcome:
    outcome = Outcome(manager)
    manager.check_now()
    wait_until(qapp, lambda: outcome.answered)
    return outcome


def _as_installed(monkeypatch: pytest.MonkeyPatch, kind: InstallKind, key: str = "windows-x86_64"):
    for module in (update_dialog, install):
        monkeypatch.setattr(module, "install_kind", lambda: kind)
    monkeypatch.setattr(update_dialog, "platform_key", lambda: key)


def _button(dialog: UpdateDialog, text: str) -> QPushButton:
    return next(b for b in dialog.findChildren(QPushButton) if b.text() == text)


def _texts(dialog: UpdateDialog) -> list[str]:
    return [label.text() for label in dialog.findChildren(QLabel)]


# ── Checking ─────────────────────────────────────────────────────────────


class TestChecking:
    def test_a_newer_signed_release_is_announced(self, qapp, manager):
        outcome = _check(qapp, manager)

        assert [(m.version, asked) for m, asked in outcome.available] == [("9.9.9", True)]
        assert manager.found.version == "9.9.9"

    def test_the_installed_version_is_not_an_update(self, qapp, manager, server, key):
        server.pages[UPDATE_MANIFEST_URL] = signed(release(APP_VERSION), key)

        outcome = _check(qapp, manager)

        assert outcome.up_to_date == 1
        assert outcome.available == []
        assert manager.found is None

    def test_an_older_release_is_never_offered(self, qapp, manager, server, key):
        server.pages[UPDATE_MANIFEST_URL] = signed(release("0.0.1"), key)

        assert _check(qapp, manager).up_to_date == 1

    def test_a_manifest_signed_by_someone_else_is_refused(self, qapp, manager, server):
        server.pages[UPDATE_MANIFEST_URL] = signed(release("9.9.9"), SigningKey())

        outcome = _check(qapp, manager)

        assert outcome.available == []
        assert outcome.failed == [update_manager.CHECK_UNVERIFIED]
        assert manager.found is None

    def test_the_unsigned_manifest_of_version_1_is_refused(self, qapp, manager, server):
        server.pages[UPDATE_MANIFEST_URL] = b'{"latest_version": "9.9.9", "platforms": {}}'

        assert _check(qapp, manager).failed == [update_manager.CHECK_UNVERIFIED]

    def test_a_manifest_that_does_not_say_when_it_was_signed_is_refused(self, qapp, manager, server, key):
        server.pages[UPDATE_MANIFEST_URL] = signed(release("9.9.9", signed_at=None), key)

        assert _check(qapp, manager).failed == [update_manager.CHECK_UNVERIFIED]

    def test_a_server_that_cannot_be_reached_is_reported(self, qapp, manager, server):
        server.error = OSError("no network")

        assert _check(qapp, manager).failed == [update_manager.CHECK_UNREACHABLE]

    def test_what_a_server_answers_is_never_shown(self, qapp, manager, server):
        server.error = OSError("Call 555-0100 now to fix your computer")

        assert "555" not in " ".join(_check(qapp, manager).failed)


class TestAnOlderManifestServedAgain:
    def test_the_newest_moment_of_signing_is_remembered(self, qapp, manager, config, server, key):
        later = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
        server.pages[UPDATE_MANIFEST_URL] = signed(release("9.9.9", signed_at=later), key)

        _check(qapp, manager)

        assert config.update_newest_manifest_signed_at == "2026-11-01T08:00:00Z"
        assert ConfigManager().update_newest_manifest_signed_at == "2026-11-01T08:00:00Z"

    def test_one_signed_before_the_newest_seen_is_refused(self, qapp, manager, config, server, key):
        config.update_newest_manifest_signed_at = "2026-11-01T08:00:00Z"
        server.pages[UPDATE_MANIFEST_URL] = signed(
            release("9.9.9", signed_at=datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)), key,
        )

        outcome = _check(qapp, manager)

        assert outcome.available == []
        assert outcome.failed == [update_manager.CHECK_OUTDATED]
        assert manager.found is None
        assert config.update_newest_manifest_signed_at == "2026-11-01T08:00:00Z"

    def test_the_same_one_again_is_accepted(self, qapp, manager, server):
        _check(qapp, manager)

        assert _check(qapp, manager).available

    def test_a_stored_moment_edited_into_nonsense_starts_again(self, qapp, manager, config):
        config.update_newest_manifest_signed_at = "yesterday"

        assert _check(qapp, manager).available
        assert config.update_newest_manifest_signed_at == "2026-10-01T09:00:00Z"

    def test_a_build_without_keys_checks_nothing(self, qapp, config, server):
        manager = UpdateManager(config, opener=server, public_keys=())
        outcome = Outcome(manager)

        manager.check_now()

        assert outcome.failed == ["This build cannot verify updates"]
        assert server.requests == []


class TestWhatACheckSends:
    def test_first_check_says_it_is_the_first(self, qapp, manager, server):
        _check(qapp, manager)

        assert server.sent(UPDATE_LAST_CHECK_HEADER) == [UPDATE_FIRST_CHECK_VALUE]
        assert server.sent("User-Agent") == [user_agent()]
        assert server.requests[0].full_url == UPDATE_MANIFEST_URL

    def test_a_request_carries_nothing_else(self, qapp, manager, server):
        _check(qapp, manager)

        sent = {name.lower() for name, _ in server.requests[0].header_items()}
        assert sent == {"user-agent", UPDATE_LAST_CHECK_HEADER.lower()}
        assert server.requests[0].data is None
        assert "?" not in server.requests[0].full_url

    def test_the_day_the_server_named_is_sent_back_next_time(self, qapp, manager, config, server):
        server.headers = {UPDATE_SERVER_DAY_HEADER: "20526"}
        _check(qapp, manager)
        assert config.update_last_check_day == "20526"

        server.headers = {UPDATE_SERVER_DAY_HEADER.lower(): "20527"}
        _check(qapp, manager)

        assert server.sent(UPDATE_LAST_CHECK_HEADER) == [UPDATE_FIRST_CHECK_VALUE, "20526"]
        assert config.update_last_check_day == "20527"
        assert ConfigManager().update_last_check_day == "20527"

    @pytest.mark.parametrize("day", ["", "tomorrow", "2026-03-14", "-1", "1234567", "12 34"])
    def test_a_day_that_is_not_a_plain_number_is_not_kept(self, qapp, manager, config, server, day):
        config.update_last_check_day = "20526"
        server.headers = {UPDATE_SERVER_DAY_HEADER: day}

        _check(qapp, manager)

        assert config.update_last_check_day == "20526"

    @pytest.mark.parametrize("offset", [-UPDATE_SERVER_DAY_TOLERANCE, 0, UPDATE_SERVER_DAY_TOLERANCE])
    def test_a_day_close_to_today_is_kept(self, qapp, manager, config, server, offset):
        day = str(_TODAY + offset)
        server.headers = {UPDATE_SERVER_DAY_HEADER: day}

        _check(qapp, manager)

        assert config.update_last_check_day == day

    @pytest.mark.parametrize(
        "offset", [-UPDATE_SERVER_DAY_TOLERANCE - 1, UPDATE_SERVER_DAY_TOLERANCE + 1, -_TODAY, 400],
    )
    def test_a_day_that_could_mark_one_installation_is_not_kept(
        self, qapp, manager, config, server, offset,
    ):
        config.update_last_check_day = str(_TODAY)
        server.headers = {UPDATE_SERVER_DAY_HEADER: str(_TODAY + offset)}

        _check(qapp, manager)

        assert config.update_last_check_day == str(_TODAY)

    @pytest.mark.parametrize("stored", ["id-4f2a", "20526; path=/", "x" * 200])
    def test_a_stored_value_that_is_not_a_day_is_never_sent(
        self, qapp, manager, config, server, stored,
    ):
        config.update_last_check_day = stored

        _check(qapp, manager)

        assert server.sent(UPDATE_LAST_CHECK_HEADER) == [UPDATE_FIRST_CHECK_VALUE]

    def test_a_refused_manifest_does_not_change_the_day(self, qapp, manager, config, server):
        server.headers = {UPDATE_SERVER_DAY_HEADER: "20526"}
        server.pages[UPDATE_MANIFEST_URL] = b"tampered"

        _check(qapp, manager)

        assert config.update_last_check_day == ""


class TestAutomaticChecks:
    @pytest.fixture(autouse=True)
    def no_cleanup_of_the_real_temp_folder(self, monkeypatch):
        monkeypatch.setattr(update_manager, "remove_legacy_downloads", lambda: None)

    def test_timers_are_set_from_the_constants(self, qapp, manager):
        manager.start()

        assert manager._first_check.isActive()
        assert manager._first_check.isSingleShot()
        assert manager._first_check.interval() == UPDATE_CHECK_STARTUP_DELAY_S * 1000
        assert manager._next_check.isActive()
        assert not manager._next_check.isSingleShot()
        assert manager._next_check.interval() == UPDATE_CHECK_INTERVAL_HOURS * 3_600_000

        manager.stop()
        assert not manager._first_check.isActive()
        assert not manager._next_check.isActive()

    def test_an_automatic_check_announces_an_update_as_not_asked_for(self, qapp, manager):
        outcome = Outcome(manager)

        manager._first_check.timeout.emit()
        wait_until(qapp, lambda: outcome.answered)

        assert [(m.version, asked) for m, asked in outcome.available] == [("9.9.9", False)]

    def test_no_automatic_check_when_switched_off(self, qapp, manager, config, server):
        config.check_updates_on_startup = False

        manager._next_check.timeout.emit()

        assert not manager.checking
        assert server.requests == []

    def test_switching_checks_on_takes_effect_without_a_restart(self, qapp, manager, config, server):
        config.check_updates_on_startup = False
        manager._next_check.timeout.emit()
        assert server.requests == []

        config.check_updates_on_startup = True
        outcome = Outcome(manager)
        manager._next_check.timeout.emit()
        wait_until(qapp, lambda: outcome.answered)

        assert len(server.requests) == 1

    def test_an_automatic_check_that_fails_or_finds_nothing_says_nothing(
        self, qapp, manager, server, key,
    ):
        outcome = Outcome(manager)
        server.error = OSError("offline")
        manager._next_check.timeout.emit()
        wait_until(qapp, lambda: not manager.checking)

        server.error = None
        server.pages[UPDATE_MANIFEST_URL] = signed(release(APP_VERSION), key)
        manager._next_check.timeout.emit()
        wait_until(qapp, lambda: not manager.checking)
        qapp.processEvents()

        assert not outcome.answered

    def test_a_check_asked_for_is_made_even_when_automatic_checks_are_off(
        self, qapp, manager, config,
    ):
        config.check_updates_on_startup = False

        assert len(_check(qapp, manager).available) == 1


class TestSkipAndSnooze:
    def _automatic(self, qapp, manager) -> Outcome:
        outcome = Outcome(manager)
        manager._next_check.timeout.emit()
        wait_until(qapp, lambda: not manager.checking)
        qapp.processEvents()
        return outcome

    def test_a_skipped_version_is_not_announced_again_even_after_a_restart(
        self, qapp, manager, server, key, clock,
    ):
        manager.skip_version("9.9.9")
        assert self._automatic(qapp, manager).available == []

        restarted = UpdateManager(
            ConfigManager(), opener=server, public_keys=(key.public_base64,), now=clock,
        )
        assert self._automatic(qapp, restarted).available == []
        assert restarted.found.version == "9.9.9"

    def test_a_later_version_than_the_skipped_one_is_announced(
        self, qapp, manager, server, key,
    ):
        manager.skip_version("9.9.9")
        server.pages[UPDATE_MANIFEST_URL] = signed(release("9.9.10"), key)

        assert [m.version for m, _ in self._automatic(qapp, manager).available] == ["9.9.10"]

    def test_a_skipped_version_is_still_shown_when_the_user_asks(self, qapp, manager):
        manager.skip_version("9.9.9")

        assert len(_check(qapp, manager).available) == 1

    def test_snooze_holds_for_its_hours_across_a_restart_and_then_ends(
        self, qapp, manager, server, key, clock,
    ):
        manager.snooze()
        assert ConfigManager().update_snoozed_until == NOW + timedelta(hours=UPDATE_SNOOZE_HOURS)

        clock.now = NOW + timedelta(hours=UPDATE_SNOOZE_HOURS, seconds=-1)
        restarted = UpdateManager(
            ConfigManager(), opener=server, public_keys=(key.public_base64,), now=clock,
        )
        assert self._automatic(qapp, restarted).available == []

        clock.now = NOW + timedelta(hours=UPDATE_SNOOZE_HOURS)
        assert len(self._automatic(qapp, restarted).available) == 1

    @pytest.mark.parametrize("stored", ["", "soon", "2026-03-14T09:30:00"])
    def test_a_stored_snooze_time_that_cannot_be_trusted_is_ignored(self, config, stored):
        config.set("Updates", "snoozed_until", stored)

        assert config.update_snoozed_until is None


class TestRestoreDefaults:
    def test_the_choice_about_update_checks_survives_restore_defaults(self, config):
        config.check_updates_on_startup = False
        config.installer_choice_read = True
        config.skipped_update_version = "9.9.9"
        config.update_last_check_day = "20526"

        config.reset_to_defaults()

        assert config.check_updates_on_startup is False
        assert config.installer_choice_read is True
        assert config.skipped_update_version == "9.9.9"
        assert config.update_last_check_day == "20526"


# ── The update window ────────────────────────────────────────────────────────


@pytest.fixture
def light(qapp) -> ThemeManager:
    themes = ThemeManager(qapp)
    themes.apply_theme(ThemeID.LIGHT)
    return themes


class TestUpdateDialog:
    def test_it_names_both_versions_the_notes_and_the_size(self, qapp, light, manager, monkeypatch):
        _as_installed(monkeypatch, InstallKind.INSTALLER)
        dialog = UpdateDialog(release("9.9.9", content=INSTALLER, notes="- Faster"), manager)

        texts = _texts(dialog)
        assert f"{APP_NAME} 9.9.9 is available" in texts
        assert f"You have version {APP_VERSION}." in texts
        assert "Download size: 64 KB" in texts
        assert dialog.findChild(QTextEdit).toPlainText() == "- Faster"
        assert dialog.findChild(QTextEdit).isReadOnly()
        assert _button(dialog, "Update now").property("primary") is True

    def test_release_notes_are_shown_as_plain_text(self, qapp, light, manager):
        dialog = UpdateDialog(release(notes='<a href="https://example.invalid">x</a>'), manager)

        assert dialog.findChild(QTextEdit).toPlainText() == '<a href="https://example.invalid">x</a>'

    @pytest.mark.parametrize(
        ("button", "answer"),
        [("Remind later", UpdateAnswer.SNOOZED), ("Skip this version", UpdateAnswer.SKIPPED)],
    )
    def test_skip_and_remind_are_recorded(self, qapp, light, manager, config, clock, button, answer):
        dialog = UpdateDialog(release("9.9.9"), manager)

        _button(dialog, button).click()

        assert dialog.answer is answer
        if answer is UpdateAnswer.SKIPPED:
            assert config.skipped_update_version == "9.9.9"
        else:
            assert config.update_snoozed_until == NOW + timedelta(hours=UPDATE_SNOOZE_HOURS)

    def test_closing_the_window_is_no_answer(self, qapp, light, manager, config):
        dialog = UpdateDialog(release("9.9.9"), manager)

        dialog.reject()

        assert dialog.answer is UpdateAnswer.DISMISSED
        assert config.skipped_update_version == ""
        assert config.update_snoozed_until is None

    def test_download_is_verified_and_then_offered_for_installing(
        self, qapp, light, manager, server, monkeypatch,
    ):
        _as_installed(monkeypatch, InstallKind.INSTALLER)
        dialog = UpdateDialog(manager_release(server), manager)
        requested: list[tuple[Path, PlatformRelease]] = []
        dialog.install_requested.connect(lambda path, release: requested.append((path, release)))
        dialog.show()

        _button(dialog, "Update now").click()
        wait_until(qapp, lambda: dialog.status_text() == "Download complete and verified")

        target = update_cache_dir() / "WireTrace-Setup-v9.9.9.exe"
        assert hashlib.sha256(target.read_bytes()).hexdigest() == hashlib.sha256(INSTALLER).hexdigest()
        assert not _button(dialog, "Install and restart").isHidden()
        for hidden in ("Skip this version", "Remind later", "Cancel"):
            assert _button(dialog, hidden).isHidden()

        _button(dialog, "Install and restart").click()

        assert [(path, chosen.sha256) for path, chosen in requested] == [
            (target, hashlib.sha256(INSTALLER).hexdigest()),
        ]
        assert dialog.answer is UpdateAnswer.INSTALL

    def test_an_installer_already_downloaded_is_not_downloaded_again(
        self, qapp, light, manager, server, monkeypatch,
    ):
        _as_installed(monkeypatch, InstallKind.INSTALLER)
        (update_cache_dir() / "WireTrace-Setup-v9.9.9.exe").write_bytes(INSTALLER)
        dialog = UpdateDialog(manager_release(server), manager)
        dialog.show()

        _button(dialog, "Update now").click()

        assert dialog.status_text() == "The installer was already downloaded and is verified"
        assert server.requests == []

    def test_a_download_that_does_not_match_is_never_called_verified(
        self, qapp, light, manager, server, monkeypatch,
    ):
        _as_installed(monkeypatch, InstallKind.INSTALLER)
        manifest = manager_release(server)
        server.pages[manifest.download_url("windows-x86_64")] = b"x" * len(INSTALLER)
        dialog = UpdateDialog(manifest, manager)
        dialog.show()

        _button(dialog, "Update now").click()
        wait_until(qapp, lambda: "could not be downloaded" in dialog.status_text())

        assert "does not match the release" in dialog.status_text()
        assert "verified" not in dialog.status_text()
        assert list(update_cache_dir().iterdir()) == []
        assert not _button(dialog, "Update now").isHidden()
        assert _button(dialog, "Update now").isEnabled()

    def test_older_downloads_are_cleared_before_a_new_one(
        self, qapp, light, manager, server, monkeypatch,
    ):
        _as_installed(monkeypatch, InstallKind.INSTALLER)
        stale = update_cache_dir() / "WireTrace-Setup-v9.9.8.exe"
        stale.write_bytes(b"old")
        dialog = UpdateDialog(manager_release(server), manager)
        dialog.show()

        _button(dialog, "Update now").click()
        wait_until(qapp, lambda: dialog.status_text() == "Download complete and verified")

        assert [path.name for path in update_cache_dir().iterdir()] == ["WireTrace-Setup-v9.9.9.exe"]

    def test_closing_during_a_download_asks_first(
        self, qapp, light, manager, server, monkeypatch,
    ):
        _as_installed(monkeypatch, InstallKind.INSTALLER)
        dialog = UpdateDialog(manager_release(server), manager)
        dialog.show()
        asked: list[str] = []

        class Running:
            running = True
            cancelled = False

            def cancel(self) -> None:
                self.cancelled = True

        dialog._task = task = Running()

        def keep(box: QMessageBox) -> None:
            asked.append(box.text())
            next(b for b in box.buttons() if b.text() == "Keep downloading").click()

        monkeypatch.setattr(QMessageBox, "exec", keep)
        dialog.reject()
        assert dialog.isVisible()
        assert task.cancelled is False

        def cancel(box: QMessageBox) -> None:
            next(b for b in box.buttons() if b.text() == "Cancel download").click()

        monkeypatch.setattr(QMessageBox, "exec", cancel)
        dialog.reject()
        assert not dialog.isVisible()
        assert task.cancelled is True
        assert asked == ["A download is in progress. Cancel it and close?"]

    @pytest.mark.parametrize(
        ("kind", "platform", "min_version"),
        [
            (InstallKind.MANUAL, "windows-x86_64", "1.0.0"),     # a .deb or a run from source
            (InstallKind.INSTALLER, "windows-aarch64", "1.0.0"),  # no installer for this processor
            (InstallKind.INSTALLER, "windows-x86_64", "99.0.0"),  # too old to update directly
        ],
    )
    def test_an_installation_that_cannot_update_itself_gets_the_release_page(
        self, qapp, light, manager, server, monkeypatch, kind, platform, min_version,
    ):
        _as_installed(monkeypatch, kind, platform)
        opened: list[QUrl] = []
        monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url) or True)
        dialog = UpdateDialog(release("9.9.9", min_version=min_version), manager)

        assert dialog.can_self_update is False
        assert not any(text.startswith("Download size") for text in _texts(dialog))
        _button(dialog, "Open the release page").click()

        assert [url.toString() for url in opened] == [
            "https://github.com/TheNuanceProject/WireTrace/releases/tag/v9.9.9"
        ]
        assert dialog.answer is UpdateAnswer.RELEASE_PAGE
        assert server.requests == []


def manager_release(server: FakeServer) -> ReleaseManifest:
    """The release the stand-in server offers."""
    return release("9.9.9", content=INSTALLER)


# ── From the main window ─────────────────────────────────────────────────────


@pytest.fixture
def window(qapp, config, light, manager):
    port = LoopbackPort()
    main_window = MainWindow(config, light, transport_factory=port.factory, updates=manager)
    main_window.show()
    qapp.processEvents()
    main_window.loopback = port
    yield main_window
    main_window.shutdown_all_tabs()


def _help_action(window: MainWindow):
    return window._act_update


class TestMainWindow:
    def test_asking_for_a_check_opens_the_update_window(self, qapp, window, monkeypatch):
        shown: list[UpdateDialog] = []
        monkeypatch.setattr(UpdateDialog, "exec", lambda dialog: shown.append(dialog))

        _help_action(window).trigger()
        assert Toast.of(window.current_tab()).message == "Checking for updates"
        wait_until(qapp, lambda: bool(shown))

        assert f"{APP_NAME} 9.9.9 is available" in _texts(shown[0])
        assert _help_action(window).text() == "Update to 9.9.9..."

    def test_an_update_found_in_the_background_is_a_message_not_a_window(
        self, qapp, window, manager, monkeypatch,
    ):
        shown: list[UpdateDialog] = []
        monkeypatch.setattr(UpdateDialog, "exec", lambda dialog: shown.append(dialog))
        toast = Toast.of(window.current_tab())

        manager._next_check.timeout.emit()
        wait_until(qapp, lambda: toast.message == f"{APP_NAME} 9.9.9 is available")

        assert shown == []
        assert toast.action_label == "See what is new"

        toast.trigger_action()
        assert len(shown) == 1

    def test_the_help_menu_keeps_the_offer_without_checking_again(
        self, qapp, window, manager, server, monkeypatch,
    ):
        shown: list[UpdateDialog] = []
        monkeypatch.setattr(UpdateDialog, "exec", lambda dialog: shown.append(dialog))
        manager._next_check.timeout.emit()
        wait_until(qapp, lambda: manager.found is not None)
        asked = len(server.requests)

        _help_action(window).trigger()

        assert len(shown) == 1
        assert len(server.requests) == asked

    def test_up_to_date_and_failure_are_said(self, qapp, window, server, key):
        toast = Toast.of(window.current_tab())
        server.pages[UPDATE_MANIFEST_URL] = signed(release(APP_VERSION), key)
        _help_action(window).trigger()
        wait_until(qapp, lambda: toast.message == f"{APP_NAME} {APP_VERSION} is the latest version")

        server.error = OSError("offline")
        _help_action(window).trigger()
        wait_until(qapp, lambda: toast.message.startswith("The update check failed"))

    def test_installing_prepares_closes_and_only_then_starts_the_installer(
        self, qapp, window, monkeypatch,
    ):
        steps: list[str] = []
        monkeypatch.setattr(
            main_window_module, "prepare_update", lambda path, release: steps.append("prepare"),
        )
        monkeypatch.setattr(
            main_window_module, "launch_update",
            lambda path, release: steps.append(
                "launch while open" if window.isVisible() else "launch"
            ),
        )
        monkeypatch.setattr(QApplication, "quit", lambda: steps.append("quit"))

        window.install_update(Path("installer"), _RELEASE)

        assert steps == ["prepare", "launch", "quit"]

    def test_installing_asks_before_it_ends_a_connection_and_stops_if_refused(
        self, qapp, window, monkeypatch,
    ):
        tab = window.current_tab()
        tab.findChild(ConnectPage).connect_requested.emit("PORT0", 115200)
        wait_until(qapp, lambda: tab.is_connected)
        steps: list[str] = []
        monkeypatch.setattr(
            main_window_module, "prepare_update", lambda path, release: steps.append("prepare"),
        )
        monkeypatch.setattr(
            main_window_module, "launch_update", lambda path, release: steps.append("launch"),
        )
        monkeypatch.setattr(QApplication, "quit", lambda: steps.append("quit"))

        def stay(box: QMessageBox) -> None:
            next(b for b in box.buttons() if b.text() == "Cancel").click()

        monkeypatch.setattr(QMessageBox, "exec", stay)
        window.install_update(Path("installer"), _RELEASE)

        assert steps == []
        assert window.isVisible()
        assert tab.is_connected

        asked = []

        def leave(box: QMessageBox) -> None:
            asked.append(box.text())
            next(b for b in box.buttons() if b.text() == "Exit").click()

        monkeypatch.setattr(QMessageBox, "exec", leave)
        window.install_update(Path("installer"), _RELEASE)

        assert steps == ["prepare", "launch", "quit"]
        assert len(asked) == 1

    def test_an_update_that_cannot_be_put_in_place_leaves_everything_open(
        self, qapp, window, monkeypatch,
    ):
        def refuse(path: Path, release: PlatformRelease) -> None:
            raise UpdateApplyError("the folder is read-only")

        steps: list[str] = []
        monkeypatch.setattr(main_window_module, "prepare_update", refuse)
        monkeypatch.setattr(
            main_window_module, "launch_update", lambda path, release: steps.append("launch"),
        )
        monkeypatch.setattr(QApplication, "quit", lambda: steps.append("quit"))

        window.install_update(Path("installer"), _RELEASE)

        assert steps == []
        assert window.isVisible()
        assert "the folder is read-only" in Toast.of(window.current_tab()).message


# ── What was chosen in the Windows installer ─────────────────────────────────


class TestTheInstallersChoice:
    """The installer offers the update check as an option. The
    application takes the choice over once, and asks nothing itself."""

    @pytest.fixture
    def application(self, config: ConfigManager) -> WireTraceApp:
        # Only this step is under test, so the application is not started.
        application = WireTraceApp.__new__(WireTraceApp)
        application._config = config
        return application

    def test_checks_are_on_when_nothing_was_chosen(self, qapp, application, config, monkeypatch):
        monkeypatch.setattr(application_module, "installer_update_choice", lambda: None)

        application._take_over_installer_choice()

        assert config.check_updates_on_startup is True
        assert config.installer_choice_read is True

    @pytest.mark.parametrize("choice", [True, False])
    def test_the_installers_choice_becomes_the_setting(
        self, qapp, application, config, monkeypatch, choice,
    ):
        monkeypatch.setattr(application_module, "installer_update_choice", lambda: choice)

        application._take_over_installer_choice()

        assert config.check_updates_on_startup is choice
        assert ConfigManager().check_updates_on_startup is choice

    def test_someone_who_already_has_preferences_keeps_their_setting(
        self, qapp, config, monkeypatch,
    ):
        """An update over 1.x shows the box ticked. That must not switch
        the check back on for a user who had switched it off."""
        config.check_updates_on_startup = False
        config.save()
        returning = ConfigManager()
        assert not returning.first_run
        application = WireTraceApp.__new__(WireTraceApp)
        application._config = returning
        monkeypatch.setattr(application_module, "installer_update_choice", lambda: True)

        application._take_over_installer_choice()

        assert returning.check_updates_on_startup is False
        assert returning.installer_choice_read is True

    def test_it_is_taken_over_once_and_preferences_count_from_then_on(
        self, qapp, application, config, monkeypatch,
    ):
        monkeypatch.setattr(application_module, "installer_update_choice", lambda: False)
        application._take_over_installer_choice()

        config.check_updates_on_startup = True
        application._take_over_installer_choice()

        assert config.check_updates_on_startup is True

    def test_starting_shows_no_window_of_its_own(self, qapp, application, monkeypatch):
        monkeypatch.setattr(application_module, "installer_update_choice", lambda: None)
        shown: list[object] = []
        monkeypatch.setattr(QMessageBox, "exec", lambda box: shown.append(box) or 0)

        application._take_over_installer_choice()

        assert shown == []


# ── The licences of what a build contains ────────────────────────────────────


class TestLicences:
    def test_the_window_shows_the_whole_text_and_it_cannot_be_edited(self, qapp):
        dialog = LicensesDialog()

        assert dialog.text() == load_third_party_licenses()
        assert dialog.findChild(QPlainTextEdit).isReadOnly()

    def test_about_opens_the_licences(self, qapp, theme_manager, monkeypatch):
        opened: list[LicensesDialog] = []
        monkeypatch.setattr(LicensesDialog, "exec", lambda dialog: opened.append(dialog) or 0)

        AboutDialog(theme_manager).licenses_button.click()

        assert len(opened) == 1
        assert "GNU LESSER GENERAL PUBLIC LICENSE" in opened[0].text()

    def test_about_says_so_when_the_texts_are_not_installed(self, qapp, theme_manager, monkeypatch):
        def missing() -> str:
            raise LicensesNotFoundError("missing")

        told: list[str] = []
        monkeypatch.setattr("ui.dialogs.licenses_dialog.load_third_party_licenses", missing)
        monkeypatch.setattr(
            about_module, "tell", lambda parent, title, text, **options: told.append(text),
        )

        AboutDialog(theme_manager).licenses_button.click()

        assert len(told) == 1
        assert "reinstall" in told[0].lower()
