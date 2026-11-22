# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests of returning to the previous version with real Qt: how the
release to return to is known, the return window, and the return from
the main window, with the settings kept from before the update. The
server is a stand-in; nothing here touches the network."""

from __future__ import annotations

import base64
import configparser
import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton

import ui.main_window as main_window_module
import updater.install as install
import updater.return_dialog as return_dialog
import updater.update_manager as update_manager
from app.config import ConfigManager
from app.constants import INSTALLED_MANIFEST_FILENAME, ThemeID
from tests.update_helpers import FakeServer, SigningKey, release, signed
from tests_gui.support import LoopbackPort, wait_until
from ui.formatting import format_size
from ui.main_window import MainWindow
from ui.themes.theme_manager import ThemeManager
from ui.widgets.toast import Toast
from updater.download import update_cache_dir
from updater.install import InstallKind, UpdateApplyError
from updater.manifest import PlatformRelease, Release, ReleaseManifest
from updater.return_dialog import ReturnAnswer, ReturnDialog, settings_sentence
from updater.update_manager import RETURN_CHECK_FAILED, RETURN_UNKNOWN, UpdateManager
from version import APP_NAME, APP_VERSION, RELEASES_URL, UPDATE_MANIFEST_URL

INSTALLER = b"earlier installer " * 4096
EARLIER = "0.9.0"
NOW = datetime(2026, 3, 14, 9, 30, tzinfo=timezone.utc)


def _earlier() -> Release:
    manifest = release(EARLIER, content=INSTALLER)
    return Release(version=manifest.version, platforms=manifest.platforms)


def _installed(previous: Release | None = None) -> ReleaseManifest:
    """The manifest of the installed release, naming ``previous``."""
    return dataclasses.replace(release(APP_VERSION), previous=previous or _earlier())


@pytest.fixture
def key() -> SigningKey:
    return SigningKey()


@pytest.fixture
def server(key: SigningKey) -> FakeServer:
    """A server whose latest release is the one installed, naming the
    earlier one; it serves the earlier release's installers."""
    fake = FakeServer()
    fake.pages[UPDATE_MANIFEST_URL] = signed(_installed(), key)
    earlier = _earlier()
    for platform_key in earlier.platforms:
        fake.pages[earlier.download_url(platform_key)] = INSTALLER
    return fake


@pytest.fixture
def manager(qapp, config: ConfigManager, server: FakeServer, key: SigningKey):
    return UpdateManager(config, opener=server, public_keys=(key.public_base64,), now=lambda: NOW)


@pytest.fixture
def kept_manifest(isolated_cache_dir: Path) -> Path:
    return isolated_cache_dir / INSTALLED_MANIFEST_FILENAME


@pytest.fixture
def light(qapp) -> ThemeManager:
    themes = ThemeManager(qapp)
    themes.apply_theme(ThemeID.LIGHT)
    return themes


def _altered(envelope: bytes) -> bytes:
    """``envelope`` with its payload naming another earlier version, the
    signature left as it was."""
    data = json.loads(envelope)
    payload = base64.b64decode(data["payload"]).replace(EARLIER.encode(), b"0.9.1")
    data["payload"] = base64.b64encode(payload).decode("ascii")
    return json.dumps(data).encode()


def _check(qapp: QApplication, manager: UpdateManager) -> None:
    answered: list[object] = []
    for signal in (manager.up_to_date, manager.update_available, manager.check_failed):
        signal.connect(lambda *args: answered.append(args))
    manager.check_now()
    wait_until(qapp, lambda: bool(answered))


class Found:
    """What ``find_return`` answered."""

    def __init__(self, manager: UpdateManager) -> None:
        self.found: list[Release] = []
        self.not_found: list[str] = []
        manager.return_found.connect(self.found.append)
        manager.return_not_found.connect(self.not_found.append)

    @property
    def answered(self) -> bool:
        return bool(self.found or self.not_found)


def _as_installed(monkeypatch: pytest.MonkeyPatch, kind: InstallKind, key: str = "windows-x86_64"):
    for module in (return_dialog, install):
        monkeypatch.setattr(module, "install_kind", lambda: kind)
    monkeypatch.setattr(return_dialog, "platform_key", lambda: key)


