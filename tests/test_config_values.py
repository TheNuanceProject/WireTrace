# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for ConfigManager values: ranges, new settings, literal text."""

from __future__ import annotations

import pytest

from app import config as config_module
from app.config import ConfigManager
from app.constants import (
    DEFAULT_FONT_FAMILY,
    DEFAULT_FONT_SIZE,
    GUI_UPDATE_INTERVAL_MS_MAX,
    GUI_UPDATE_INTERVAL_MS_MIN,
    LOG_BUFFER_MAX_ENTRIES_MAX,
    LOG_BUFFER_MAX_ENTRIES_MIN,
    LOG_FLUSH_INTERVAL_MS_MAX,
    LOG_FLUSH_INTERVAL_MS_MIN,
    LOG_SPLIT_DEFAULT_MB,
    LOG_SPLIT_MAX_MB,
    MAX_CONSOLE_LINES_MAX,
    MAX_CONSOLE_LINES_MIN,
    SETTINGS_VERSION,
    THEME_FOLLOW_SYSTEM,
    WINDOW_POSITION_MAX,
    WINDOW_POSITION_MIN,
    WINDOW_SIZE_MAX,
    WINDOW_SIZE_MIN,
    ExportFormat,
    LineEnding,
)


@pytest.fixture()
def cfg(tmp_path, monkeypatch) -> ConfigManager:
    monkeypatch.setattr(config_module, "get_config_dir", lambda: str(tmp_path))
    return ConfigManager()


def _reloaded(cfg: ConfigManager) -> ConfigManager:
    cfg.save()
    fresh = ConfigManager()
    fresh.load()
    return fresh


def _write_file(tmp_path, text: str) -> None:
    (tmp_path / config_module.PREFERENCES_FILENAME).write_text(text, encoding="utf-8")


_V1_FILE = """[General]
theme = midnight_dark
check_updates_on_startup = false

[Display]
font_family = Consolas
font_size = 15
hide_blank_lines = true

[Logging]
split_size_mb = 250
"""


class TestSettingsMigration:
    """A 1.x preferences file receives the 2.0 defaults once."""

    @pytest.fixture(autouse=True)
    def _config_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_module, "get_config_dir", lambda: str(tmp_path))
        self.dir = tmp_path

    def test_new_installation_starts_at_the_current_version(self):
        cfg = ConfigManager()
        assert cfg.get_int("General", "settings_version") == SETTINGS_VERSION
        assert cfg.theme == THEME_FOLLOW_SYSTEM
        assert cfg.font_family == DEFAULT_FONT_FAMILY

    @pytest.mark.parametrize("theme", ["midnight_dark", "studio_light"])
    def test_v1_file_gets_the_new_theme_and_font(self, theme):
        _write_file(self.dir, _V1_FILE.replace("midnight_dark", theme))
        cfg = ConfigManager()
        assert cfg.theme == THEME_FOLLOW_SYSTEM
        assert cfg.font_family == DEFAULT_FONT_FAMILY
        # 1.x stored the size in points; 2.0 starts at its own size in pixels.
        assert cfg.font_size == DEFAULT_FONT_SIZE
        assert cfg.get_int("General", "settings_version") == SETTINGS_VERSION

    def test_v1_file_keeps_every_other_setting(self):
        _write_file(self.dir, _V1_FILE)
        cfg = ConfigManager()
        assert cfg.hide_blank_lines is True
        assert cfg.check_updates_on_startup is False
        assert cfg.log_split_size_mb == 250

    def test_choice_made_after_the_upgrade_is_kept(self):
        _write_file(self.dir, _V1_FILE)
        cfg = ConfigManager()
        cfg.theme = "dark"
        cfg.font_family = "Consolas"
        again = _reloaded(cfg)
        assert again.theme == "dark"
        assert again.font_family == "Consolas"
        assert _reloaded(again).theme == "dark"

    def test_upgrade_repeats_until_the_file_is_saved(self):
        """Closing without a save must not leave a half-upgraded file."""
        _write_file(self.dir, _V1_FILE)
        ConfigManager()
        assert ConfigManager().theme == THEME_FOLLOW_SYSTEM

    def test_current_file_is_left_alone(self):
        _write_file(
            self.dir,
            f"[General]\nsettings_version = {SETTINGS_VERSION}\ntheme = dark\n"
            "[Display]\nfont_family = Consolas\n",
        )
        cfg = ConfigManager()
        assert cfg.theme == "dark"
        assert cfg.font_family == "Consolas"

    def test_file_from_a_newer_version_is_left_alone(self):
        newer = SETTINGS_VERSION + 1
        _write_file(
            self.dir,
            f"[General]\nsettings_version = {newer}\ntheme = dark\n",
        )
        cfg = ConfigManager()
        assert cfg.theme == "dark"
        assert cfg.get_int("General", "settings_version") == newer

    @pytest.mark.parametrize("version", ["", "abc", "1.5", "0", "-3"])
    def test_unreadable_or_old_version_counts_as_v1(self, version):
        _write_file(
            self.dir,
            f"[General]\nsettings_version = {version}\ntheme = midnight_dark\n",
        )
        cfg = ConfigManager()
        assert cfg.theme == THEME_FOLLOW_SYSTEM
        assert cfg.get_int("General", "settings_version") == SETTINGS_VERSION

    def test_v1_file_without_the_sections_is_upgraded(self):
        _write_file(self.dir, "[Logging]\nsplit_size_mb = 250\n")
        cfg = ConfigManager()
        assert cfg.theme == THEME_FOLLOW_SYSTEM
        assert cfg.font_family == DEFAULT_FONT_FAMILY
        assert cfg.log_split_size_mb == 250

    def test_reset_to_defaults_stays_at_the_current_version(self):
        _write_file(self.dir, _V1_FILE)
        cfg = ConfigManager()
        cfg.reset_to_defaults()
        assert _reloaded(cfg).get_int("General", "settings_version") == SETTINGS_VERSION


