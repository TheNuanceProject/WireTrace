# SPDX-License-Identifier: MIT
"""Regression tests for B6 — AppImage missing shiboken6 (and everything).

The bug: the Linux packaging step copied only the launcher binary out of
the Nuitka ``--standalone`` directory and renamed it ``.AppImage``. A
standalone build needs all its sibling files — Qt libraries, shiboken6,
the bundled resources/themes, and the numpy/pyqtgraph/regex extension
modules — so the lone binary could not launch.

The fix (``build/build.py``) assembles a complete AppDir from the ENTIRE
standalone directory and packs it with ``appimagetool``, falling back to a
gzipped tarball of the whole directory when the tool is unavailable.

``appimagetool`` itself needs FUSE/root and isn't available in CI, so the
external pack step is verified by the maintainer on Ubuntu. What is fully
verifiable here is the part that actually caused B6: that the packaging
includes the complete dependency tree rather than the bare binary. These
tests assemble an AppDir from a synthetic standalone dir (binary + a stand
-in ``libshiboken6`` + bundled resources) and assert every piece is
carried through both the appimagetool path and the tarball fallback.
"""

from __future__ import annotations

import importlib.util
import os
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

# Load build/build.py by path — it is a script, not an importable package.
_BUILD_PY = Path(__file__).resolve().parent.parent / "build" / "build.py"
_spec = importlib.util.spec_from_file_location("wiretrace_build", _BUILD_PY)
buildmod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(buildmod)


def _make_fake_standalone(tmp_path: Path) -> Path:
    """A fake Nuitka --standalone output: the binary plus the siblings
    that B6 used to leave behind."""
    final_dir = tmp_path / "WireTrace"
    final_dir.mkdir()
    (final_dir / "wiretrace").write_bytes(b"\x7fELF fake binary")
    # The sibling that gave B6 its name, plus other dependency artefacts.
    (final_dir / "libshiboken6.abi3.so").write_bytes(b"fake shiboken")
    (final_dir / "libpyside6.abi3.so").write_bytes(b"fake pyside")
    (final_dir / "regex").mkdir()
    (final_dir / "regex" / "_regex.so").write_bytes(b"fake regex ext")
    res = final_dir / "resources"
    res.mkdir()
    (res / "app_icon.svg").write_text("<svg/>")
    return final_dir


class TestAssembleAppDir:
    def test_full_tree_copied_into_usr_bin(self, tmp_path):
        final_dir = _make_fake_standalone(tmp_path)
        appdir = buildmod._assemble_appdir(final_dir, tmp_path / "AppDir")

        bin_dir = appdir / "usr" / "bin"
        # The binary AND its siblings — the B6 fix.
        assert (bin_dir / "wiretrace").exists()
        assert (bin_dir / "libshiboken6.abi3.so").exists()
        assert (bin_dir / "libpyside6.abi3.so").exists()
        assert (bin_dir / "regex" / "_regex.so").exists()
        assert (bin_dir / "resources" / "app_icon.svg").exists()

    def test_apprun_is_executable_and_forwards_args(self, tmp_path):
        final_dir = _make_fake_standalone(tmp_path)
        appdir = buildmod._assemble_appdir(final_dir, tmp_path / "AppDir")

        apprun = appdir / "AppRun"
        assert apprun.exists()
        # The Unix execute bit only exists (and only matters) on POSIX —
        # the AppImage is built on Linux. os.chmod can't set it on Windows.
        if os.name == "posix":
            assert apprun.stat().st_mode & 0o111, "AppRun must be executable"
        text = apprun.read_text()
        assert "usr/bin/wiretrace" in text
        assert '"$@"' in text, "AppRun must forward args (e.g. --smoke-test)"

    def test_desktop_entry_present_and_valid(self, tmp_path):
        final_dir = _make_fake_standalone(tmp_path)
        appdir = buildmod._assemble_appdir(final_dir, tmp_path / "AppDir")

        root_desktop = appdir / "wiretrace.desktop"
        assert root_desktop.exists()
        installed = appdir / "usr" / "share" / "applications" / "wiretrace.desktop"
        assert installed.exists()
        text = root_desktop.read_text()
        assert "Exec=wiretrace" in text
        assert "Icon=wiretrace" in text
        assert "Categories=Development;Electronics;" in text
        assert "Type=Application" in text

    def test_icon_at_root_and_in_hicolor(self, tmp_path):
        final_dir = _make_fake_standalone(tmp_path)
        appdir = buildmod._assemble_appdir(final_dir, tmp_path / "AppDir")
        # Uses the real resources/app_icon.png from the repo.
        assert (appdir / "wiretrace.png").exists()
        assert (appdir / "usr" / "share" / "icons" / "hicolor"
                / "256x256" / "apps" / "wiretrace.png").exists()

    def test_rebuild_replaces_stale_appdir(self, tmp_path):
        final_dir = _make_fake_standalone(tmp_path)
        appdir_path = tmp_path / "AppDir"
        # Pre-existing stale content must not survive a rebuild.
        appdir_path.mkdir()
        (appdir_path / "stale.txt").write_text("old")
        buildmod._assemble_appdir(final_dir, appdir_path)
        assert not (appdir_path / "stale.txt").exists()