def _button(dialog: ReturnDialog, text: str) -> QPushButton:
    return next(b for b in dialog.findChildren(QPushButton) if b.text() == text)


def _texts(dialog: ReturnDialog) -> list[str]:
    return [label.text() for label in dialog.findChildren(QLabel)]


# ── Knowing the release to return to ────────────────────────────────────────


class TestTheReleaseToReturnTo:
    def test_a_check_that_finds_the_installed_release_names_the_one_before_and_keeps_it(
        self, qapp, manager, kept_manifest, server,
    ):
        named: list[Release | None] = []
        manager.returnable_changed.connect(named.append)
        assert manager.returnable is None

        _check(qapp, manager)

        assert manager.returnable == _earlier()
        assert named == [_earlier()]
        assert kept_manifest.read_bytes() == server.pages[UPDATE_MANIFEST_URL]

    def test_it_is_known_at_the_next_start_without_a_check(
        self, qapp, config, manager, key, server,
    ):
        _check(qapp, manager)
        asked = len(server.requests)
        later = UpdateManager(config, opener=server, public_keys=(key.public_base64,), now=lambda: NOW)

        later.start()
        later.stop()

        assert later.returnable == _earlier()
        assert len(server.requests) == asked

    def test_it_stays_known_once_a_newer_release_is_out(self, qapp, manager, key, server):
        _check(qapp, manager)
        newer = dataclasses.replace(release("99.0.0"), previous=Release(APP_VERSION, release(APP_VERSION).platforms))
        server.pages[UPDATE_MANIFEST_URL] = signed(newer, key)

        _check(qapp, manager)

        assert manager.returnable == _earlier()

    @pytest.mark.parametrize("kept", ["another release", "untrusted key", "altered", "not a manifest"])
    def test_a_kept_manifest_that_is_not_the_installed_releases_and_trusted_names_none(
        self, qapp, config, key, server, kept_manifest, kept,
    ):
        data = {
            "another release": signed(dataclasses.replace(release("0.8.0"), previous=None), key),
            "untrusted key": signed(_installed(), SigningKey()),
            "altered": _altered(signed(_installed(), key)),
            "not a manifest": b"{}",
        }[kept]
        kept_manifest.parent.mkdir(parents=True, exist_ok=True)
        kept_manifest.write_bytes(data)
        manager = UpdateManager(config, opener=server, public_keys=(key.public_base64,), now=lambda: NOW)

        manager.start()
        manager.stop()

        assert manager.returnable is None


class TestFindingTheReturn:
    def test_a_known_release_is_found_at_once(self, qapp, manager, server):
        _check(qapp, manager)
        asked = len(server.requests)
        found = Found(manager)

        manager.find_return()

        assert found.found == [_earlier()]
        assert len(server.requests) == asked

    def test_an_unknown_release_is_looked_up(self, qapp, manager, server):
        found = Found(manager)

        manager.find_return()
        wait_until(qapp, lambda: found.answered)

        assert found.found == [_earlier()]
        assert len(server.requests) == 1

    def test_a_look_up_that_fails_says_so(self, qapp, manager, server):
        server.error = OSError("offline")
        found = Found(manager)

        manager.find_return()
        wait_until(qapp, lambda: found.answered)

        assert found.not_found == [RETURN_CHECK_FAILED]

    def test_a_newer_release_that_names_another_previous_one_is_announced_not_asked_for(
        self, qapp, manager, key, server,
    ):
        server.pages[UPDATE_MANIFEST_URL] = signed(release("99.0.0"), key)
        found = Found(manager)
        available: list[tuple[str, bool]] = []
        manager.update_available.connect(lambda manifest, asked: available.append((manifest.version, asked)))

        manager.find_return()
        wait_until(qapp, lambda: found.answered)

        assert found.not_found == [RETURN_UNKNOWN]
        assert available == [("99.0.0", False)]

    def test_a_look_up_while_a_check_is_under_way_waits_for_it(self, qapp, manager, server):
        found = Found(manager)

        manager.check_now()
        manager.find_return()
        wait_until(qapp, lambda: found.answered)

        assert found.found == [_earlier()]
        assert len(server.requests) == 1