class TestRestoringDefaults:
    def test_settings_return_to_their_defaults(self, cfg):
        cfg.font_size = cfg.font_size + 2
        cfg.hide_blank_lines = True
        cfg.log_line_ending = LineEnding.CRLF

        cfg.reset_to_defaults()

        assert cfg.hide_blank_lines is False
        assert cfg.log_line_ending is LineEnding.LF
        assert cfg.font_family == DEFAULT_FONT_FAMILY
        assert cfg.font_size == DEFAULT_FONT_SIZE

    def test_what_the_user_built_up_is_kept(self, cfg, tmp_path):
        cfg.set("Plot", "profiles_json", '[{"name":"Motor","mode":"auto"}]')
        cfg.set("Ports", "remembered_json", '{"PORT0": {"baud_rate": 9600}}')
        cfg.export_directory = str(tmp_path)
        cfg.window_width = 1234
        cfg.maximized = True

        cfg.reset_to_defaults()

        assert cfg.get("Plot", "profiles_json") == '[{"name":"Motor","mode":"auto"}]'
        assert cfg.get("Ports", "remembered_json") == '{"PORT0": {"baud_rate": 9600}}'
        assert cfg.export_directory == str(tmp_path)
        assert cfg.window_width == 1234
        assert cfg.maximized is True

    def test_default_plot_profile_is_a_setting_and_is_reset(self, cfg):
        cfg.set("Plot", "default_profile", "Motor")

        cfg.reset_to_defaults()

        assert cfg.get("Plot", "default_profile") == "Auto-detect"


class TestLiteralText:
    def test_percent_sign_survives_save_and_load(self, cfg):
        cfg.default_log_directory = "/data/100%_tests/%(name)s"
        assert _reloaded(cfg).default_log_directory == "/data/100%_tests/%(name)s"


class TestRanges:
    @pytest.mark.parametrize(
        ("key", "attribute", "minimum", "maximum"),
        [
            ("gui_update_interval_ms", "gui_update_interval_ms",
             GUI_UPDATE_INTERVAL_MS_MIN, GUI_UPDATE_INTERVAL_MS_MAX),
            ("log_buffer_max_entries", "log_buffer_max_entries",
             LOG_BUFFER_MAX_ENTRIES_MIN, LOG_BUFFER_MAX_ENTRIES_MAX),
            ("log_flush_interval_ms", "log_flush_interval_ms",
             LOG_FLUSH_INTERVAL_MS_MIN, LOG_FLUSH_INTERVAL_MS_MAX),
            ("max_console_lines", "max_console_lines",
             MAX_CONSOLE_LINES_MIN, MAX_CONSOLE_LINES_MAX),
        ],
    )
    def test_hand_edited_value_is_kept_within_its_range(
        self, cfg, key, attribute, minimum, maximum,
    ):
        cfg.set("Performance", key, -5)
        assert getattr(cfg, attribute) == minimum
        cfg.set("Performance", key, 10**9)
        assert getattr(cfg, attribute) == maximum


class TestLogSplitting:
    def test_default_size(self, cfg):
        assert cfg.log_split_size_mb == LOG_SPLIT_DEFAULT_MB
        assert cfg.log_split_bytes == LOG_SPLIT_DEFAULT_MB * 1024 * 1024

    def test_zero_turns_splitting_off(self, cfg):
        cfg.log_split_size_mb = 0
        assert _reloaded(cfg).log_split_bytes == 0

    def test_size_is_kept_within_its_range(self, cfg):
        cfg.log_split_size_mb = LOG_SPLIT_MAX_MB + 1
        assert cfg.log_split_size_mb == LOG_SPLIT_MAX_MB
        cfg.log_split_size_mb = -1
        assert cfg.log_split_size_mb == 0


