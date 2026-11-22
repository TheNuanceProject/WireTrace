# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests of the settings kept before an update and put back on a return
to the version that kept them (``ConfigManager.keep_copy`` and
``ConfigManager.put_back``)."""

from __future__ import annotations

import configparser
import os
import stat
import sys

import pytest

from app import config as config_module
from app.config import ConfigManager
from app.constants import SETTINGS_COPIES_KEPT, SETTINGS_COPY_DIRECTORY


@pytest.fixture
def config(tmp_path, monkeypatch) -> ConfigManager:
    monkeypatch.setattr(config_module, "get_config_dir", lambda: str(tmp_path))
    return ConfigManager()


def _saved(config: ConfigManager, **values: str) -> ConfigManager:
    """``config`` with each ``section.key=value`` set and saved."""
    for name, value in values.items():
        section, key = name.split("__")
        config.set(section, key, value)
    assert config.save()
    return config


def _on_disk(config: ConfigManager) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config.config_path, encoding="utf-8")
    return parser


def _copies(config: ConfigManager) -> list[str]:
    return sorted(os.listdir(os.path.dirname(config.copy_path("1.0.0"))))


class TestKeepingACopy:
    def test_the_copy_is_the_file_as_saved_named_for_its_version(self, config):
        _saved(config, General__theme="dark")

        config.keep_copy("2.1.0")

        assert config.has_copy("2.1.0")
        assert not config.has_copy("2.0.0")
        assert os.path.basename(os.path.dirname(config.copy_path("2.1.0"))) == SETTINGS_COPY_DIRECTORY
        with open(config.copy_path("2.1.0"), "rb") as copy, open(config.config_path, "rb") as file:
            assert copy.read() == file.read()

    @pytest.mark.skipif(sys.platform == "win32", reason="permissions are POSIX modes here")
    def test_the_copy_is_private_to_its_owner(self, config):
        _saved(config)

        config.keep_copy("2.1.0")

        assert stat.S_IMODE(os.stat(config.copy_path("2.1.0")).st_mode) == 0o600

    def test_only_the_copies_of_the_newest_versions_are_kept(self, config):
        _saved(config)
        for version in ("2.10.0", "2.9.1", "2.2.0", "2.9.0"):
            config.keep_copy(version)

        assert SETTINGS_COPIES_KEPT == 2
        assert _copies(config) == ["preferences-2.10.0.ini", "preferences-2.9.1.ini"]

    def test_a_copy_made_again_by_the_same_version_replaces_it(self, config):
        _saved(config, General__theme="dark")
        config.keep_copy("2.1.0")
        _saved(config, General__theme="light")

        config.keep_copy("2.1.0")

        with open(config.copy_path("2.1.0"), encoding="utf-8") as copy:
            assert "theme = light" in copy.read()

    def test_a_file_in_the_folder_not_named_as_a_copy_is_left_alone(self, config):
        _saved(config)
        config.keep_copy("2.0.0")
        other = os.path.join(os.path.dirname(config.copy_path("2.0.0")), "notes.txt")
        with open(other, "w", encoding="utf-8") as file:
            file.write("mine")

        for version in ("2.1.0", "2.2.0", "2.3.0"):
            config.keep_copy(version)

        assert os.path.isfile(other)

    def test_without_a_preferences_file_no_copy_is_made(self, config):
        with pytest.raises(OSError):
            config.keep_copy("2.1.0")

        assert not config.has_copy("2.1.0")


class TestPuttingBack:
    def test_settings_are_put_back_and_the_users_own_data_is_kept(self, config):
        _saved(
            config, General__theme="dark", Display__font_size="15",
            Commands__saved_json='["before"]', Privacy__usage_statistics="false",
        )
        config.keep_copy("2.1.0")
        _saved(
            config, General__theme="light", Display__font_size="18",
            Commands__saved_json='["before", "after"]', Privacy__usage_statistics="true",
            Updates__skipped_version="9.9.9",
        )

        assert config.put_back("2.1.0") is True

        disk = _on_disk(config)
        assert disk.get("General", "theme") == "dark"
        assert disk.get("Display", "font_size") == "15"
        assert disk.get("Commands", "saved_json") == '["before", "after"]'
        assert disk.get("Privacy", "usage_statistics") == "true"
        assert disk.get("Updates", "skipped_version") == "9.9.9"
        # And the manager holds what the file holds.
        assert config.theme == "dark"
        assert config.usage_statistics is True

    def test_saving_afterwards_writes_only_what_changed_since(self, config):
        _saved(config, General__theme="dark")
        config.keep_copy("2.1.0")
        _saved(config, General__theme="light")
        config.put_back("2.1.0")

        config.window_width = 1111
        assert config.save()

        disk = _on_disk(config)
        assert disk.get("General", "theme") == "dark"
        assert disk.get("WindowState", "width") == "1111"

    def test_a_setting_another_window_saved_since_is_kept_if_it_is_the_users_own(self, config):
        _saved(config, General__theme="dark")
        config.keep_copy("2.1.0")
        other = ConfigManager()
        other.set("Ports", "remembered_json", '{"COM7": {}}')
        assert other.save()

        config.put_back("2.1.0")

        assert _on_disk(config).get("Ports", "remembered_json") == '{"COM7": {}}'

    def test_without_a_copy_nothing_changes(self, config):
        _saved(config, General__theme="light")
        with open(config.config_path, "rb") as file:
            before = file.read()

        assert config.put_back("2.1.0") is False

        with open(config.config_path, "rb") as file:
            assert file.read() == before

    def test_a_copy_that_cannot_be_read_changes_nothing(self, config):
        _saved(config, General__theme="light")
        config.keep_copy("2.1.0")
        with open(config.copy_path("2.1.0"), "wb") as copy:
            copy.write(b"\xff\xfe not preferences [")

        assert config.put_back("2.1.0") is False

        assert _on_disk(config).get("General", "theme") == "light"
