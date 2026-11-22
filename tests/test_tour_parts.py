# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests of the parts of the tour that need no Qt: the system's motion
setting, the memory of the sample tab that is not kept, and the
preferences the tour keeps."""

from __future__ import annotations

import subprocess
import sys

import pytest

import app.system_motion as system_motion
from app import config as config_module
from app.config import ConfigManager
from app.constants import SAMPLE_PORT_NAME
from app.port_memory import PortMemory


@pytest.fixture
def config(tmp_path, monkeypatch) -> ConfigManager:
    monkeypatch.setattr(config_module, "get_config_dir", lambda: str(tmp_path))
    return ConfigManager()


class TestTheSystemsMotionSetting:
    def test_gnome_with_animations_off_asks_for_reduced_motion(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(system_motion.shutil, "which", lambda name: "/usr/bin/gsettings")
        monkeypatch.setattr(
            system_motion.subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="false\n", stderr=""),
        )

        assert system_motion.reduced_motion() is True

    def test_gnome_with_animations_on_does_not(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setattr(system_motion.shutil, "which", lambda name: "/usr/bin/gsettings")
        monkeypatch.setattr(
            system_motion.subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="true\n", stderr=""),
        )

        assert system_motion.reduced_motion() is False

    @pytest.mark.parametrize(("factor", "reduced"), [("0", True), ("0.5", False), ("x", False)])
    def test_kde_with_an_animation_speed_of_nothing_asks_for_reduced_motion(
        self, monkeypatch, tmp_path, factor, reduced,
    ):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(system_motion.shutil, "which", lambda name: None)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        (tmp_path / "kdeglobals").write_text(f"[KDE]\nAnimationDurationFactor={factor}\n", encoding="utf-8")

        assert system_motion.reduced_motion() is reduced

    def test_a_setting_that_cannot_be_read_does_not_reduce_motion(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setattr(system_motion.shutil, "which", lambda name: "/usr/bin/gsettings")

        def hang(*args, **kwargs):
            raise subprocess.TimeoutExpired("gsettings", 1)

        monkeypatch.setattr(system_motion.subprocess, "run", hang)

        assert system_motion.reduced_motion() is False

    @pytest.mark.skipif(sys.platform not in ("win32", "darwin"), reason="reads this system's own setting")
    def test_this_systems_setting_is_read(self):
        assert system_motion.reduced_motion() in (True, False)


class TestAMemoryNotKept:
    def test_it_remembers_for_as_long_as_it_lives_and_writes_nothing(self, config):
        memory = PortMemory(config, kept=False)

        memory.remember(SAMPLE_PORT_NAME, baud_rate=115200)

        assert memory.get(SAMPLE_PORT_NAME).baud_rate == 115200
        assert SAMPLE_PORT_NAME not in config.get("Ports", "remembered_json", fallback="")
        assert PortMemory(config).get(SAMPLE_PORT_NAME) is None


class TestWhatTheTourKeeps:
    def test_the_offer_and_the_chapters_taken_are_kept(self, config, tmp_path):
        config.tour_offered_version = "2.2.0"
        config.tour_chapters_taken = ("connecting", "plot")
        assert config.save()

        again = ConfigManager()
        assert again.tour_offered_version == "2.2.0"
        assert again.tour_chapters_taken == ("connecting", "plot")

    def test_restoring_the_defaults_keeps_them(self, config):
        config.tour_offered_version = "2.2.0"
        config.tour_chapters_taken = ("connecting",)

        config.reset_to_defaults()

        assert config.tour_offered_version == "2.2.0"
        assert config.tour_chapters_taken == ("connecting",)

