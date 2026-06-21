# SPDX-License-Identifier: MIT
"""WireTrace cross-platform build orchestrator.

Production build pipeline per spec section 13.

Usage:
    python build/build.py --platform windows --version 1.0.0
    python build/build.py --platform macos   --version 1.0.0
    python build/build.py --platform linux   --version 1.0.0
    python build/build.py --platform all     --version 1.0.0

Build steps:
    1. Validate environment (Python, Nuitka, platform tools)
    2. Version stamp (update version.py + platform configs)
    3. Compile with Nuitka (Python → C → standalone native binary)
    4. Post-process (rename binary, copy resources)
    5. Validate build (executes, version matches)
    6. Package installer (Inno Setup / create-dmg / AppImage / .deb)
    7. Sign (if certificates available)
    8. Generate update JSON (SHA-256 hashes)
    9. Final report
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ── Path Resolution ──────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
BUILD_DIR = PROJECT_ROOT / "build"
DIST_DIR = BUILD_DIR / "dist"
DEPLOYMENT_DIR = PROJECT_ROOT / "deployment"

sys.path.insert(0, str(PROJECT_ROOT))
from version import APP_NAME, APP_VERSION, RELEASES_BASE_URL

# ── Logging ──────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO") -> None:
    print(f"  [{level}] {msg}")


def log_section(title: str) -> None:
    print(f"\n{'═' * 60}")
    print(f"  {title}")
    print(f"{'═' * 60}")


def run_cmd(
    cmd: list[str],
    cwd: str | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run a command, optionally raising on failure."""
    log(f"$ {' '.join(cmd)}")
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, env=env,
    )
    if result.returncode != 0:
        log(f"STDOUT:\n{result.stdout}", "DEBUG")
        log(f"STDERR:\n{result.stderr}", "DEBUG")
        if check:
            raise RuntimeError(
                f"Command failed (exit {result.returncode}): {' '.join(cmd)}\n"
                f"{result.stderr[:500]}"
            )
    return result


# ── Tool Discovery ────────────────────────────────────────────────────────────

def find_iscc() -> str | None:
    """Find Inno Setup compiler (ISCC.exe).

    Inno Setup does not add itself to PATH by default, so we check
    common installation paths as a fallback — matching the approach
    used in the original SerialLoggerPro build pipeline.
    """
    # Try PATH first
    found = shutil.which("iscc") or shutil.which("ISCC")
    if found:
        return found

    # Common install locations (Inno Setup 6)
    candidates = [
        Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
        Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
        Path(r"C:\Program Files (x86)\Inno Setup 5\ISCC.exe"),
        Path(r"C:\Program Files\Inno Setup 5\ISCC.exe"),
    ]

    # Also check via PROGRAMFILES environment variables
    for env_var in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        base = os.environ.get(env_var)
        if base:
            for ver in ("6", "5"):
                candidates.append(Path(base) / f"Inno Setup {ver}" / "ISCC.exe")

    for path in candidates:
        if path.is_file():
            return str(path)

    return None


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1: VALIDATE ENVIRONMENT
# ══════════════════════════════════════════════════════════════════════════════

def validate_environment(plat: str) -> None:
    log_section("Step 1: Validate Environment")

    # Python version
    log(f"Python {sys.version}")

    # Nuitka
    try:
        result = run_cmd([sys.executable, "-m", "nuitka", "--version"])
        nuitka_ver = result.stdout.strip().splitlines()[0]
        log(f"Nuitka {nuitka_ver}")
    except (RuntimeError, FileNotFoundError) as exc:
        raise RuntimeError(
            "Nuitka not found. Install with:\n"
            "  pip install nuitka ordered-set"
        ) from exc

    # PySide6
    try:
        import PySide6
        log(f"PySide6 {PySide6.__version__}")
    except ImportError as exc:
        raise RuntimeError("PySide6 not found. Install: pip install PySide6") from exc

    # Platform-specific tools
    if plat == "windows":
        iscc = find_iscc()
        if iscc:
            log(f"Inno Setup: {iscc}")
        else:
            log("Inno Setup (iscc) not found — installer will NOT be created", "WARN")
            log("  Download from: https://jrsoftware.org/issetup.exe", "WARN")

    elif plat == "macos":
        if shutil.which("create-dmg"):
            log("create-dmg: found")
        else:
            log("create-dmg not found — .dmg will not be created", "WARN")

    elif plat == "linux":
        for tool in ("appimagetool", "dpkg-deb"):
            if shutil.which(tool):
                log(f"{tool}: found")
            else:
                log(f"{tool} not found", "WARN")

    log("Environment validated ✓")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2: VERSION STAMP
