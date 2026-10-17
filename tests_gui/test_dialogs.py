# SPDX-License-Identifier: GPL-3.0-or-later
"""Real-Qt smoke tests for every dialog the application can open.

Each dialog is constructed, shown, laid out, and closed against the
real Qt stack. A dialog that calls a Qt API that does not exist, or
passes it the wrong arguments, fails here instead of in a user's hands.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QStyle,
    QWidget,
)

import ui.dialogs.about_dialog as about_module
from app.config import ConfigManager
from app.constants import (
    DEFAULT_FONT_FAMILY,
    FILENAME_DEFAULT_NAME,
    ExportFormat,
    LineEnding,
    ThemeID,
)
from app.plot_config import PlotConfig, PlotProfileStore
from tests.update_helpers import release
from tests_gui.support import pixel
from ui.dialogs.about_dialog import AboutDialog
from ui.dialogs.common import ChoiceRow
from ui.dialogs.export_dialog import DEFAULT_EXPORT_NAME, ExportDialog
from ui.dialogs.new_log_dialog import NewLogDialog
from ui.dialogs.plot_config_dialog import PlotConfigDialog
from ui.dialogs.preferences_dialog import PAGES, PreferencesDialog
from ui.dialogs.saved_commands_dialog import SavedCommandsDialog
from ui.dialogs.shortcuts_dialog import ShortcutsDialog
from ui.splash_screen import SplashScreen
from ui.themes import tokens
from ui.themes.theme_manager import ThemeManager
from updater.update_dialog import UpdateDialog
from updater.update_manager import UpdateManager
from version import (
    APP_AUTHOR,
    APP_DEVELOPER,
    APP_LICENSE,
    APP_LICENSE_URL,
    APP_NAME,
    APP_VERSION,
    APP_WEBSITE,
)

_SAMPLE_PORT = "PORT0"
_SAMPLE_DATA_LINE = "RPM: 1465, Current: 2.45, Voltage: 12.49"
_MB = 1024 * 1024

DialogFactory = Callable[[ConfigManager, ThemeManager, Path], QDialog]


def _preferences(config: ConfigManager, theme: ThemeManager, _: Path) -> QDialog:
    return PreferencesDialog(config, theme)


def _about(_config: ConfigManager, theme: ThemeManager, _: Path) -> QDialog:
    return AboutDialog(theme)


def _new_log(_config: ConfigManager, _theme: ThemeManager, directory: Path) -> QDialog:
    return NewLogDialog(_SAMPLE_PORT, str(directory))


def _export(_config: ConfigManager, _theme: ThemeManager, directory: Path) -> QDialog:
    return ExportDialog(str(directory), _SAMPLE_PORT)


def _plot_config(config: ConfigManager, _theme: ThemeManager, _: Path) -> QDialog:
    return PlotConfigDialog(PlotProfileStore(config), lambda: [_SAMPLE_DATA_LINE])


def _shortcuts(_config: ConfigManager, _theme: ThemeManager, _: Path) -> QDialog:
    return ShortcutsDialog()


def _update(config: ConfigManager, _theme: ThemeManager, _: Path) -> QDialog:
    return UpdateDialog(release("9.9.9"), UpdateManager(config))


def _saved_commands(_config: ConfigManager, _theme: ThemeManager, _: Path) -> QDialog:
    return SavedCommandsDialog(["status", "reset"])


_DIALOG_FACTORIES: dict[str, DialogFactory] = {
    "saved_commands": _saved_commands,
    "preferences": _preferences,
    "about": _about,
    "new_log": _new_log,
    "export": _export,
    "plot_config": _plot_config,
    "shortcuts": _shortcuts,
    "update": _update,
}


def _texts(widget: QWidget) -> list[str]:
    return [label.text() for label in widget.findChildren(QLabel)]


def _button(widget: QWidget, text: str) -> QPushButton:
    return next(b for b in widget.findChildren(QPushButton) if b.text() == text)


def _radio(widget: QWidget, text: str) -> QRadioButton:
    return next(b for b in widget.findChildren(QRadioButton) if b.text() == text)


@pytest.mark.parametrize("theme_id", list(ThemeID))
@pytest.mark.parametrize("name", list(_DIALOG_FACTORIES))
def test_dialog_builds_and_shows(
    qapp: QApplication,
    config: ConfigManager,
    theme_manager: ThemeManager,
    tmp_path: Path,
    name: str,
    theme_id: ThemeID,
) -> None:
    assert theme_manager.apply_theme(theme_id) is True

    dialog = _DIALOG_FACTORIES[name](config, theme_manager, tmp_path)
    try:
        dialog.show()
        qapp.processEvents()
        assert dialog.isVisible()
        assert dialog.windowTitle() != ""
        assert dialog.width() > 0
        assert dialog.height() > 0
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("name", list(_DIALOG_FACTORIES))
def test_no_button_of_a_dialog_is_narrower_than_its_text(
    qapp: QApplication,
    config: ConfigManager,
    theme_manager: ThemeManager,
    tmp_path: Path,
    name: str,
) -> None:
    theme_manager.apply_theme(ThemeID.LIGHT)
    dialog = _DIALOG_FACTORIES[name](config, theme_manager, tmp_path)
    dialog.show()
    qapp.processEvents()

    for button in dialog.findChildren(QPushButton):
        if button.isVisible() and button.text():
            assert button.width() >= button.sizeHint().width(), button.text()
            top_left = button.mapTo(dialog, button.rect().topLeft())
            bottom_right = button.mapTo(dialog, button.rect().bottomRight())
            assert dialog.rect().contains(top_left), button.text()
            assert dialog.rect().contains(bottom_right), button.text()


@pytest.fixture
def light(theme_manager: ThemeManager) -> ThemeManager:
    theme_manager.apply_theme(ThemeID.LIGHT)
    return theme_manager


class TestStartALog:
    def test_empty_name_gives_the_default_and_text_only(self, qapp, light, tmp_path):
        dialog = NewLogDialog(_SAMPLE_PORT, str(tmp_path))

        assert dialog.windowTitle() == "Start a log"
        assert dialog.log_name == FILENAME_DEFAULT_NAME
        assert dialog.log_directory == str(tmp_path)
        assert dialog.export_format is ExportFormat.TXT
        assert dialog.description == ""
        [name] = dialog.preview_names()
        assert name.startswith(f"{FILENAME_DEFAULT_NAME}_{_SAMPLE_PORT}_") and name.endswith(".txt")

    def test_preview_follows_the_name_and_the_files_chosen(self, qapp, light, tmp_path):
        dialog = NewLogDialog(_SAMPLE_PORT, str(tmp_path))

        dialog.findChild(QLineEdit).setText("Motor run: 1")
        _radio(dialog, "Text and CSV").click()

        text, csv = dialog.preview_names()
        assert text.startswith(f"Motor_run_1_{_SAMPLE_PORT}_") and text.endswith(".txt")
        assert csv == text[:-4] + ".csv"
        assert dialog.export_format is ExportFormat.BOTH
        assert dialog.log_name == "Motor run: 1"

    @pytest.mark.parametrize("stored", [ExportFormat.CSV, ExportFormat.BOTH])
    def test_preference_for_a_csv_starts_on_text_and_csv(self, qapp, light, tmp_path, stored):
        dialog = NewLogDialog(_SAMPLE_PORT, str(tmp_path), stored)

        assert dialog.export_format is ExportFormat.BOTH
        assert _radio(dialog, "Text and CSV").isChecked()

    def test_split_size_is_mentioned_only_when_splitting_is_on(self, qapp, light, tmp_path):
        split = NewLogDialog(_SAMPLE_PORT, str(tmp_path), ExportFormat.TXT, 100 * _MB)
        whole = NewLogDialog(_SAMPLE_PORT, str(tmp_path), ExportFormat.TXT, 0)

        assert split.remark().endswith("A new file starts every 100 MB.")
        assert "part" not in whole.remark()

    def test_notes_and_folder_are_returned(self, qapp, light, tmp_path, monkeypatch):
        chosen = tmp_path / "chosen"
        monkeypatch.setattr(
            QFileDialog, "getExistingDirectory", lambda *args, **kwargs: str(chosen),
        )
        dialog = NewLogDialog(_SAMPLE_PORT, str(tmp_path))

        dialog.findChild(QPlainTextEdit).setPlainText("  bench 3, new firmware  ")
        _button(dialog, "Browse").click()

        assert dialog.description == "bench 3, new firmware"
        assert dialog.log_directory == str(chosen)

    def test_cancelled_folder_choice_keeps_the_folder(self, qapp, light, tmp_path, monkeypatch):
        monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args, **kwargs: "")
        dialog = NewLogDialog(_SAMPLE_PORT, str(tmp_path))

        _button(dialog, "Browse").click()

        assert dialog.log_directory == str(tmp_path)

    def test_buttons_accept_and_reject(self, qapp, light, tmp_path):
        dialog = NewLogDialog(_SAMPLE_PORT, str(tmp_path))
        results: list[int] = []
        dialog.finished.connect(results.append)

        _button(dialog, "Start log").click()
        _button(dialog, "Cancel").click()

        assert results == [QDialog.DialogCode.Accepted, QDialog.DialogCode.Rejected]
        assert _button(dialog, "Start log").property("primary") is True

    def test_port_with_path_separators_gives_a_valid_file_name(self, qapp, light, tmp_path):
        dialog = NewLogDialog("/dev/ttyUSB0", str(tmp_path))

        [name] = dialog.preview_names()

        assert "/" not in name and "\\" not in name
        assert name.startswith(f"{FILENAME_DEFAULT_NAME}__dev_ttyUSB0_")


class TestExportSession:
    def test_dialog_opens_on_what_was_chosen_last_time(self, qapp, light, tmp_path):
        dialog = ExportDialog(str(tmp_path), _SAMPLE_PORT, ExportFormat.CSV)

        assert dialog.windowTitle() == "Export session"
        assert dialog.export_format is ExportFormat.CSV
        assert dialog.export_directory == str(tmp_path)
        assert dialog.export_name == DEFAULT_EXPORT_NAME

    @pytest.mark.parametrize(
        ("choice", "export_format", "endings"),
        [
            ("Text", ExportFormat.TXT, [".txt"]),
            ("CSV", ExportFormat.CSV, [".csv"]),
            ("Both", ExportFormat.BOTH, [".txt", ".csv"]),
        ],
    )
    def test_preview_lists_the_files_of_the_choice(
        self, qapp, light, tmp_path, choice, export_format, endings,
    ):
        dialog = ExportDialog(str(tmp_path), _SAMPLE_PORT)

        _radio(dialog, choice).click()

        assert dialog.export_format is export_format
        assert [name[-4:] for name in dialog.preview_names()] == endings

    def test_export_with_no_port_says_so_in_the_file_name(self, qapp, light, tmp_path):
        dialog = ExportDialog(str(tmp_path), "")

        assert dialog.preview_names()[0].startswith(f"{DEFAULT_EXPORT_NAME}_NoPort_")


class TestChoiceRow:
    def test_unknown_value_chooses_the_first(self, qapp, light):
        row = ChoiceRow((("One", 1), ("Two", 2)), 7)

        assert row.value == 1
        assert row.texts() == ["One", "Two"]

    def test_a_click_announces_the_change_and_setting_does_not(self, qapp, light):
        row = ChoiceRow((("One", 1), ("Two", 2)), 1)
        changes: list[object] = []
        row.changed.connect(lambda: changes.append(row.value))

        row.set_value(2)
        assert changes == []

        _radio(row, "One").click()
        assert changes == [1]


class TestAbout:
    def test_dialog_states_what_version_py_states(self, qapp, light):
        dialog = AboutDialog(light)

        assert dict(dialog.facts()) == {
            "Developer": APP_DEVELOPER,
            "Company": APP_AUTHOR,
            "Website": APP_WEBSITE,
            "Source": "github.com/TheNuanceProject/WireTrace",
            "Licence": APP_LICENSE,
        }
        assert APP_NAME in _texts(dialog)
        assert f"Version {APP_VERSION}" in _texts(dialog)

    def test_dialog_states_the_licence_and_that_there_is_no_warranty(self, qapp, light):
        texts = _texts(AboutDialog(light))

        assert any(APP_LICENSE in text and APP_LICENSE_URL in text for text in texts)
        assert any("free software" in text and "no warranty" in text for text in texts)

    def test_links_take_the_link_colour_of_the_theme(self, qapp, light):
        dialog = AboutDialog(light)
        website = next(text for text in _texts(dialog) if APP_WEBSITE in text and "<a " in text)
        assert tokens.LIGHT.accent_strong in website

        light.apply_theme(ThemeID.DARK)

        website = next(text for text in _texts(dialog) if APP_WEBSITE in text and "<a " in text)
        assert tokens.DARK.accent_strong in website

    def test_no_support_button_without_a_page_to_open(self, qapp, light, monkeypatch):
        monkeypatch.setattr(about_module, "APP_SUPPORT_URL", "")

        dialog = AboutDialog(light)

        assert dialog.support_button is None
        assert "Support" not in [b.text() for b in dialog.findChildren(QPushButton)]

    def test_support_button_opens_the_support_page(self, qapp, light, monkeypatch):
        opened: list[str] = []
        monkeypatch.setattr(about_module, "APP_SUPPORT_URL", "https://example.invalid/support")
        monkeypatch.setattr(
            about_module.QDesktopServices, "openUrl",
            lambda url: opened.append(QUrl(url).toString()) or True,
        )
        dialog = AboutDialog(light)

        dialog.support_button.click()

        assert opened == ["https://example.invalid/support"]
        assert dialog.support_button.property("primary") in (None, False)


class TestSplash:
    def test_splash_names_the_step_and_fills_the_bar(self, qapp, light):
        splash = SplashScreen()
        splash.set_version("2.0.0")
        splash.show()

        splash.show_step("Building the window", 0.5)

        assert splash.status == "Building the window"
        assert splash.fraction == 0.5
        assert "v2.0.0" in _texts(splash)
        assert (splash.width(), splash.height()) == (
            tokens.METRICS.splash_width, tokens.METRICS.splash_height,
        )
        splash.close()

    @pytest.mark.parametrize(("given", "shown"), [(-1.0, 0.0), (2.0, 1.0)])
    def test_bar_stays_within_its_range(self, qapp, light, given, shown):
        splash = SplashScreen()

        splash.show_step("Step", given)

        assert splash.fraction == shown

    def test_splash_closes_the_moment_the_window_is_shown(self, qapp, light):
        splash = SplashScreen()
        splash.show()
        window = QWidget()

        splash.finish_and_close(window)
        qapp.processEvents()

        assert window.isVisible()
        assert not splash.isVisible()
        window.close()

    def test_splash_is_outlined_against_the_desktop(self, qapp, light):
        splash = SplashScreen()
        splash.show()
        qapp.processEvents()

        assert pixel(splash, 0, splash.height() // 2).name().upper() == tokens.LIGHT.edge
        splash.close()


class TestPreferences:
    @pytest.fixture
    def dialog(self, qapp, config, light) -> PreferencesDialog:
        return PreferencesDialog(config, light)

    def test_pages_are_listed_in_order_and_the_first_is_shown(self, dialog):
        assert PAGES == (
            "Appearance", "Console", "Connection", "Logging", "Plot", "Updates and privacy",
            "Advanced",
        )
        assert dialog.current_page == "Appearance"

    @pytest.mark.parametrize("page", PAGES)
    def test_every_page_can_be_shown(self, qapp, dialog, page):
        dialog.show()
        dialog.show_page(page)
        qapp.processEvents()

        assert dialog.current_page == page
        assert page in _texts(dialog._pages.currentWidget())

    def test_no_page_name_is_cut_short(self, qapp, dialog):
        dialog.show()
        qapp.processEvents()
        sidebar = dialog._sidebar
        text_margin = sidebar.style().pixelMetric(QStyle.PixelMetric.PM_FocusFrameHMargin) + 1
        padding = 2 * (tokens.METRICS.space_3 + text_margin)

        for name in PAGES:
            (item,) = sidebar.findItems(name, Qt.MatchFlag.MatchExactly)
            room = sidebar.visualItemRect(item).width() - padding
            assert room >= sidebar.fontMetrics().horizontalAdvance(name), name

    def test_every_setting_shown_is_one_that_has_an_effect(self, dialog):
        assert dialog.setting_names() == [
            "Theme",
            "Font", "Text size", "Timestamps", "Colours", "Following", "Blank lines",
            "Baud rate",
            "Log folder", "Log format", "Maximum log file size", "Line endings in files",
            "Default profile", "Profiles",
            "Updates", "Usage statistics",
            "Console line limit", "Screen refresh", "Write to disk every",
            "Lines that may wait for a slow disk",
        ]

    def test_opening_and_saving_changes_nothing(self, qapp, config, dialog):
        before = {
            section: dict(config._config.items(section)) for section in config._config.sections()
        }

        _button(dialog, "Save").click()

        after = {
            section: dict(config._config.items(section)) for section in config._config.sections()
        }
        assert after == before
        assert dialog.result() == QDialog.DialogCode.Accepted

    def test_every_value_is_saved_and_read_back(self, qapp, config, light, tmp_path):
        store = PlotProfileStore(config)
        store.upsert(PlotConfig(name="Motor", mode="auto"))
        dialog = PreferencesDialog(config, light)
        folder = tmp_path / "logs here"

        dialog._theme.setCurrentIndex(dialog._theme.findData(ThemeID.DARK.value))
        dialog._font_size.setValue(15)
        dialog._timestamps.setCurrentIndex(1)
        dialog._severity_colours.setChecked(False)
        dialog._follow.setChecked(False)
        dialog._hide_blank.setChecked(True)
        dialog._baud.setCurrentIndex(dialog._baud.findData(9600))
        dialog._log_folder.setText(f"  {folder}  ")
        dialog._log_format.set_value(ExportFormat.BOTH)
        dialog._split.setValue(250)
        dialog._line_ending.set_value(LineEnding.CRLF)
        dialog._plot_profile.setCurrentIndex(dialog._plot_profile.findData("Motor"))
        dialog._check_updates.setChecked(False)
        dialog._console_lines.setValue(20_000)
        dialog._refresh.setValue(80)
        dialog._flush.setValue(2_000)
        dialog._buffer.setValue(60_000)
        _button(dialog, "Save").click()

        saved = ConfigManager()
        assert saved.theme == ThemeID.DARK.value
        assert light.current_theme is ThemeID.DARK
        assert saved.font_size == 15
        assert saved.timestamp_mode == "relative"
        assert saved.color_mode is False
        assert saved.auto_scroll is False
        assert saved.hide_blank_lines is True
        assert saved.default_baud_rate == 9600
        assert saved.default_log_directory == str(folder)
        assert saved.default_log_format is ExportFormat.BOTH
        assert saved.log_split_size_mb == 250
        assert saved.log_line_ending is LineEnding.CRLF
        assert PlotProfileStore(saved).default_name == "Motor"
        assert saved.check_updates_on_startup is False
        assert saved.max_console_lines == 20_000
        assert saved.gui_update_interval_ms == 80
        assert saved.log_flush_interval_ms == 2_000
        assert saved.log_buffer_max_entries == 60_000

        reopened = PreferencesDialog(saved, light)
        assert reopened._timestamps.currentText() == "Since previous line"
        assert reopened._log_format.value is ExportFormat.BOTH
        assert reopened._line_ending.value is LineEnding.CRLF
        assert reopened._plot_profile.currentData() == "Motor"
        assert reopened._split.value() == 250

    def test_cancel_saves_nothing(self, qapp, config, dialog):
        dialog._font_size.setValue(dialog._font_size.value() + 3)
        size = config.font_size

        _button(dialog, "Cancel").click()

        assert config.font_size == size
        assert dialog.result() == QDialog.DialogCode.Rejected

    def test_saved_font_that_is_not_installed_is_kept(self, qapp, config, light):
        config.font_family = "A Font Nobody Has"

        dialog = PreferencesDialog(config, light)
        _button(dialog, "Save").click()

        assert config.font_family == "A Font Nobody Has"

    def test_bundled_console_font_is_offered(self, dialog):
        families = [dialog._font_family.itemText(i) for i in range(dialog._font_family.count())]

        assert DEFAULT_FONT_FAMILY in families
        assert dialog._font_family.currentText() == DEFAULT_FONT_FAMILY

    def test_baud_rate_typed_into_the_file_is_kept(self, qapp, config, light):
        config.default_baud_rate = 31_250

        dialog = PreferencesDialog(config, light)
        _button(dialog, "Save").click()

        assert config.default_baud_rate == 31_250

    def test_split_of_zero_reads_off(self, dialog):
        dialog._split.setValue(0)

        assert dialog._split.text() == "Off"

    def test_fields_carry_their_setting_name_for_screen_readers(self, dialog):
        named = {
            widget.accessibleName()
            for kind in (QComboBox, QSpinBox, QCheckBox)
            for widget in dialog.findChildren(kind)
        }

        assert {"Theme", "Font", "Text size", "Console line limit", "Updates"} <= named

    def test_restore_defaults_asks_and_cancel_keeps_everything(
        self, qapp, config, dialog, monkeypatch,
    ):
        config.font_size = 15
        asked: list[str] = []

        def cancel(box: QMessageBox) -> None:
            asked.append(box.text())
            next(b for b in box.buttons() if b.text() == "Cancel").click()

        monkeypatch.setattr(QMessageBox, "exec", cancel)

        _button(dialog, "Restore defaults").click()

        assert asked == ["Return every setting to its default?"]
        assert config.font_size == 15

    def test_restore_defaults_resets_settings_and_keeps_profiles(
        self, qapp, config, light, monkeypatch,
    ):
        PlotProfileStore(config).upsert(PlotConfig(name="Motor", mode="auto"))
        config.font_size = 15
        config.log_line_ending = LineEnding.CRLF
        dialog = PreferencesDialog(config, light)
        monkeypatch.setattr(
            QMessageBox, "exec",
            lambda box: next(b for b in box.buttons() if b.text() == "Restore defaults").click(),
        )

        _button(dialog, "Restore defaults").click()

        assert dialog._font_size.value() == ConfigManager().font_size != 15
        assert dialog._line_ending.value is LineEnding.LF
        assert "Motor" in PlotProfileStore(config).names()

    def test_configure_plot_is_offered_only_when_it_can_be_opened(self, qapp, config, light):
        without = PreferencesDialog(config, light)
        assert not _button(without, "Configure plot...").isEnabled()

        opened: list[bool] = []

        def open_and_add() -> None:
            opened.append(True)
            PlotProfileStore(config).upsert(PlotConfig(name="Added", mode="auto"))

        with_it = PreferencesDialog(config, light, open_plot_config=open_and_add)
        _button(with_it, "Configure plot...").click()

        assert opened == [True]
        assert with_it._plot_profile.findData("Added") >= 0
