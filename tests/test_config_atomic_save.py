# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests that saving the preferences is atomic.

``ConfigManager.save`` writes through ``app._atomic.atomic_write_text``:
a temporary file, flushed and synced, then renamed over the destination.
Two things are held to:

  1. A successful save produces a complete, parseable file and leaves no
     temporary file behind.
  2. A save interrupted at any point leaves the destination as the
     previous complete file.

Plot profiles are saved through the same call, so they are covered too.
"""

from __future__ import annotations

import configparser
import os

import pytest

import app._atomic as atomic
from app import config as config_module
from app.config import ConfigManager
from app.constants import SHARED_FILE_LOCK_SUFFIX
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


def _left_over(path: str) -> list[str]:
    """Files beside ``path`` other than ``path`` itself and its lock,
    which stays for the next save (see app/file_lock.py)."""
    folder = os.path.dirname(path)
    name = os.path.basename(path)
    return sorted(set(os.listdir(folder)) - {name, name + SHARED_FILE_LOCK_SUFFIX})


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

    def test_save_leaves_no_temporary_file(self, cfg):
        cfg.set("Display", "font_size", "18")
        assert cfg.save() is True

        path = _config_path(cfg)
        assert _left_over(path) == [], \
            "the rename should consume the temporary file on success"

    def test_repeated_saves_overwrite_cleanly(self, cfg):
        cfg.set("General", "theme", "first")
        assert cfg.save() is True
        cfg.set("General", "theme", "second")
        assert cfg.save() is True

        parsed = _read_parsed(_config_path(cfg))
        assert parsed.get("General", "theme") == "second"
        assert _left_over(_config_path(cfg)) == []


class TestInterruptedSavePreservesDestination:
    """The core guarantee: an interrupted save never corrupts the
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

    def test_failed_save_removes_its_temporary_file(self, cfg, monkeypatch):
        path = self._seed_good_file(cfg)

        def boom(*_args, **_kwargs):
            raise OSError("simulated crash at rename")

        monkeypatch.setattr(atomic.os, "replace", boom)

        cfg.set("General", "theme", "doomed")
        assert cfg.save() is False

        # Best-effort cleanup removed the partial temp file.
        assert _left_over(path) == []


class TestAtomicWriteHelper:
    """Direct unit tests for the helper, independent of ConfigManager."""

    def test_writes_exact_content(self, tmp_path):
        target = str(tmp_path / "out.txt")
        atomic.atomic_write_text(target, "hello\nworld\n")
        with open(target, encoding="utf-8") as f:
            assert f.read() == "hello\nworld\n"
        assert _left_over(target) == []

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
        assert _left_over(target) == []


class TestPlotProfilePersistenceIsAtomic:
    """Plot profiles are saved through ConfigManager.save, so atomic
    saving protects them too."""

    def test_profile_save_produces_complete_file(self, cfg):
        store = PlotProfileStore(cfg)
        store.upsert(PlotConfig.manual("Motor v3", r"RPM:\s*(?P<r>\d+)"))

        path = _config_path(cfg)
        assert os.path.isfile(path)
        assert _left_over(path) == []

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

    def test_a_file_already_beside_the_destination_is_never_written_over(self, tmp_path):
        target = str(tmp_path / "out.txt")
        bystander = tmp_path / "out.txt.tmp"
        bystander.write_text("not ours", encoding="utf-8")

        atomic.atomic_write_text(target, "new")

        assert bystander.read_text(encoding="utf-8") == "not ours"
        assert _left_over(target) == ["out.txt.tmp"]

    def test_the_temporary_file_has_a_name_of_its_own_each_time(self, tmp_path, monkeypatch):
        target = str(tmp_path / "out.txt")
        used: list[str] = []
        replace = os.replace

        def recording_replace(source, destination):
            used.append(os.path.basename(source))
            replace(source, destination)

        monkeypatch.setattr(atomic.os, "replace", recording_replace)

        atomic.atomic_write_text(target, "one")
        atomic.atomic_write_text(target, "two")

        assert len(set(used)) == 2
        assert all(name.startswith(".wiretrace-") and name.endswith(".tmp") for name in used)

    @pytest.mark.skipif(os.name == "nt", reason="Windows has no owner-only file mode")
    def test_the_file_is_for_its_owner_only(self, tmp_path):
        target = str(tmp_path / "out.txt")

        atomic.atomic_write_text(target, "private")

        assert os.stat(target).st_mode & 0o777 == 0o600

    def test_an_interrupted_write_leaves_no_temporary_file(self, tmp_path, monkeypatch):
        target = str(tmp_path / "out.txt")
        atomic.atomic_write_text(target, "original")

        def interrupted(*_arguments, **_named):
            raise KeyboardInterrupt

        monkeypatch.setattr(atomic.os, "fsync", interrupted)
        with pytest.raises(KeyboardInterrupt):
            atomic.atomic_write_text(target, "should-not-land")

        with open(target, encoding="utf-8") as f:
            assert f.read() == "original"
        assert _left_over(target) == []