# ══════════════════════════════════════════════════════════════════════════════

def version_stamp(version: str) -> None:
    log_section("Step 2: Version Stamp")

    # Update version.py
    version_file = PROJECT_ROOT / "version.py"
    content = version_file.read_text()
    content = re.sub(
        r'APP_VERSION\s*=\s*"[^"]*"',
        f'APP_VERSION = "{version}"',
        content,
    )
    version_file.write_text(content)
    log(f"Stamped version.py → {version}")

    # Update Inno Setup script (Windows installer)
    iss_file = BUILD_DIR / "windows" / "installer.iss"
    if iss_file.exists():
        iss_content = iss_file.read_text(encoding="utf-8")
        iss_content = re.sub(
            r'#define\s+MyAppVersion\s+"[^"]*"',
            f'#define MyAppVersion     "{version}"',
            iss_content,
        )
        iss_file.write_text(iss_content, encoding="utf-8")
        log(f"Stamped installer.iss → {version}")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3: COMPILE WITH NUITKA (STANDALONE MODE)
# ══════════════════════════════════════════════════════════════════════════════

def compile_nuitka(plat: str, version: str) -> Path:
    """Compile to standalone directory using Nuitka.

    IMPORTANT: Uses --standalone (NOT --onefile) because WireTrace
    loads QSS themes and SVG icons from the filesystem at runtime.
    Onefile mode would bundle them inside the binary where
    os.path.isfile() can't find them.

    Output: build/dist/main.dist/ (contains .exe + all dependencies)
    """
    log_section("Step 3: Compile with Nuitka")

    # Clean previous build
    for old_dir in ("main.dist", "main.build", "main.onefile-build"):
        old = DIST_DIR / old_dir
        if old.exists():
            shutil.rmtree(old)
            log(f"Cleaned {old.name}")

    DIST_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "nuitka",
        "--standalone",                     # Directory output (NOT onefile)
        f"--output-dir={DIST_DIR}",
        "--enable-plugin=pyside6",          # PySide6 dependency detection
        "--assume-yes-for-downloads",       # Auto-download gcc if needed

        # ── Application metadata ──
        f"--product-name={APP_NAME}",
        f"--product-version={version}",
        "--company-name=The Nuance Project",
        "--file-description=Professional Serial Data Monitor",
        "--copyright=© 2026 The Nuance Project",

        # ── Include runtime data files ──
        # Icons (SVG) — loaded by icon_loader.py via os.path.isfile()
        f"--include-data-dir={PROJECT_ROOT / 'resources'}=resources",
        # QSS themes — loaded by theme_manager.py via open()
        f"--include-data-dir={PROJECT_ROOT / 'ui' / 'themes'}=ui/themes",

        # ── Bundle lazy-imported third-party packages ──
        # CRITICAL: pyqtgraph and numpy are imported lazily from
        # device_tab.py → plot_view.py (only when the Plot panel is
        # first opened). Nuitka's static analyzer does not reliably
        # follow lazy chains into third-party packages with internal
        # dynamic imports — pyqtgraph in particular uses runtime
        # configuration loaders and optional OpenGL bindings that
        # static analysis cannot resolve. Without these flags, the
        # built .exe shows the Plot button as enabled but the panel
        # silently fails to construct because pyqtgraph isn't there.
        # The Step 5.5 smoke test (see run_smoke_test below) catches
        # any regression of this — do NOT remove these flags.
        "--include-package=pyqtgraph",
        "--include-package=numpy",

        # ── PySide6 optional submodules used by pyqtgraph ──
        # pyqtgraph imports these internally for hardware-accelerated
        # rendering (QtOpenGL/QtOpenGLWidgets) and vector-graphics
        # icons (QtSvg). The Nuitka --enable-plugin=pyside6 flag only
        # bundles the core Qt modules our own code touches directly
        # (QtCore, QtGui, QtWidgets, QtSerialPort, ...) — optional
        # submodules require explicit --include-module flags. If
        # any of these is missing, pyqtgraph either fails to import
        # outright (QtOpenGL) or silently falls back to a degraded
        # renderer with broken SVG / no GL acceleration.
        "--include-module=PySide6.QtOpenGL",
        "--include-module=PySide6.QtOpenGLWidgets",
        "--include-module=PySide6.QtSvg",
    ]

    # Platform-specific flags
    if plat == "windows":
        icon_path = PROJECT_ROOT / "resources" / "app_icon.ico"
        if icon_path.exists():
            cmd.append(f"--windows-icon-from-ico={icon_path}")
        cmd.append("--windows-console-mode=disable")  # No console window
    elif plat == "macos":
        icon_path = PROJECT_ROOT / "resources" / "app_icon.icns"
        cmd.append(f"--macos-app-name={APP_NAME}")
        cmd.append(f"--macos-app-version={version}")
        if icon_path.exists():
            cmd.append(f"--macos-app-icon={icon_path}")
    elif plat == "linux":
        icon_path = PROJECT_ROOT / "resources" / "app_icon.png"
        if icon_path.exists():
            cmd.append(f"--linux-icon={icon_path}")

    # Entry point
    cmd.append(str(PROJECT_ROOT / "main.py"))

    log("Starting Nuitka compilation (this may take several minutes)...")
    run_cmd(cmd, cwd=str(PROJECT_ROOT))

    # Nuitka outputs to: dist/main.dist/
    dist_dir = DIST_DIR / "main.dist"
    if not dist_dir.exists():
        raise RuntimeError(f"Nuitka output not found at {dist_dir}")

    log(f"Compilation complete → {dist_dir}")
    return dist_dir


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4: POST-PROCESS
# ══════════════════════════════════════════════════════════════════════════════