class TestBuildLinuxAppImage:
    def test_appimagetool_invoked_with_appdir(self, tmp_path, monkeypatch):
        final_dir = _make_fake_standalone(tmp_path)
        deploy = tmp_path / "deploy"
        deploy.mkdir()

        calls: list[tuple[list[str], dict | None]] = []

        def fake_which(name: str):
            return "/usr/bin/appimagetool" if name == "appimagetool" else None

        def fake_run_cmd(cmd, cwd=None, check=True, env=None):
            # Simulate appimagetool producing the output file (cmd[2]).
            Path(cmd[2]).write_bytes(b"AI\x02fake-appimage")
            calls.append((cmd, env))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(buildmod.shutil, "which", fake_which)
        monkeypatch.setattr(buildmod, "run_cmd", fake_run_cmd)

        out = buildmod._build_linux_appimage(final_dir, deploy, "1.2.0")

        assert out.name == "WireTrace-v1.2.0-x86_64.AppImage"
        assert out.exists()
        assert len(calls) == 1
        cmd, env = calls[0]
        assert cmd[0] == "/usr/bin/appimagetool"
        assert cmd[1] == str(deploy / "AppDir")
        assert cmd[2] == str(out)
        assert env is not None and env.get("ARCH") == "x86_64"

    def test_fallback_tarball_contains_full_tree(self, tmp_path, monkeypatch):
        final_dir = _make_fake_standalone(tmp_path)
        deploy = tmp_path / "deploy"
        deploy.mkdir()

        # No appimagetool → fallback to a tarball of the WHOLE directory.
        monkeypatch.setattr(buildmod.shutil, "which", lambda _name: None)

        out = buildmod._build_linux_appimage(final_dir, deploy, "1.2.0")

        assert out.suffix == ".gz"
        with tarfile.open(out, "r:gz") as tf:
            names = tf.getnames()
        # The fallback must carry the siblings too — not the bare binary.
        assert any(n.endswith("WireTrace/wiretrace") for n in names)
        assert any(n.endswith("libshiboken6.abi3.so") for n in names)
        assert any(n.endswith("regex/_regex.so") for n in names)

    def test_appimagetool_no_output_falls_back(self, tmp_path, monkeypatch):
        final_dir = _make_fake_standalone(tmp_path)
        deploy = tmp_path / "deploy"
        deploy.mkdir()

        def fake_which(name: str):
            return "/usr/bin/appimagetool" if name == "appimagetool" else None

        # appimagetool "runs" but produces nothing → must fall back.
        def fake_run_cmd(cmd, cwd=None, check=True, env=None):
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")

        monkeypatch.setattr(buildmod.shutil, "which", fake_which)
        monkeypatch.setattr(buildmod, "run_cmd", fake_run_cmd)

        out = buildmod._build_linux_appimage(final_dir, deploy, "1.2.0")
        assert out.suffix == ".gz", "must fall back to tarball when no AppImage produced"


@pytest.mark.parametrize("missing", [False])
def test_assemble_warns_when_icon_missing(tmp_path, monkeypatch, missing):
    # If the source icon is absent, assembly proceeds with a warning
    # rather than crashing the build.
    final_dir = _make_fake_standalone(tmp_path)
    monkeypatch.setattr(buildmod, "PROJECT_ROOT", tmp_path / "no_such_root")
    appdir = buildmod._assemble_appdir(final_dir, tmp_path / "AppDir")
    # Icon copy skipped, but the AppDir is otherwise complete.
    assert not (appdir / "wiretrace.png").exists()
    assert (appdir / "AppRun").exists()
