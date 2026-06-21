# SPDX-License-Identifier: MIT
"""Regression tests for B4 — non-atomic config file save.

The bug: ``ConfigManager.save`` used ``open(path, "w")`` directly, which
truncates ``preferences.ini`` before writing. A crash, power loss, or
disk-full between truncate and complete write left the file empty or
partial, so the next launch silently fell back to defaults — losing
every preference and saved plot profile.

The fix routes the write through ``app._atomic.atomic_write_text``
(temp sibling → flush → fsync → ``os.replace``). These tests pin the
two invariants that matter:

  1. A successful save produces a complete, parseable file and leaves no
     ``.tmp`` sibling behind.
  2. A save interrupted at *any* point never corrupts the destination:
     it stays the previous complete file. This is the property the old
     code could not provide, so these tests fail against it.

Plot-profile persistence (``PlotProfileStore._save``) flows through the
same ``ConfigManager.save``, so its atomicity is covered here too.

These run at the data layer with the PySide6 stubs from ``conftest`` —
no Qt, no real process kills (which are flaky in CI). Interruption is
simulated by injecting failures into the atomic write's filesystem
calls, which exercises the exact corruption window the bug describes.
"""

from __future__ import annotations

import configparser
import os

import pytest

import app._atomic as atomic
from app import config as config_module
from app.config import ConfigManager
from app.plot_config import PlotConfig, PlotProfileStore


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    """A real ConfigManager whose config directory is an isolated
    temp dir. Patches ``get_config_dir`` in the config module namespace
    so ``__init__`` resolves the temp path."""
    monkeypatch.setattr(config_module, "get_config_dir", lambda: str(tmp_path))
    manager = ConfigManager()
    return manager


def _config_path(cfg: ConfigManager) -> str:
    return cfg.config_path


def _tmp_sibling(path: str) -> str:
    return path + ".tmp"