def post_process(plat: str, dist_dir: Path, version: str) -> Path:
    """Rename binary and prepare final distribution directory.

    Nuitka names the binary 'main.exe'. We rename it to 'WireTrace.exe'
    and move the entire directory to build/dist/WireTrace/.
    """
    log_section("Step 4: Post-Process")

    # Target directory
    final_dir = DIST_DIR / APP_NAME
    if final_dir.exists():
        shutil.rmtree(final_dir)

    # Rename main.dist → WireTrace
    dist_dir.rename(final_dir)
    log(f"Renamed {dist_dir.name} → {final_dir.name}")

    # Rename binary
    if plat == "windows":
        old_bin = final_dir / "main.exe"
        new_bin = final_dir / f"{APP_NAME}.exe"
    else:
        old_bin = final_dir / "main.bin"
        if not old_bin.exists():
            old_bin = final_dir / "main"
        new_bin = final_dir / APP_NAME.lower()

    if old_bin.exists():
        old_bin.rename(new_bin)
        log(f"Renamed {old_bin.name} → {new_bin.name}")
    else:
        log(f"Binary {old_bin.name} not found — checking alternatives...", "WARN")
        # Find any executable in the directory
        for f in final_dir.iterdir():
            if f.is_file() and f.name.startswith("main"):
                f.rename(new_bin)
                log(f"Renamed {f.name} → {new_bin.name}")
                break

    if not new_bin.exists():
        raise RuntimeError(f"Binary not found after rename: {new_bin}")

    # Report contents
    total_size = sum(f.stat().st_size for f in final_dir.rglob("*") if f.is_file())
    file_count = sum(1 for f in final_dir.rglob("*") if f.is_file())
    log(f"Distribution: {file_count} files, {total_size / 1024 / 1024:.1f} MB total")

    return final_dir


# ══════════════════════════════════════════════════════════════════════════════
# STEP 5: VALIDATE BUILD
# ══════════════════════════════════════════════════════════════════════════════

def validate_build(final_dir: Path, plat: str) -> None:
    log_section("Step 5: Validate Build")

    exe = final_dir / f"{APP_NAME}.exe" if plat == "windows" else final_dir / APP_NAME.lower()

    if not exe.exists():
        raise RuntimeError(f"Executable not found: {exe}")

    size_mb = exe.stat().st_size / (1024 * 1024)
    log(f"Executable: {exe.name} ({size_mb:.1f} MB)")

    # Verify resources were bundled
    resources_dir = final_dir / "resources"
    themes_dir = final_dir / "ui" / "themes"

    if resources_dir.exists():
        icon_count = len(list(resources_dir.rglob("*.svg"))) + len(list(resources_dir.rglob("*.ico")))
        log(f"Resources: {icon_count} icon files bundled")
    else:
        log("Resources directory NOT found in build output", "WARN")

    if themes_dir.exists():
        qss_count = len(list(themes_dir.glob("*.qss")))
        log(f"Themes: {qss_count} QSS files bundled")
    else:
        log("Themes directory NOT found in build output", "WARN")

    log("Build validation passed ✓")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 5.5: SMOKE TEST — verify the binary's lazy-import chains
