# SPDX-License-Identifier: GPL-3.0-or-later
"""Files shared by several open WireTrace windows.

The lock on a shared file is held by one process at a time, and is let
go by the system when that process ends however it ends. Preferences
saved by two windows keep what each changed.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from app import config as config_module
from app import file_lock as file_lock_module
from app.config import ConfigManager
from app.constants import DEFAULT_FONT_SIZE
from app.file_lock import FileLock, LockTimeout, held_lock

_HOLDER = """
import sys, time
sys.path.insert(0, {root!r})
from app.file_lock import FileLock
lock = FileLock({path!r})
assert lock.try_acquire()
print("held", flush=True)
time.sleep(60)
"""


#: How long a test waits for the system to let go of a killed process's lock.
_RELEASE_AFTER_EXIT_S = 5.0


def _hold_in_another_process(path: Path) -> subprocess.Popen[str]:
    root = str(Path(__file__).resolve().parent.parent)
    process = subprocess.Popen(
        [sys.executable, "-c", _HOLDER.format(root=root, path=str(path))],
        stdout=subprocess.PIPE, text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "held"
    return process


def _end(process: subprocess.Popen[str]) -> None:
    """End the holding process as a crash would, and let go of its pipe."""
    process.kill()
    process.wait()
    if process.stdout is not None:
        process.stdout.close()


class TestFileLock:
    def test_is_held_by_one_at_a_time(self, tmp_path):
        first, second = FileLock(tmp_path / "f.lock"), FileLock(tmp_path / "f.lock")

        assert first.try_acquire()
        assert not second.try_acquire()
        first.release()
        assert second.try_acquire()
        second.release()

    def test_waits_for_another_process_and_gives_up(self, tmp_path):
        process = _hold_in_another_process(tmp_path / "f.lock")
        try:
            started = time.monotonic()
            with pytest.raises(LockTimeout):
                FileLock(tmp_path / "f.lock").acquire(0.2)
            assert time.monotonic() - started >= 0.2
        finally:
            _end(process)

    def test_is_let_go_when_its_process_is_killed(self, tmp_path):
        process = _hold_in_another_process(tmp_path / "f.lock")
        _end(process)

        # Windows lets go of a dead process's locks shortly after it ends,
        # not at once, so the lock is waited for, as WireTrace waits for it.
        lock = FileLock(tmp_path / "f.lock")
        lock.acquire(_RELEASE_AFTER_EXIT_S)
        assert lock.held
        lock.release()

    def test_held_for_a_block_and_then_let_go(self, tmp_path):
        with held_lock(tmp_path / "f.lock", 1) as lock:
            assert lock.held
            assert not FileLock(tmp_path / "f.lock").try_acquire()
        assert FileLock(tmp_path / "f.lock").try_acquire()

    def test_release_without_holding_does_nothing(self, tmp_path):
        FileLock(tmp_path / "f.lock").release()

    def test_remove_deletes_the_file_and_lets_the_lock_go(self, tmp_path):
        lock = FileLock(tmp_path / "f.lock")
        assert lock.try_acquire()

        lock.remove()

        assert not lock.held
        assert not (tmp_path / "f.lock").exists()
        FileLock(tmp_path / "f.lock").remove()

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows refuses to remove a file another process has open, so this cannot happen",
    )
    def test_a_file_removed_while_it_was_being_locked_is_locked_again_at_its_path(
        self, tmp_path, monkeypatch,
    ):
        """Another process that held the lock removes the file between this
        one opening and locking it: the lock must be on the file now at the
        path, or two processes would each believe they hold it."""
        path = tmp_path / "f.lock"
        real_try_lock = file_lock_module._try_lock
        removed: list[bool] = []

        def removed_in_between(descriptor: int) -> bool:
            if not removed:
                removed.append(True)
                path.unlink()
            return real_try_lock(descriptor)

        monkeypatch.setattr(file_lock_module, "_try_lock", removed_in_between)
        lock = FileLock(path)

        assert lock.try_acquire()
        assert path.exists()
        monkeypatch.setattr(file_lock_module, "_try_lock", real_try_lock)
        assert not FileLock(path).try_acquire()
        lock.release()


@pytest.fixture()
def config_dir(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(config_module, "get_config_dir", lambda: str(tmp_path))
    return tmp_path


class TestTwoWindows:
    def test_each_keeps_what_the_other_changed(self, config_dir):
        first, second = ConfigManager(), ConfigManager()

        first.theme = "dark"
        assert first.save()
        second.font_size = 16
        assert second.save()

        reread = ConfigManager()
        assert reread.theme == "dark"
        assert reread.font_size == 16
        assert second.theme == "dark"

    def test_a_setting_changed_in_both_keeps_the_last_saved(self, config_dir):
        first, second = ConfigManager(), ConfigManager()
        first.theme = "dark"
        second.theme = "light"

        assert first.save()
        assert second.save()

        assert ConfigManager().theme == "light"

    def test_restoring_the_defaults_writes_every_setting(self, config_dir):
        first, second = ConfigManager(), ConfigManager()
        first.font_size = 16
        assert first.save()

        second.reset_to_defaults()
        assert second.save()

        assert ConfigManager().font_size == DEFAULT_FONT_SIZE

    def test_restoring_the_defaults_keeps_what_another_window_chose(self, config_dir):
        first = ConfigManager()
        first.usage_statistics = True
        assert first.save()
        second, third = ConfigManager(), ConfigManager()
        third.usage_statistics = False
        assert third.save()

        second.reset_to_defaults()
        assert second.save()

        assert ConfigManager().usage_statistics is False

    def test_restoring_the_defaults_keeps_a_choice_made_in_this_window(self, config_dir):
        config = ConfigManager()
        config.usage_statistics = True
        config.reset_to_defaults()
        assert config.save()

        assert ConfigManager().usage_statistics is True

    def test_does_not_save_while_another_window_holds_the_file(self, config_dir, monkeypatch):
        monkeypatch.setattr(config_module, "SHARED_FILE_LOCK_TIMEOUT_S", 0.1)
        config = ConfigManager()
        config.theme = "dark"
        lock = FileLock(config.config_path + ".lock")
        assert lock.try_acquire()
        try:
            assert config.save() is False
        finally:
            lock.release()
        assert config.save() is True
        assert ConfigManager().theme == "dark"