def _read_parsed(path: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    return parser


class TestSuccessfulSave:
    """Happy path: complete file, no lingering temp sibling."""

    def test_save_writes_complete_parseable_file(self, cfg):
        cfg.set("General", "theme", "solarized")
        assert cfg.save() is True

        path = _config_path(cfg)
        assert os.path.isfile(path)
        # File is non-empty and parses cleanly.
        parsed = _read_parsed(path)
        assert parsed.get("General", "theme") == "solarized"

    def test_save_leaves_no_tmp_sibling(self, cfg):
        cfg.set("Display", "font_size", "18")
        assert cfg.save() is True

        path = _config_path(cfg)
        assert not os.path.exists(_tmp_sibling(path)), \
            "atomic rename should consume the .tmp file on success"

    def test_repeated_saves_overwrite_cleanly(self, cfg):
        cfg.set("General", "theme", "first")
        assert cfg.save() is True
        cfg.set("General", "theme", "second")
        assert cfg.save() is True

        parsed = _read_parsed(_config_path(cfg))
        assert parsed.get("General", "theme") == "second"
        assert not os.path.exists(_tmp_sibling(_config_path(cfg)))


class TestInterruptedSavePreservesDestination:
    """The core B4 guarantee: an interrupted save never corrupts the
    destination. Each test first writes a known-good file, then injects
    a failure and asserts the old complete content survives."""

    def _seed_good_file(self, cfg: ConfigManager) -> str:
        cfg.set("General", "theme", "known-good")
        assert cfg.save() is True
        path = _config_path(cfg)
        # Sanity: the seed really is on disk and complete.
        assert _read_parsed(path).get("General", "theme") == "known-good"
        return path

    def test_failure_at_replace_keeps_old_file(self, cfg, monkeypatch):
        path = self._seed_good_file(cfg)

        def boom(*_args, **_kwargs):
            raise OSError("simulated crash at rename")

        monkeypatch.setattr(atomic.os, "replace", boom)

        cfg.set("General", "theme", "new-value-that-must-not-win")
        # save() swallows OSError and reports failure.
        assert cfg.save() is False

        # Destination is untouched: still the complete old file.
        parsed = _read_parsed(path)
        assert parsed.get("General", "theme") == "known-good"
        assert os.path.getsize(path) > 0

    def test_failure_during_write_keeps_old_file(self, cfg, monkeypatch):
        path = self._seed_good_file(cfg)

        def boom(*_args, **_kwargs):
            raise OSError("simulated disk-full during fsync")

        monkeypatch.setattr(atomic.os, "fsync", boom)

        cfg.set("General", "theme", "new-value-that-must-not-win")
        assert cfg.save() is False

        parsed = _read_parsed(path)
        assert parsed.get("General", "theme") == "known-good"

    def test_failed_save_cleans_up_tmp_sibling(self, cfg, monkeypatch):
        path = self._seed_good_file(cfg)

        def boom(*_args, **_kwargs):
            raise OSError("simulated crash at rename")

        monkeypatch.setattr(atomic.os, "replace", boom)

        cfg.set("General", "theme", "doomed")
        assert cfg.save() is False

        # Best-effort cleanup removed the partial temp file.
        assert not os.path.exists(_tmp_sibling(path))


class TestAtomicWriteHelper:
    """Direct unit tests for the helper, independent of ConfigManager."""

    def test_writes_exact_content(self, tmp_path):
        target = str(tmp_path / "out.txt")
        atomic.atomic_write_text(target, "hello\nworld\n")
        with open(target, encoding="utf-8") as f:
            assert f.read() == "hello\nworld\n"
        assert not os.path.exists(target + ".tmp")

    def test_overwrites_existing_atomically(self, tmp_path):
        target = str(tmp_path / "out.txt")
        atomic.atomic_write_text(target, "old")
        atomic.atomic_write_text(target, "new")
        with open(target, encoding="utf-8") as f:
            assert f.read() == "new"

    def test_replace_failure_propagates_and_preserves(self, tmp_path, monkeypatch):
        target = str(tmp_path / "out.txt")
        atomic.atomic_write_text(target, "original")

        monkeypatch.setattr(
            atomic.os, "replace",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError("boom")),
        )
        with pytest.raises(OSError):
            atomic.atomic_write_text(target, "should-not-land")

        with open(target, encoding="utf-8") as f:
            assert f.read() == "original"
        assert not os.path.exists(target + ".tmp")


class TestPlotProfilePersistenceIsAtomic:
    """Plot profiles persist through ConfigManager.save, so the B4 fix
    must protect them too (VERIFY S-4 additional verification)."""

    def test_profile_save_produces_complete_file(self, cfg):
        store = PlotProfileStore(cfg)
        store.upsert(PlotConfig.manual("Motor v3", r"RPM:\s*(?P<r>\d+)"))

        path = _config_path(cfg)
        assert os.path.isfile(path)
        assert not os.path.exists(_tmp_sibling(path))

        # A fresh manager resolves the same temp dir (the cfg fixture's
        # get_config_dir patch is still active) and loads from disk.
        reloaded = ConfigManager()
        store2 = PlotProfileStore(reloaded)
        assert store2.get("Motor v3") is not None

    def test_profile_save_interrupted_keeps_old_profiles(self, cfg, monkeypatch):
        store = PlotProfileStore(cfg)
        store.upsert(PlotConfig.manual("Motor v3", r"RPM:\s*(?P<r>\d+)"))
        path = _config_path(cfg)

        def boom(*_args, **_kwargs):
            raise OSError("simulated crash at rename")

        monkeypatch.setattr(atomic.os, "replace", boom)

        # This upsert's persistence will fail; the on-disk file must
        # still hold the previously-saved Motor v3 profile, never a
        # truncated blob.
        store.upsert(PlotConfig.manual("Motor v4", r"V:\s*(?P<v>\d+)"))

        parsed = _read_parsed(path)
        profiles_json = parsed.get("Plot", "profiles_json")
        assert "Motor v3" in profiles_json
        assert os.path.getsize(path) > 0