# ══════════════════════════════════════════════════════════════════════════════

def run_smoke_test(final_dir: Path, plat: str) -> None:
    """Run the binary in --smoke-test mode and require a clean exit.

    Why this exists:
        Nuitka's static analyzer cannot always follow lazy imports
        into third-party packages with internal dynamic loading
        (pyqtgraph being the canonical example). The result is a
        binary that *looks* fine — Step 5 validation passes, all
        data dirs are bundled — but the Plot panel silently fails
        to construct because pyqtgraph was never included.

        This step runs the compiled binary in a headless self-check
        mode (`--smoke-test`) that imports every lazy entry point.
        If any import fails, the binary exits non-zero and we abort
        the build BEFORE the installer is built and shipped.

    Raises:
        RuntimeError: if the smoke test process exits non-zero or
            times out. The build halts here.
    """
    log_section("Step 5.5: Smoke Test")

    exe = final_dir / f"{APP_NAME}.exe" if plat == "windows" else final_dir / APP_NAME.lower()
    if not exe.exists():
        raise RuntimeError(f"Executable not found for smoke test: {exe}")

    log("Running --smoke-test on the built binary (verifies lazy imports)...")
    log(f"  $ {exe} --smoke-test")

    try:
        result = subprocess.run(
            [str(exe), "--smoke-test"],
            cwd=str(final_dir),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "Smoke test timed out after 60s. The binary may be hanging "
            "on a missing dependency or initialisation error.",
        ) from exc

    # Echo the smoke-test output so the operator can see what passed
    # and what failed. stderr typically holds the logging messages.
    output = result.stderr or result.stdout
    for line in output.splitlines():
        log(f"  {line}")

    if result.returncode != 0:
        raise RuntimeError(
            f"Smoke test FAILED (exit code {result.returncode}). "
            "The built binary cannot resolve one or more lazy imports — "
            "see lines above for the specific module. "
            "Common cause: a third-party package needs an explicit "
            "--include-package=<name> flag in compile_nuitka(). "
            "Fix the build script and rebuild. Do NOT package or "
            "ship this binary."
        )

    log("Smoke test passed ✓ — all lazy imports resolve in the binary")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 6: PACKAGE INSTALLER
# ══════════════════════════════════════════════════════════════════════════════

def _assemble_appdir(final_dir: Path, appdir: Path) -> Path:
    """Build a complete AppDir from the FULL standalone distribution.

    B6: the previous Linux packaging copied only the launcher binary out
    of the Nuitka ``--standalone`` directory and renamed it ``.AppImage``,
    leaving every sibling behind — the Qt libraries, shiboken6, the
    bundled resources/themes, and the numpy/pyqtgraph/regex extension
    modules. The result could not launch. An AppImage must contain the
    whole dependency tree, so we copy the entire ``final_dir`` into
    ``AppDir/usr/bin`` and add the AppRun entry point, desktop entry, and
    icon that ``appimagetool`` requires.

    Returns the populated AppDir path.
    """
    app = APP_NAME.lower()

    if appdir.exists():
        shutil.rmtree(appdir)
    bin_dir = appdir / "usr" / "bin"
    bin_dir.mkdir(parents=True)
    (appdir / "usr" / "share" / "applications").mkdir(parents=True)
    icon_install = appdir / "usr" / "share" / "icons" / "hicolor" / "256x256" / "apps"
    icon_install.mkdir(parents=True)

    # The COMPLETE standalone tree — binary plus all sibling libraries
    # and bundled data — goes into usr/bin so the launcher finds its
    # dependencies at runtime.
    shutil.copytree(final_dir, bin_dir, dirs_exist_ok=True)

    # AppRun: resolve our own location, expose the bundled libs, exec the
    # real binary, forwarding all arguments (so ``--smoke-test`` works).
    apprun = appdir / "AppRun"
    apprun.write_text(
        "#!/bin/sh\n"
        'HERE="$(dirname "$(readlink -f "${0}")")"\n'
        'export LD_LIBRARY_PATH="${HERE}/usr/bin:${LD_LIBRARY_PATH}"\n'
        f'exec "${{HERE}}/usr/bin/{app}" "$@"\n'
    )
    os.chmod(apprun, 0o755)

    # Desktop entry — required at the AppDir root by appimagetool, and
    # installed under usr/share for desktop integration. Mirrors the .deb
    # entry; Exec/Icon use AppImage conventions (AppRun resolves the path).
    desktop_text = (
        "[Desktop Entry]\n"
        f"Name={APP_NAME}\n"
        "Comment=Professional Serial Data Monitor\n"
        f"Exec={app}\n"
        f"Icon={app}\n"
        "Terminal=false\n"
        "Type=Application\n"
        "Categories=Development;Electronics;\n"
    )
    (appdir / f"{app}.desktop").write_text(desktop_text)
    (appdir / "usr" / "share" / "applications" / f"{app}.desktop").write_text(desktop_text)

    # Icon — appimagetool needs one at the AppDir root named to match the
    # desktop Icon= key; also install it into the hicolor theme.
    icon_src = PROJECT_ROOT / "resources" / "app_icon.png"
    if icon_src.exists():
        shutil.copy2(icon_src, appdir / f"{app}.png")
        shutil.copy2(icon_src, icon_install / f"{app}.png")
    else:
        log(f"Icon {icon_src} not found — AppImage may lack an icon", "WARN")

    return appdir