# ── The return window ────────────────────────────────────────────────────────


class TestReturnDialog:
    @pytest.mark.parametrize("kept", [True, False])
    def test_it_says_what_the_return_does(self, qapp, light, server, monkeypatch, kept):
        _as_installed(monkeypatch, InstallKind.INSTALLER)

        dialog = ReturnDialog(_earlier(), kept, server)

        texts = _texts(dialog)
        assert dialog.windowTitle() == f"Return to version {EARLIER}"
        assert f"Return to {APP_NAME} {EARLIER}" in texts
        assert f"You have version {APP_VERSION}." in texts
        assert settings_sentence(EARLIER, kept) in texts
        assert f"Download size: {format_size(len(INSTALLER))}" in texts
        assert _button(dialog, f"Return to {EARLIER}").property("primary") is True
        assert server.requests == []

    def test_the_earlier_release_is_downloaded_verified_and_then_offered(
        self, qapp, light, server, monkeypatch,
    ):
        _as_installed(monkeypatch, InstallKind.INSTALLER)
        dialog = ReturnDialog(_earlier(), True, server)
        requested: list[tuple[Path, PlatformRelease]] = []
        dialog.return_requested.connect(lambda path, chosen: requested.append((path, chosen)))
        dialog.show()

        _button(dialog, f"Return to {EARLIER}").click()
        wait_until(qapp, lambda: dialog.status_text() == "Download complete and verified")
        assert _button(dialog, "Return and restart").hasFocus()
        _button(dialog, "Return and restart").click()

        target = update_cache_dir() / f"WireTrace-Setup-v{EARLIER}.exe"
        assert target.read_bytes() == INSTALLER
        assert requested == [(target, _earlier().platforms["windows-x86_64"])]
        assert dialog.answer is ReturnAnswer.RETURN

    def test_a_download_that_does_not_match_says_so_in_plain_words(
        self, qapp, light, server, monkeypatch,
    ):
        _as_installed(monkeypatch, InstallKind.INSTALLER)
        server.pages[_earlier().download_url("windows-x86_64")] = b"x" * len(INSTALLER)
        dialog = ReturnDialog(_earlier(), True, server)
        dialog.show()

        _button(dialog, f"Return to {EARLIER}").click()
        wait_until(qapp, lambda: "did not match" in dialog.status_text())

        assert dialog.status_text() == (
            "The download did not match the release, so it was not kept. Try again, or get "
            f"version {EARLIER} from the release page."
        )
        assert _button(dialog, "Try again").isVisible()
        assert _button(dialog, "Try again").hasFocus()
        assert _button(dialog, "Open the release page").isVisible()
        assert list(update_cache_dir().iterdir()) == []

    @pytest.mark.parametrize(
        ("kind", "platform"),
        [(InstallKind.MANUAL, "windows-x86_64"), (InstallKind.INSTALLER, "windows-aarch64")],
    )
    def test_an_installation_that_cannot_replace_itself_gets_the_release_page(
        self, qapp, light, server, monkeypatch, kind, platform,
    ):
        _as_installed(monkeypatch, kind, platform)
        opened: list[QUrl] = []
        monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url) or True)
        dialog = ReturnDialog(_earlier(), True, server)

        assert dialog.can_self_install is False
        assert settings_sentence(EARLIER, True) not in _texts(dialog)
        _button(dialog, "Open the release page").click()

        assert [url.toString() for url in opened] == [_earlier().release_page]
        assert dialog.answer is ReturnAnswer.RELEASE_PAGE
        assert server.requests == []

    def test_cancel_closes_without_returning(self, qapp, light, server, monkeypatch):
        _as_installed(monkeypatch, InstallKind.INSTALLER)
        dialog = ReturnDialog(_earlier(), True, server)
        dialog.show()

        _button(dialog, "Cancel").click()

        assert not dialog.isVisible()
        assert dialog.answer is ReturnAnswer.DISMISSED


# ── From the main window ─────────────────────────────────────────────────────


@pytest.fixture
def window(qapp, config, light, manager):
    port = LoopbackPort()
    main_window = MainWindow(config, light, transport_factory=port.factory, updates=manager)
    main_window.show()
    qapp.processEvents()
    yield main_window
    main_window.shutdown_all_tabs()


