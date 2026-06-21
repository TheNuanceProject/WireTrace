# SPDX-License-Identifier: MIT
"""Tests for the Linux update apply step (AppImage self-update).

Before this, ``launch_installer`` on Linux only ``xdg-open``-ed the
downloaded file — it never updated anything, so Linux had no real
auto-update. The fix: when the app runs as an AppImage, atomically
replace the running image with the downloaded one and relaunch; for any
other install type (a ``.deb`` or a source run) fall back to opening the
file for a manual install, since those cannot be replaced from user space.

These tests drive the real ``_replace_appimage`` / ``_apply_linux_update``
against the filesystem, with ``subprocess`` and ``QApplication`` patched
so nothing is actually spawned or quit.
"""

from __future__ import annotations

import os
import stat

import pytest

import updater.update_manager as um
from updater.update_manager import _apply_linux_update, _replace_appimage


class TestReplaceAppImage:
    @pytest.mark.skipif(os.name != "posix", reason="POSIX file-permission semantics")
    def test_replaces_content_and_sets_executable(self, tmp_path):
        target = tmp_path / "WireTrace.AppImage"
        target.write_bytes(b"OLD VERSION")
        src = tmp_path / "downloaded.AppImage"
        src.write_bytes(b"NEW VERSION")

        _replace_appimage(str(src), str(target))

        assert target.read_bytes() == b"NEW VERSION"
        mode = os.stat(target).st_mode
        assert mode & stat.S_IXUSR  # executable bit set

    def test_no_temp_file_left_behind(self, tmp_path):
        target = tmp_path / "WireTrace.AppImage"
        target.write_bytes(b"OLD")
        src = tmp_path / "new.AppImage"
        src.write_bytes(b"NEW")

        _replace_appimage(str(src), str(target))

        leftovers = [p for p in tmp_path.iterdir()
                     if p.name.startswith(".wiretrace-update-")]
        assert leftovers == []

    @pytest.mark.skipif(os.name != "posix", reason="POSIX file-permission semantics")
    def test_unwritable_target_dir_raises(self, tmp_path):
        # A root-owned/read-only target dir can't be written — must raise
        # so the caller can fall back. (Skip if running as root, which
        # bypasses permission bits.)
        if os.geteuid() == 0:
            pytest.skip("running as root bypasses directory permissions")
        ro_dir = tmp_path / "ro"
        ro_dir.mkdir()
        target = ro_dir / "WireTrace.AppImage"
        target.write_bytes(b"OLD")
        src = tmp_path / "new.AppImage"
        src.write_bytes(b"NEW")
        os.chmod(ro_dir, 0o500)  # read+execute, no write
        try:
            with pytest.raises(OSError):
                _replace_appimage(str(src), str(target))
        finally:
            os.chmod(ro_dir, 0o700)  # restore so tmp cleanup works


class TestApplyLinuxUpdate:
    @pytest.fixture
    def patched(self, monkeypatch):
        calls = {"popen": [], "quit": 0}
        monkeypatch.setattr(um.subprocess, "Popen",
                            lambda args, *a, **k: calls["popen"].append(args))
        monkeypatch.setattr(um, "QApplication",
                            type("Q", (), {"quit": staticmethod(
                                lambda: calls.__setitem__("quit", calls["quit"] + 1))}))
        return calls

    def test_appimage_env_triggers_self_replace_and_relaunch(
        self, tmp_path, monkeypatch, patched,
    ):
        running = tmp_path / "WireTrace-running.AppImage"
        running.write_bytes(b"OLD")
        downloaded = tmp_path / "WireTrace-v9.9.9-x86_64.AppImage"
        downloaded.write_bytes(b"NEW")
        monkeypatch.setenv("APPIMAGE", str(running))

        _apply_linux_update(str(downloaded))

        # Running image replaced with the new content...
        assert running.read_bytes() == b"NEW"
        # ...relaunched via the SAME path (not the temp download)...
        assert patched["popen"] == [[str(running)]]
        # ...and the app quit so the new process takes over.
        assert patched["quit"] == 1

    def test_not_appimage_falls_back_to_manual_open(
        self, tmp_path, monkeypatch, patched,
    ):
        downloaded = tmp_path / "WireTrace-v9.9.9-x86_64.AppImage"
        downloaded.write_bytes(b"NEW")
        monkeypatch.delenv("APPIMAGE", raising=False)

        _apply_linux_update(str(downloaded))

        # No self-replace; the download is opened for manual install.
        assert patched["popen"] == [["xdg-open", str(downloaded)]]
        assert patched["quit"] == 1

    def test_replace_failure_falls_back(self, tmp_path, monkeypatch, patched):
        # APPIMAGE points at a real file, but the replace raises — must not
        # crash; must fall back to opening the download.
        running = tmp_path / "WireTrace-running.AppImage"
        running.write_bytes(b"OLD")
        downloaded = tmp_path / "new.AppImage"
        downloaded.write_bytes(b"NEW")
        monkeypatch.setenv("APPIMAGE", str(running))
        monkeypatch.setattr(um, "_replace_appimage",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))

        _apply_linux_update(str(downloaded))

        assert patched["popen"] == [["xdg-open", str(downloaded)]]
        assert patched["quit"] == 1


class TestLaunchInstallerDispatch:
    def test_linux_routes_to_apply(self, tmp_path, monkeypatch):
        f = tmp_path / "WireTrace-v9.9.9-x86_64.AppImage"
        f.write_bytes(b"x")
        monkeypatch.setattr(um.platform, "system", lambda: "Linux")
        seen = []
        monkeypatch.setattr(um, "_apply_linux_update", lambda p: seen.append(p))

        um.UpdateManager.launch_installer(str(f))

        assert seen == [str(f)]