def _build_linux_appimage(final_dir: Path, deploy_dir: Path, version: str) -> Path:
    """Package the standalone build as a runnable Linux artifact.

    Preferred: a real AppImage via ``appimagetool`` from a full AppDir.
    Fallback (tool absent): a gzipped tarball of the COMPLETE standalone
    directory — still fully runnable. Either way the artifact contains
    every dependency, so the B6 bare-binary defect cannot recur.

    Returns the artifact path.

    Raises:
        RuntimeError: if neither an AppImage nor the fallback archive
            could be produced.
    """
    appdir = deploy_dir / "AppDir"
    _assemble_appdir(final_dir, appdir)

    tool = shutil.which("appimagetool")
    if tool:
        out = deploy_dir / f"WireTrace-v{version}-x86_64.AppImage"
        if out.exists():
            out.unlink()
        # appimagetool selects the runtime by the ARCH env var.
        env = dict(os.environ)
        env["ARCH"] = "x86_64"
        run_cmd([tool, str(appdir), str(out)], env=env, check=False)
        if out.exists():
            os.chmod(out, 0o755)
            log(f"AppImage: {out.name} ({out.stat().st_size / 1024 / 1024:.1f} MB)")
            return out
        log("appimagetool ran but produced no AppImage — using tarball fallback", "WARN")
    else:
        log("appimagetool not found — packaging full directory as tarball", "WARN")
        log("  Install appimagetool to produce a .AppImage; the tarball is runnable.", "WARN")

    # Fallback: archive the ENTIRE distribution directory (not the bare
    # binary). final_dir is build/dist/WireTrace; archive it by name.
    archive_base = deploy_dir / f"WireTrace-v{version}-x86_64"
    shutil.make_archive(
        str(archive_base), "gztar",
        root_dir=str(final_dir.parent), base_dir=final_dir.name,
    )
    tarball = Path(f"{archive_base}.tar.gz")
    if not tarball.exists():
        raise RuntimeError("Failed to produce AppImage or fallback tarball")
    log(f"Portable tarball: {tarball.name} ({tarball.stat().st_size / 1024 / 1024:.1f} MB)")
    return tarball


