# WireTrace — Build Guide

Production build instructions for WireTrace v2.0.

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
    └── WireTrace-Setup-v1.0.0.exe    ← Installer
```

The intermediate standalone directory is at:
```
build/dist/WireTrace/
├── WireTrace.exe              ← Main executable
├── python3xx.dll              ← Python runtime
├── PySide6/                   ← Qt6 libraries
├── resources/                 ← Icons, app_icon.ico/png
│   └── icons/*.svg
├── ui/themes/                 ← QSS theme files
│   ├── studio_light.qss
│   └── midnight_dark.qss
└── [other DLLs]               ← Nuitka dependencies
```

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
deployment/windows/WireTrace-v1.0.0-portable-win64.zip
```

Extract anywhere and run `WireTrace.exe` directly. No installation needed.

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

### Antivirus blocks the executable
Nuitka-compiled binaries are sometimes flagged by antivirus software. This is a false positive. Code signing (Step 7) eliminates this — requires a code signing certificate.

---

## Cross-Platform Builds

### macOS
```bash
python build/build.py --platform macos
```
Outputs: `deployment/macos/WireTrace-v1.0.0.dmg`

### Linux
```bash
python build/build.py --platform linux
```
Outputs:
- `deployment/linux/WireTrace-v1.0.0-x86_64.AppImage`
- `deployment/linux/wiretrace_1.0.0_amd64.deb`