def _on_disk(config: ConfigManager) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config.config_path, encoding="utf-8")
    return parser


class TestFromTheMainWindow:
    def test_the_help_menu_names_the_release_once_it_is_known(self, qapp, window, manager):
        assert window._act_return.text() == "Return to the previous version..."

        _check(qapp, manager)

        assert window._act_return.text() == f"Return to version {EARLIER}..."

    def test_choosing_it_looks_the_release_up_and_opens_the_window(self, qapp, window, monkeypatch):
        shown: list[ReturnDialog] = []
        monkeypatch.setattr(ReturnDialog, "exec", lambda dialog: shown.append(dialog))

        window._act_return.trigger()
        assert Toast.of(window.current_tab()).message == "Looking up the previous version"
        wait_until(qapp, lambda: bool(shown))

        assert shown[0].release == _earlier()

    def test_a_release_not_found_offers_the_release_list(self, qapp, window, key, server, monkeypatch):
        server.pages[UPDATE_MANIFEST_URL] = signed(release("99.0.0"), key)
        opened: list[str] = []
        monkeypatch.setattr(
            main_window_module.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True,
        )
        toast = Toast.of(window.current_tab())

        window._act_return.trigger()
        wait_until(qapp, lambda: toast.message == update_manager.RETURN_UNKNOWN)
        assert toast.action_label == "Open the release list"
        toast.trigger_action()

        assert opened == [RELEASES_URL]

    def test_the_window_says_whether_settings_were_kept(self, qapp, window, config, monkeypatch):
        _as_installed(monkeypatch, InstallKind.INSTALLER)
        shown: list[ReturnDialog] = []
        monkeypatch.setattr(ReturnDialog, "exec", lambda dialog: shown.append(dialog))
        assert config.save()
        config.keep_copy(EARLIER)

        window._act_return.trigger()
        wait_until(qapp, lambda: bool(shown))

        assert settings_sentence(EARLIER, True) in _texts(shown[0])

    def test_returning_puts_the_settings_back_closes_and_only_then_starts_the_installer(
        self, qapp, window, config, monkeypatch,
    ):
        config.theme = "dark"
        assert config.save()
        config.keep_copy(EARLIER)
        config.theme = "light"
        assert config.save()
        steps: list[str] = []
        monkeypatch.setattr(
            main_window_module, "prepare_update", lambda path, chosen: steps.append("prepare"),
        )

        def launch(path: Path, chosen: PlatformRelease) -> None:
            theme = _on_disk(config).get("General", "theme")
            steps.append(f"launch while open, theme {theme}" if window.isVisible() else f"launch, theme {theme}")

        monkeypatch.setattr(main_window_module, "launch_update", launch)
        monkeypatch.setattr(QApplication, "quit", lambda: steps.append("quit"))

        window.install_return(Path("installer"), _earlier().platforms["windows-x86_64"], EARLIER)

        assert steps == ["prepare", "launch, theme dark", "quit"]

    def test_a_return_that_cannot_be_put_in_place_changes_no_setting(
        self, qapp, window, config, monkeypatch,
    ):
        config.theme = "dark"
        assert config.save()
        config.keep_copy(EARLIER)
        config.theme = "light"
        assert config.save()

        def refuse(path: Path, chosen: PlatformRelease) -> None:
            raise UpdateApplyError("the folder is read-only")

        steps: list[str] = []
        monkeypatch.setattr(main_window_module, "prepare_update", refuse)
        monkeypatch.setattr(main_window_module, "launch_update", lambda path, chosen: steps.append("launch"))
        monkeypatch.setattr(QApplication, "quit", lambda: steps.append("quit"))

        window.install_return(Path("installer"), _earlier().platforms["windows-x86_64"], EARLIER)

        assert steps == []
        assert window.isVisible()
        assert _on_disk(config).get("General", "theme") == "light"
        assert Toast.of(window.current_tab()).message == (
            f"Version {EARLIER} could not be installed: the folder is read-only"
        )

    def test_settings_that_cannot_be_put_back_are_said_and_the_return_goes_on(
        self, qapp, window, config, monkeypatch,
    ):
        assert config.save()
        config.keep_copy(EARLIER)
        told: list[str] = []
        steps: list[str] = []

        def refuse(version: str) -> bool:
            raise OSError("the disk is full")

        monkeypatch.setattr(config, "put_back", refuse)
        monkeypatch.setattr(main_window_module, "prepare_update", lambda path, chosen: steps.append("prepare"))
        monkeypatch.setattr(main_window_module, "launch_update", lambda path, chosen: steps.append("launch"))
        monkeypatch.setattr(QApplication, "quit", lambda: steps.append("quit"))
        monkeypatch.setattr(QMessageBox, "exec", lambda box: told.append(box.text()))

        window.install_return(Path("installer"), _earlier().platforms["windows-x86_64"], EARLIER)

        assert told == [
            f"Your settings as they were in version {EARLIER} could not be put back, "
            "so the current ones are kept."
        ]
        assert steps == ["prepare", "launch", "quit"]

    def test_a_settings_copy_that_cannot_be_read_is_said_and_the_return_goes_on(
        self, qapp, window, config, monkeypatch,
    ):
        assert config.save()
        config.keep_copy(EARLIER)
        Path(config.copy_path(EARLIER)).write_bytes(b"\xff\xfe not settings [")
        told: list[str] = []
        steps: list[str] = []
        monkeypatch.setattr(main_window_module, "prepare_update", lambda path, chosen: steps.append("prepare"))
        monkeypatch.setattr(main_window_module, "launch_update", lambda path, chosen: steps.append("launch"))
        monkeypatch.setattr(QApplication, "quit", lambda: steps.append("quit"))
        monkeypatch.setattr(QMessageBox, "exec", lambda box: told.append(box.text()))

        window.install_return(Path("installer"), _earlier().platforms["windows-x86_64"], EARLIER)

        assert told == [
            f"Your settings as they were in version {EARLIER} could not be put back, "
            "so the current ones are kept."
        ]
        assert steps == ["prepare", "launch", "quit"]

    def test_with_no_settings_copy_nothing_is_said(self, qapp, window, config, monkeypatch):
        assert config.save()
        told: list[str] = []
        monkeypatch.setattr(main_window_module, "prepare_update", lambda path, chosen: None)
        monkeypatch.setattr(main_window_module, "launch_update", lambda path, chosen: None)
        monkeypatch.setattr(QApplication, "quit", lambda: None)
        monkeypatch.setattr(QMessageBox, "exec", lambda box: told.append(box.text()))

        window.install_return(Path("installer"), _earlier().platforms["windows-x86_64"], EARLIER)

        assert told == []

    def test_an_update_keeps_the_settings_as_saved_on_closing(self, qapp, window, config, monkeypatch):
        at_launch: list[bool] = []
        monkeypatch.setattr(main_window_module, "prepare_update", lambda path, chosen: None)

        def launch(path: Path, chosen: PlatformRelease) -> None:
            with open(config.copy_path(APP_VERSION), "rb") as copy, open(config.config_path, "rb") as file:
                at_launch.append(copy.read() == file.read())

        monkeypatch.setattr(main_window_module, "launch_update", launch)
        monkeypatch.setattr(QApplication, "quit", lambda: None)
        window.resize(window.width() + 40, window.height())

        window.install_update(Path("installer"), _earlier().platforms["windows-x86_64"])

        assert at_launch == [True]
        assert _on_disk(config).get("WindowState", "width") == str(window.width())

    def test_an_update_goes_on_when_the_settings_cannot_be_kept(self, qapp, window, config, monkeypatch):
        steps: list[str] = []

        def refuse(version: str) -> None:
            raise OSError("the disk is full")

        monkeypatch.setattr(config, "keep_copy", refuse)
        monkeypatch.setattr(main_window_module, "prepare_update", lambda path, chosen: steps.append("prepare"))
        monkeypatch.setattr(main_window_module, "launch_update", lambda path, chosen: steps.append("launch"))
        monkeypatch.setattr(QApplication, "quit", lambda: steps.append("quit"))

        window.install_update(Path("installer"), _earlier().platforms["windows-x86_64"])

        assert steps == ["prepare", "launch", "quit"]