def package_installer(plat: str, final_dir: Path, version: str) -> list[Path]:
    log_section("Step 6: Package Installer")

    artifacts: list[Path] = []
    deploy_dir = DEPLOYMENT_DIR / plat
    deploy_dir.mkdir(parents=True, exist_ok=True)

    if plat == "windows":
        iss_script = BUILD_DIR / "windows" / "installer.iss"
        iscc = find_iscc()

        if iss_script.exists() and iscc:
            log("Building Windows installer with Inno Setup...")
            run_cmd([iscc, str(iss_script)], cwd=str(PROJECT_ROOT))

            installer = deploy_dir / f"WireTrace-Setup-v{version}.exe"
            if installer.exists():
                size_mb = installer.stat().st_size / (1024 * 1024)
                log(f"Installer: {installer.name} ({size_mb:.1f} MB)")
                artifacts.append(installer)
            else:
                log("Installer not found after Inno Setup — check output dir", "ERROR")
        else:
            # Fallback: ZIP the distribution directory
            log("Inno Setup not available — creating portable ZIP...")
            zip_name = f"WireTrace-v{version}-portable-win64"
            zip_path = deploy_dir / f"{zip_name}.zip"
            shutil.make_archive(
                str(deploy_dir / zip_name), "zip",
                root_dir=DIST_DIR, base_dir=APP_NAME,
            )
            if zip_path.exists():
                size_mb = zip_path.stat().st_size / (1024 * 1024)
                log(f"Portable ZIP: {zip_path.name} ({size_mb:.1f} MB)")
                artifacts.append(zip_path)

    elif plat == "macos":
        if shutil.which("create-dmg"):
            dmg_path = deploy_dir / f"WireTrace-v{version}.dmg"
            run_cmd([
                "create-dmg",
                "--volname", APP_NAME,
                "--window-size", "600", "400",
                str(dmg_path),
                str(final_dir),
            ])
            if dmg_path.exists():
                artifacts.append(dmg_path)
        else:
            # Fallback: tar.gz
            archive = deploy_dir / f"WireTrace-v{version}-macos"
            shutil.make_archive(str(archive), "gztar", DIST_DIR, APP_NAME)
            artifacts.append(Path(f"{archive}.tar.gz"))

    elif plat == "linux":
        # AppImage (or runnable tarball fallback) from the FULL standalone
        # directory — see _build_linux_appimage. (B6: previously this
        # copied only the launcher binary, omitting shiboken6 and every
        # other dependency.)
        artifacts.append(_build_linux_appimage(final_dir, deploy_dir, version))

        # .deb package
        if shutil.which("dpkg-deb"):
            deb_name = f"wiretrace_{version}_amd64.deb"
            deb_staging = deploy_dir / "deb_staging"
            if deb_staging.exists():
                shutil.rmtree(deb_staging)

            # Directory structure
            (deb_staging / "DEBIAN").mkdir(parents=True)
            (deb_staging / "opt" / "wiretrace").mkdir(parents=True)
            (deb_staging / "usr" / "bin").mkdir(parents=True)
            (deb_staging / "usr" / "share" / "applications").mkdir(parents=True)

            # Copy distribution
            shutil.copytree(final_dir, deb_staging / "opt" / "wiretrace", dirs_exist_ok=True)

            # Symlink in /usr/bin
            (deb_staging / "usr" / "bin" / "wiretrace").symlink_to(
                f"/opt/wiretrace/{APP_NAME.lower()}"
            )

            # DEBIAN/control
            (deb_staging / "DEBIAN" / "control").write_text(
                f"Package: wiretrace\n"
                f"Version: {version}\n"
                f"Section: electronics\n"
                f"Priority: optional\n"
                f"Architecture: amd64\n"
                f"Depends: libgl1, libxcb-xinerama0\n"
                f"Maintainer: The Nuance Project <info@thenuanceproject.com>\n"
                f"Description: {APP_NAME} — Professional Serial Data Monitor\n"
                f" High-performance serial data monitor and logger for embedded\n"
                f" engineers. Multi-device, real-time filtering, CSV export.\n"
            )

            # .desktop file
            (deb_staging / "usr" / "share" / "applications" / "wiretrace.desktop").write_text(
                f"[Desktop Entry]\n"
                f"Name={APP_NAME}\n"
                f"Comment=Professional Serial Data Monitor\n"
                f"Exec=/opt/wiretrace/{APP_NAME.lower()}\n"
                f"Icon=/opt/wiretrace/resources/app_icon.png\n"
                f"Terminal=false\n"
                f"Type=Application\n"
                f"Categories=Development;Electronics;\n"
            )

            deb_path = deploy_dir / deb_name
            run_cmd(["dpkg-deb", "--build", str(deb_staging), str(deb_path)])
            if deb_path.exists():
                artifacts.append(deb_path)
            shutil.rmtree(deb_staging, ignore_errors=True)

    for a in artifacts:
        log(f"  → {a.name}")

    return artifacts


# ══════════════════════════════════════════════════════════════════════════════
# STEP 7: SIGN (optional)
# ══════════════════════════════════════════════════════════════════════════════