class TestLogLineEnding:
    def test_lf_by_default_and_the_choice_persists(self, cfg):
        assert cfg.log_line_ending is LineEnding.LF
        cfg.log_line_ending = LineEnding.CRLF
        assert _reloaded(cfg).log_line_ending is LineEnding.CRLF

    @pytest.mark.parametrize("stored", ["", "CR", "NONE", "tabs", "lf"])
    def test_hand_edited_value_that_is_not_offered_gives_the_default(self, cfg, stored):
        cfg.set("Logging", "line_ending", stored)
        assert cfg.log_line_ending is LineEnding.LF

    @pytest.mark.parametrize("ending", [LineEnding.CR, LineEnding.NONE])
    def test_ending_that_would_break_a_file_cannot_be_chosen(self, cfg, ending):
        with pytest.raises(ValueError):
            cfg.log_line_ending = ending


class TestFileFormats:
    def test_log_format_is_text_by_default_and_the_choice_persists(self, cfg):
        assert cfg.default_log_format is ExportFormat.TXT
        cfg.default_log_format = ExportFormat.BOTH
        assert _reloaded(cfg).default_log_format is ExportFormat.BOTH

    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            ("txt", ExportFormat.TXT),
            ("csv", ExportFormat.BOTH),
            ("BOTH", ExportFormat.BOTH),
            ("pdf", ExportFormat.TXT),
            ("", ExportFormat.TXT),
        ],
    )
    def test_log_always_has_its_text_file(self, cfg, stored, expected):
        cfg.set("Export", "default_format", stored)
        assert cfg.default_log_format is expected

    def test_log_format_written_by_version_1_is_understood(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_module, "get_config_dir", lambda: str(tmp_path))
        _write_file(tmp_path, "[Export]\ndefault_format = both\n")
        assert ConfigManager().default_log_format is ExportFormat.BOTH

    def test_last_export_choices_are_remembered(self, cfg, tmp_path):
        assert cfg.export_format is ExportFormat.TXT
        assert cfg.export_directory == cfg.default_log_directory

        cfg.export_format = ExportFormat.CSV
        cfg.export_directory = str(tmp_path / "exports")
        fresh = _reloaded(cfg)

        assert fresh.export_format is ExportFormat.CSV
        assert fresh.export_directory == str(tmp_path / "exports")


class TestHideBlankLines:
    def test_off_by_default_and_persists(self, cfg):
        assert cfg.hide_blank_lines is False
        cfg.hide_blank_lines = True
        assert _reloaded(cfg).hide_blank_lines is True


class TestWindowGeometryFromTheFile:
    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            ("1200", 1200),
            ("0", WINDOW_SIZE_MIN),
            ("-40", WINDOW_SIZE_MIN),
            ("99999999999999999999", WINDOW_SIZE_MAX),
        ],
    )
    def test_a_size_is_kept_within_bounds(self, tmp_path, monkeypatch, stored, expected):
        monkeypatch.setattr(config_module, "get_config_dir", lambda: str(tmp_path))
        _write_file(tmp_path, f"[WindowState]\nwidth = {stored}\nheight = {stored}\n")

        cfg = ConfigManager()

        assert cfg.window_width == expected
        assert cfg.window_height == expected

    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            ("-1200", -1200),
            ("2934", 2934),
            ("-99999999999999999999", WINDOW_POSITION_MIN),
            ("99999999999999999999", WINDOW_POSITION_MAX),
        ],
    )
    def test_a_position_is_kept_within_bounds(self, tmp_path, monkeypatch, stored, expected):
        monkeypatch.setattr(config_module, "get_config_dir", lambda: str(tmp_path))
        _write_file(tmp_path, f"[WindowState]\nx = {stored}\ny = {stored}\n")

        cfg = ConfigManager()

        assert cfg.window_x == expected
        assert cfg.window_y == expected


class TestFirstRun:
    def test_no_preferences_file_is_a_first_run(self, cfg):
        assert cfg.first_run is True

    def test_saving_does_not_end_the_first_run_of_this_start(self, cfg):
        cfg.save()

        assert cfg.first_run is True

    def test_a_preferences_file_from_before_is_not_a_first_run(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_module, "get_config_dir", lambda: str(tmp_path))
        _write_file(tmp_path, _V1_FILE)

        assert ConfigManager().first_run is False
