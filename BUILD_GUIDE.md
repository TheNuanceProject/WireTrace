# WireTrace — Build Guide

Production build instructions for WireTrace.

---

## Prerequisites (Windows)

Install these before building:

### 1. Python 3.10+ (64-bit)

```
https://www.python.org/downloads/
```

During install, check **"Add Python to PATH"**.

Verify:
```cmd
python --version
```

### 2. Nuitka (Python-to-C compiler)

```cmd
pip install nuitka ordered-set
```

Verify:
```cmd
python -m nuitka --version
```

### 3. C Compiler

Nuitka needs a C compiler. On first run it will offer to download MinGW64 automatically. Alternatively, install **Visual Studio Build Tools**:

```
https://visualstudio.microsoft.com/visual-cpp-build-tools/
```

Select: **"Desktop development with C++"**

### 4. Inno Setup 6 (installer builder)

```
https://jrsoftware.org/isdl.php
```

During install, ensure **"Install ISCC"** is checked.

The build script auto-detects Inno Setup in common locations (`C:\Program Files (x86)\Inno Setup 6\`, etc.) — no need to add it to PATH manually.

### 5. Python Dependencies

From the WireTrace project root:
```cmd
pip install -r requirements.txt
pip install -r requirements-build.txt
```

---

## Build Commands

### Full Build (recommended)

From the project root directory:

```cmd
python build/build.py --platform windows
```

This runs the complete pipeline:
1. Validates environment
2. Stamps version
3. Compiles with Nuitka (~5-15 minutes first time)
4. Renames binary to WireTrace.exe
5. Creates installer via Inno Setup
6. Generates SHA-256 hashes and update JSON

### Custom Version

```cmd
python build/build.py --platform windows --version 1.0.1
```

---

## Build Output

After a successful build:

```
deployment/
└── windows/
    └── WireTrace-Setup-v1.1.0.exe    ← Installer
```

The intermediate standalone directory is at:
```
build/dist/WireTrace/
├── WireTrace.exe              ← Main executable
├── python3xx.dll              ← Python runtime
├── PySide6/                   ← Qt6 libraries
├── resources/                 ← Bundled assets
│   ├── app_icon.ico
│   ├── app_icon.png
│   ├── icons/*.svg
│   └── help/
│       └── user_guide.html    ← User Guide (loaded by F1 / Help menu)
├── ui/themes/                 ← QSS theme files
│   ├── studio_light.qss
│   └── midnight_dark.qss
└── [other DLLs]               ← Nuitka dependencies
```

> **Bundling the User Guide.** The Nuitka build must include
> `resources/help/` so the F1 / Help menu can find the User Guide
> HTML in the installed app. Pass
> `--include-data-dir=resources/help=resources/help` to Nuitka.
> The same applies to `resources/icons/` and `ui/themes/` — every
> data directory that the running app reads from at runtime needs
> an explicit `--include-data-dir` flag. The help loader
> (`app/help_loader.py`) verifies these paths at runtime and falls
> back across dev-tree, frozen-exe, and macOS .app-bundle layouts,
> so even a partial bundle reports a clear error rather than failing
> silently.

---

## Installation Modes

The installer supports **two modes**, chosen at install time:

### Admin Install (All Users)

- Right-click the installer → **"Run as administrator"**
- Or select **"Install for all users"** when prompted
- Installs to: `C:\Program Files\WireTrace\`
- Start Menu entry visible to all users
- Requires admin password

### Non-Admin Install (Current User Only)

- Double-click the installer normally
- Select **"Install for me only"** when prompted
- Installs to: `C:\Users\{you}\AppData\Local\Programs\WireTrace\`
- Start Menu entry for current user only
- No admin password needed

Both modes create:
- Desktop shortcut (optional)
- Start Menu shortcut (optional)
- Uninstaller in Add/Remove Programs

---

## Portable Mode (No Installer)

If Inno Setup is not installed, the build script creates a portable ZIP instead:

```
deployment/windows/WireTrace-v1.1.0-portable-win64.zip
```

Extract anywhere and run `WireTrace.exe` directly. No installation needed.

---

## Build Pipeline Stages

The build runs as eight sequential steps. The build halts on the first
failure — partial artifacts are never shipped.

1. **Validate environment** — Python, Nuitka, PySide6, Inno Setup.
2. **Version stamp** — write the version into `version.py` and
   `installer.iss`.
3. **Compile with Nuitka** — `--standalone` directory build with
   PySide6 plugin and explicit `--include-package` flags for
   `pyqtgraph` and `numpy` (these are reached only through lazy
   imports and Nuitka's static analyser cannot follow them on its
   own).
4. **Post-process** — rename `main.dist/` to `WireTrace/` and the
   `main.exe` to `WireTrace.exe`.
5. **Validate build** — confirm the .exe exists and resources +
   themes are bundled.
6. **Smoke test** — run the compiled .exe with `--smoke-test`. This
   imports every lazy-loaded module (PlotView, ConfigDialog, Help
   loader, etc.) inside the frozen runtime. If any import is
   missing, the build halts before the installer is built. This is
   the guardrail that catches the "works in dev, broken in .exe"
   class of bug.
7. **Package installer** — Inno Setup builds `WireTrace-Setup-vX.Y.Z.exe`.
8. **Generate update manifest** — `deployment/wiretrace-update.json`
   with the .exe's SHA-256 hash. This is copied to the site repo's
   `public/updates/` directory only AFTER the GitHub Release is
   published.

If Step 6 fails, the build output names the failing module(s). The
fix is almost always to add another `--include-package=<name>` flag
in `compile_nuitka()` in `build/build.py`.

---

## Troubleshooting

### "Nuitka not found"
```cmd
pip install nuitka ordered-set
```

### "No C compiler found"
Let Nuitka download MinGW64 automatically (say yes when prompted), or install Visual Studio Build Tools.

### "iscc not found"
Install Inno Setup 6 from https://jrsoftware.org/isdl.php. The build script auto-detects common install locations. If it still isn't found, ensure ISCC.exe exists in `C:\Program Files (x86)\Inno Setup 6\`.

### Build takes very long
First Nuitka build compiles the entire Python runtime to C. This takes 10-20 minutes. Subsequent builds are cached and much faster (~2-5 minutes).

### "Resources not found" at runtime
This means QSS themes or icons weren't bundled. Verify:
```cmd
dir build\dist\WireTrace\resources\
dir build\dist\WireTrace\ui\themes\
```

Both directories should exist with files. If not, re-run the build.

### Smoke test failed (Step 6)
The build output names the missing module — for example:

```
FAIL  pyqtgraph (live plotter rendering library): ModuleNotFoundError: No module named 'pyqtgraph'
```

Fix: open `build/build.py`, locate `compile_nuitka()`, and add the
missing item to the `cmd = [...]` list. Two flag shapes are
relevant depending on what's missing:

* `--include-package=<name>` — for a third-party package on PyPI,
  bundles the whole package and its submodules. Use for: `numpy`,
  `pyqtgraph`, `requests`, etc.
* `--include-module=<dotted.path>` — for a single submodule of an
  already-bundled package. Use for: `PySide6.QtOpenGL`,
  `PySide6.QtSvg`, etc. (PySide6 itself is included by the plugin,
  but its optional submodules need explicit flags.)

Read the failing module name carefully:

* `ModuleNotFoundError: No module named 'foo'` (no dot) → add
  `--include-package=foo`
* `ModuleNotFoundError: No module named 'foo.bar'` (with a dot, and
  `foo` is already bundled) → add `--include-module=foo.bar`

Rebuild. The smoke test runs again; iterate until it passes. **Do
not package or ship a binary that failed the smoke test.**

### Antivirus blocks the executable
Nuitka-compiled binaries are sometimes flagged by antivirus software. This is a false positive. Code signing (Step 7) eliminates this — requires a code signing certificate.

---

## Cross-Platform Builds

### macOS
```bash
python build/build.py --platform macos
```
Outputs: `deployment/macos/WireTrace-v1.1.0.dmg`

### Linux
```bash
python build/build.py --platform linux
```
Outputs:
- `deployment/linux/WireTrace-v1.1.0-x86_64.AppImage`
- `deployment/linux/wiretrace_1.1.0_amd64.deb`