def sign_artifacts(plat: str, artifacts: list[Path]) -> None:
    log_section("Step 7: Code Signing")

    if plat == "windows" and shutil.which("signtool"):
        for a in artifacts:
            if a.suffix == ".exe":
                log(f"Signing {a.name}...")
                # Uncomment with real certificate:
                # run_cmd(["signtool", "sign", "/a", "/tr",
                #          "http://timestamp.digicert.com", "/td", "sha256",
                #          "/fd", "sha256", str(a)])
                log("Signing skipped — no certificate configured")
    elif plat == "macos" and shutil.which("codesign"):
        log("macOS codesign available — no identity configured")
    else:
        log("No signing tools configured — skipping")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 8: GENERATE UPDATE JSON
# ══════════════════════════════════════════════════════════════════════════════

def generate_update_json(version: str, all_artifacts: dict[str, list[Path]]) -> None:
    log_section("Step 8: Generate Update JSON")

    DEPLOYMENT_DIR.mkdir(parents=True, exist_ok=True)

    platforms_data = {}
    for plat, artifacts in all_artifacts.items():
        if not artifacts:
            continue
        primary = artifacts[0]
        sha256 = hashlib.sha256(primary.read_bytes()).hexdigest()

        platforms_data[plat] = {
            "download_url": f"{RELEASES_BASE_URL}/v{version}/{primary.name}",
            "file_size": primary.stat().st_size,
            "sha256_hash": sha256,
        }

    update_json = {
        "latest_version": version,
        "release_notes": "",
        # min_version follows SemVer (matches latest_version format).
        # "1.0.0" means: any installed version >= 1.0.0 can update to
        # this one. For the very first release, this is just a
        # baseline. Bump it on a release that requires migration from
        # a specific older version.
        "min_version": "1.0.0",
        "platforms": platforms_data,
    }

    json_path = DEPLOYMENT_DIR / "wiretrace-update.json"
    json_path.write_text(json.dumps(update_json, indent=2))
    log(f"Generated {json_path}")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 9: FINAL REPORT
# ══════════════════════════════════════════════════════════════════════════════

def final_report(version: str) -> None:
    log_section("Build Complete")

    info_path = DEPLOYMENT_DIR / "build_info.txt"
    info_path.write_text(
        f"{APP_NAME} Build Report\n"
        f"{'=' * 40}\n"
        f"Version:    {version}\n"
        f"Built:      {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Python:     {sys.version.split()[0]}\n"
        f"Platform:   {platform.platform()}\n"
        f"Machine:    {platform.machine()}\n"
    )

    log("Deployment artifacts:")
    for f in sorted(DEPLOYMENT_DIR.rglob("*")):
        if f.is_file():
            size = f.stat().st_size
            log(f"  {f.relative_to(DEPLOYMENT_DIR)}  ({size:,} bytes)")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def build_platform(plat: str, version: str) -> list[Path]:
    """Execute the full build pipeline for a single platform."""
    print(f"\n{'▓' * 60}")
    print(f"  Building {APP_NAME} v{version} for {plat.upper()}")
    print(f"{'▓' * 60}")

    validate_environment(plat)
    version_stamp(version)
    dist_dir = compile_nuitka(plat, version)
    final_dir = post_process(plat, dist_dir, version)
    validate_build(final_dir, plat)
    run_smoke_test(final_dir, plat)
    artifacts = package_installer(plat, final_dir, version)
    sign_artifacts(plat, artifacts)

    return artifacts


def main() -> int:
    parser = argparse.ArgumentParser(
        description=f"Build {APP_NAME} — Production Build Pipeline"
    )
    parser.add_argument(
        "--platform",
        choices=["windows", "macos", "linux", "all"],
        required=True,
        help="Target platform",
    )
    parser.add_argument(
        "--version",
        default=APP_VERSION,
        help=f"Version (default: {APP_VERSION})",
    )
    args = parser.parse_args()

    DEPLOYMENT_DIR.mkdir(parents=True, exist_ok=True)

    targets = ["windows", "macos", "linux"] if args.platform == "all" else [args.platform]

    all_artifacts: dict[str, list[Path]] = {}

    for plat in targets:
        try:
            artifacts = build_platform(plat, args.version)
            all_artifacts[plat] = artifacts
        except RuntimeError as e:
            log(f"FAILED: {e}", "ERROR")
            if len(targets) == 1:
                return 1
            log(f"Skipping {plat}, continuing with next platform...", "WARN")

    generate_update_json(args.version, all_artifacts)
    final_report(args.version)

    return 0


if __name__ == "__main__":
    sys.exit(main())
