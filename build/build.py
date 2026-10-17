# SPDX-License-Identifier: GPL-3.0-or-later
"""Build WireTrace for the system this runs on.

A build is made on the system it is for: Windows on Windows, Linux on
Linux. There is no macOS build; on macOS WireTrace is run from source.

Usage:
    python build/build.py                    # compile, check and package
    python build/build.py --stage compile    # stop at the checked program folder
    python build/build.py --stage package    # package a program folder made earlier

The two stages exist so that the program can be code-signed between
them: the installer is then made from the signed files.

Compile:
    1. Check what a release needs: a key to verify updates with, licence
       texts that match the installed packages, the tools.
    2. Compile with Nuitka into a folder that holds the program and
       everything it needs.
    3. Linux: add the libraries the X11 window system plugin needs that
       not every desktop has.
    4. Add the licence texts of what the build carries of this system:
       the Python interpreter, and on Linux the system libraries.
    5. Check that folder: the files the program reads at run time are
       there, and the program passes its own ``--smoke-test``.
    6. Linux: measure the oldest C library the build runs on.

Package:
    Windows: the installer, made by Inno Setup.
    Linux:   an AppImage and a ``.deb``.

Nothing in the repository is written to. The version, the names and the
wording all come from ``version.py``, ``app/constants.py`` and
``updater/``. Results go to ``deployment/<system>/``.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from xml.etree import ElementTree

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = PROJECT_ROOT / "build" / "dist"
DEPLOYMENT_DIR = PROJECT_ROOT / "deployment"
INSTALLER_SCRIPT = PROJECT_ROOT / "build" / "windows" / "installer.iss"
EXCLUDE_LIST = PROJECT_ROOT / "build" / "linux" / "excludelist"
#: The licence of the runtime every AppImage starts with, as its makers
#: publish it for the version the release workflow pins.
APPIMAGE_RUNTIME_LICENSE = PROJECT_ROOT / "build" / "linux" / "appimage-runtime-license.txt"
COMPILER_REPORT = DIST_DIR / "report.xml"

sys.path.insert(0, str(PROJECT_ROOT))
from app.constants import (
    INSTALLER_CHOICES_FILE,
    INSTALLER_CHOICES_SECTION,
    INSTALLER_UPDATE_CHECK_KEY,
    WINDOWS_INSTALLER_MUTEX,
)
from app.licenses import ENCODING, SYSTEM_LICENSE_SUFFIX, SYSTEM_LICENSES_DIR
from tools import collect_licenses
from updater.install import UPDATE_CHECK_OPTION, system_name
from updater.manifest import ManifestError, decode_public_key
from updater.privacy import PRIVACY_STATEMENT
from version import (
    APP_AUTHOR,
    APP_DESCRIPTION,
    APP_MAINTAINER_EMAIL,
    APP_NAME,
    APP_VERSION,
    APP_WEBSITE,
    COPYRIGHT,
    LINUX_APPIMAGE_NAME,
    LINUX_DEB_NAME,
    UPDATE_PUBLIC_KEYS,
    WINDOWS_INSTALLER_NAME,
)

STAGES = ("compile", "package", "all")
SYSTEMS = ("windows", "linux")

#: The folder Nuitka writes, named after the entry point, and the program in it.
_COMPILED_DIR_NAME = "main.dist"
_COMPILED_NAMES = {"windows": "main.exe", "linux": "main.bin"}
_EXECUTABLE_NAMES = {"windows": f"{APP_NAME}.exe", "linux": APP_NAME.lower()}

#: Files of the repository that go beside the program.
_ROOT_DOCUMENTS = ("LICENSE", "NOTICE")

#: What the program reads at run time, relative to its folder. A glob
#: must match at least one file.
_REQUIRED_FILES = (
    "LICENSE",
    "resources/help/user_guide.html",
    "resources/help/images/*.png",
    "resources/icons/*.svg",
    "resources/fonts/*.ttf",
    "resources/licenses/THIRD_PARTY_LICENSES.txt",
    "ui/themes/*.qss.template",
)

#: What the build itself adds to the folder. Checked like the above.
_ADDED_FILES = (
    f"{SYSTEM_LICENSES_DIR.relative_to(PROJECT_ROOT).as_posix()}/*{SYSTEM_LICENSE_SUFFIX}",
)

#: The plugin that puts windows on an X11 display. Qt loads it at run
#: time, so the libraries it needs are looked up here.
_X11_PLUGIN = "PySide6/qt-plugins/platforms/libqxcb.so"

#: Kept in the repository to make the licence file; not part of a build.
_NOT_INSTALLED = ("resources/licenses/components.json", "resources/licenses/texts")

#: The oldest C library a Linux build may need: that of Ubuntu 22.04,
#: the system releases are built on. Qt itself needs 2.34.
LINUX_GLIBC_BASELINE = (2, 35)

_SMOKE_TEST_ARGUMENT = "--smoke-test"
_SMOKE_TEST_TIMEOUT_S = 120
#: What the smoke test prints when the program runs from its AppImage.
_APPIMAGE_RECOGNISED = "Installed as: appimage"
_ELF_MAGIC = b"\x7fELF"
_GLIBC_VERSION = re.compile(rb"GLIBC_(\d+)\.(\d+)")
_PRIVACY_FILE_NAME = "privacy.txt"
_INNO_SETUP_FOLDER = "Inno Setup 6"
_INNO_SETUP_COMPILER = "ISCC.exe"
_PROGRAM_FILES_VARIABLES = ("ProgramFiles(x86)", "ProgramFiles")
_INTERPRETER_NAME = "Python"
_APPIMAGE_RUNTIME_NAME = "AppImage runtime"
_LIBRARY_MODE = 0o755
_LIBRARY_RPATH = "$ORIGIN"
_LDD_LINE = re.compile(r"^\s*(\S+) => (\S+)")
_LDD_NOT_FOUND = "not"
_REPORT_LIBRARY = "included_dll"
_REPORT_SOURCE = "source_path"
#: The compiler writes a path inside the Python environment as ``${...}/...``.
_REPORT_ENVIRONMENT_PREFIX = "${"
_DEBIAN_COPYRIGHT = "/usr/share/doc/{package}/copyright"
#: The folders that a merged /usr makes one with their /usr namesakes
#: (/lib is /usr/lib). A package records a file under the name it was
#: installed by, which may be either.
_MERGED_USR_FOLDERS = ("bin", "sbin", "lib", "lib32", "lib64", "libx32")
_SHLIBS_PREFIX = "shlibs:Depends="
_DEB_INSTALL_DIR = f"/opt/{APP_NAME.lower()}"
_ICON_NAME = "app_icon"
_ICON_SIZE_DIR = "256x256"
_EXECUTABLE_MODE = 0o755


class BuildError(Exception):
    """The build cannot go on."""


def log(message: str) -> None:
    print(f"  {message}", flush=True)


def section(title: str) -> None:
    print(f"\n== {title}", flush=True)


def run(command: list[str], *, env: Mapping[str, str] | None = None) -> None:
    """Run a command of the build. Its output is shown as it runs.

    Raises:
        BuildError: it could not be started, or it failed.
    """
    log("$ " + " ".join(command))
    try:
        result = subprocess.run(command, cwd=PROJECT_ROOT, env=env, check=False)
    except OSError as exc:
        raise BuildError(f"{command[0]} could not be started: {exc}") from exc
    if result.returncode != 0:
        raise BuildError(f"{command[0]} failed with exit code {result.returncode}")


def target_system() -> str:
    system = system_name()
    if system not in SYSTEMS:
        raise BuildError(f"no build is made for {system}; run {APP_NAME} from source instead")
    return system


def program_dir() -> Path:
    return DIST_DIR / APP_NAME


def executable(system: str) -> Path:
    return program_dir() / _EXECUTABLE_NAMES[system]


def deployment_dir(system: str) -> Path:
    return DEPLOYMENT_DIR / system


# ── What a release needs ─────────────────────────────────────────────────────


def find_inno_setup() -> str | None:
    """The Inno Setup compiler: on the path, or where its installer puts it."""
    found = shutil.which(_INNO_SETUP_COMPILER)
    if found:
        return found
    for variable in _PROGRAM_FILES_VARIABLES:
        base = os.environ.get(variable)
        if base:
            candidate = Path(base) / _INNO_SETUP_FOLDER / _INNO_SETUP_COMPILER
            if candidate.is_file():
                return str(candidate)
    return None


def _find_tools(names: Iterable[str]) -> dict[str, str]:
    found = {name: shutil.which(name) for name in names}
    missing = [name for name, path in found.items() if not path]
    if missing:
        raise BuildError("not found: " + ", ".join(missing))
    return {name: path for name, path in found.items() if path}


def compile_tools(system: str) -> dict[str, str]:
    """The tools the compile stage runs beside the compiler, by name.

    A Linux build is made on Debian or Ubuntu: the licence texts of the
    system libraries it carries are read from the package system.

    Raises:
        BuildError: one is missing.
    """
    if system == "linux":
        return _find_tools(("patchelf", "ldd", "dpkg-query"))
    return {}


def packaging_tools(system: str) -> dict[str, str]:
    """The tools that package a build on ``system``, by name.

    Raises:
        BuildError: one is missing.
    """
    if system == "windows":
        compiler = find_inno_setup()
        if not compiler:
            raise BuildError("not found: Inno Setup")
        return {"Inno Setup": compiler}
    return _find_tools(("appimagetool", "dpkg-deb", "dpkg-shlibdeps"))


def check_update_keys() -> None:
    """A build that trusts no key could never be updated.

    Raises:
        BuildError: ``UPDATE_PUBLIC_KEYS`` is empty or holds a bad key.
    """
    if not UPDATE_PUBLIC_KEYS:
        raise BuildError(
            "UPDATE_PUBLIC_KEYS in version.py is empty. Make the keys with "
            "tools/release_keys.py and add their public halves."
        )
    try:
        for key in UPDATE_PUBLIC_KEYS:
            decode_public_key(key)
    except ManifestError as exc:
        raise BuildError(f"UPDATE_PUBLIC_KEYS in version.py: {exc}") from exc


def check_licenses() -> None:
    """The licence file must list the packages that are installed here,
    at their versions.

    Raises:
        BuildError: it does not.
    """
    output = collect_licenses.OUTPUT
    try:
        found = collect_licenses.differences(output.read_text(encoding=collect_licenses.ENCODING))
    except (OSError, KeyError, collect_licenses.LicenseError,
            collect_licenses.metadata.PackageNotFoundError) as exc:
        raise BuildError(f"the licence file could not be checked: {exc}") from exc
    if found:
        raise BuildError(
            f"{output.relative_to(PROJECT_ROOT)} does not list what is installed: "
            + "; ".join(found)
            + ". Install from requirements-release.txt."
        )


def check_release_inputs(system: str) -> None:
    section("Check what a release needs")
    check_update_keys()
    log(f"Update keys: {len(UPDATE_PUBLIC_KEYS)}")
    check_licenses()
    log("The licence file lists the installed packages")
    for name, path in {**compile_tools(system), **packaging_tools(system)}.items():
        log(f"{name}: {path}")


# ── Compile ──────────────────────────────────────────────────────────────


def nuitka_command(system: str) -> list[str]:
    resources = PROJECT_ROOT / "resources"
    command = [
        sys.executable, "-m", "nuitka",
        # A folder, not one file: the program reads its stylesheet,
        # fonts, icons and guide from disk beside itself.
        "--standalone",
        f"--output-dir={DIST_DIR}",
        "--enable-plugin=pyside6",
        # No --assume-yes-for-downloads: the compiler downloads nothing a
        # build has not pinned. On Windows it needs Dependency Walker, which
        # the release workflow fetches and checks by SHA-256 into the
        # folder NUITKA_CACHE_DIR_DOWNLOADS names. Any other download it
        # wants stops a build without a console.
        f"--product-name={APP_NAME}",
        f"--product-version={APP_VERSION}",
        f"--company-name={APP_AUTHOR}",
        f"--file-description={APP_DESCRIPTION}",
        f"--copyright={COPYRIGHT}",
        f"--include-data-dir={resources}=resources",
        f"--include-data-dir={PROJECT_ROOT / 'ui' / 'themes'}=ui/themes",
        # Reached only when the plot is first opened, so the compiler
        # does not find them by reading the imports. The smoke test
        # fails if one of them is missing from the build.
        "--include-package=pyqtgraph",
        "--include-package=numpy",
        # The test suites and example programs of those packages are no
        # part of the program.
        "--nofollow-import-to=*.tests",
        "--nofollow-import-to=pyqtgraph.examples",
        # Command-line editing, which a program without a console never
        # uses. Leaving it out keeps the GPL readline library out of the
        # build.
        "--nofollow-import-to=readline",
        # Says where each library of the build was taken from.
        f"--report={COMPILER_REPORT}",
        "--include-module=PySide6.QtOpenGL",
        "--include-module=PySide6.QtOpenGLWidgets",
        "--include-module=PySide6.QtSvg",
    ]
    if system == "windows":
        command.append(f"--windows-icon-from-ico={resources / (_ICON_NAME + '.ico')}")
        command.append("--windows-console-mode=disable")
    command.append(str(PROJECT_ROOT / "main.py"))
    return command


def compile_program(system: str) -> Path:
    """Compile into ``build/dist/<name>/`` and return that folder."""
    section("Compile")
    compiled = DIST_DIR / _COMPILED_DIR_NAME
    target = program_dir()
    for stale in (compiled, target):
        if stale.exists():
            shutil.rmtree(stale)
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    run(nuitka_command(system))
    if not (compiled / _COMPILED_NAMES[system]).is_file():
        raise BuildError(f"the compiler did not produce {_COMPILED_NAMES[system]}")

    compiled.rename(target)
    (target / _COMPILED_NAMES[system]).rename(executable(system))
    for name in _ROOT_DOCUMENTS:
        shutil.copy2(PROJECT_ROOT / name, target / name)
    for name in _NOT_INSTALLED:
        unwanted = target / name
        if unwanted.is_dir():
            shutil.rmtree(unwanted)
        elif unwanted.exists():
            unwanted.unlink()

    system_libraries = libraries_from_the_system(COMPILER_REPORT)
    if system == "linux":
        added = add_x11_libraries(target)
        log(f"X11 libraries added: {', '.join(path.name for path in added)}")
        system_libraries.extend(added)
    for name in add_system_licenses(target, system, system_libraries):
        log(f"Licence text added: {name}")

    files = [path for path in target.rglob("*") if path.is_file()]
    size = sum(path.stat().st_size for path in files)
    log(f"{target.relative_to(PROJECT_ROOT)}: {len(files)} files, {size / 1024 / 1024:.1f} MB")
    return target


# ── What a build carries of the system it is made on ─────────────────────────


def libraries_from_the_system(report: Path) -> list[Path]:
    """The libraries the compiler took from the operating system, as
    opposed to the Python environment, read from its report.

    Raises:
        BuildError: the report cannot be read.
    """
    # The report is UTF-8 but names its encoding in a way the parser does
    # not know, so the parser is told.
    parser = ElementTree.XMLParser(encoding=ENCODING)
    try:
        root = ElementTree.parse(report, parser=parser).getroot()
    except (OSError, ElementTree.ParseError) as exc:
        raise BuildError(f"the compiler's report could not be read: {exc}") from exc
    sources = (entry.get(_REPORT_SOURCE, "") for entry in root.iter(_REPORT_LIBRARY))
    return [
        Path(source) for source in sources
        if source and not source.startswith(_REPORT_ENVIRONMENT_PREFIX)
    ]


def excluded_libraries(path: Path = EXCLUDE_LIST) -> set[str]:
    """The libraries every Linux desktop is expected to have."""
    names = set()
    for line in path.read_text(encoding=ENCODING).splitlines():
        name = line.split("#", 1)[0].strip()
        if name:
            names.add(name)
    return names


def _output_of(command: list[str], *, cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, cwd=cwd)
    except OSError as exc:
        raise BuildError(f"{command[0]} could not be started: {exc}") from exc
    if result.returncode != 0:
        raise BuildError(f"{' '.join(command)} failed: {result.stderr.strip()}")
    return result.stdout


def libraries_loaded_by(library: Path) -> dict[str, Path | None]:
    """Where this system finds each library that ``library`` loads, by
    name. None for one it does not have."""
    found: dict[str, Path | None] = {}
    for line in _output_of([compile_tools("linux")["ldd"], str(library)]).splitlines():
        match = _LDD_LINE.match(line)
        if match:
            name, location = match.groups()
            found[name] = None if location == _LDD_NOT_FOUND else Path(location)
    return found


def libraries_needed_by(library: Path) -> list[str]:
    """The names of the libraries ``library`` itself asks for."""
    patchelf = compile_tools("linux")["patchelf"]
    return _output_of([patchelf, "--print-needed", str(library)]).split()


def add_x11_libraries(folder: Path) -> list[Path]:
    """Copy into ``folder`` the libraries its X11 plugin needs that a
    desktop may not have, and return where each was copied from.

    Qt cannot open a window on X11 without them, and stock installations
    of several distributions lack one or another. A library on the
    exclude list is left to the system, and so is everything only such a
    library needs.

    Raises:
        BuildError: the plugin is not in the build, or this system does
            not have a library that must be carried.
    """
    plugin = folder / _X11_PLUGIN
    if not plugin.is_file():
        raise BuildError(f"the build does not contain {_X11_PLUGIN}")
    patchelf = compile_tools("linux")["patchelf"]
    on_this_system = libraries_loaded_by(plugin)
    excluded = excluded_libraries()

    added: list[Path] = []
    pending = libraries_needed_by(plugin)
    while pending:
        name = pending.pop()
        target = folder / name
        if name in excluded or target.exists():
            continue
        source = on_this_system.get(name)
        if source is None:
            raise BuildError(
                f"{name} is not installed on this system. The X11 plugin needs it and "
                "the build carries it along. Install it and build again."
            )
        shutil.copyfile(source, target)
        target.chmod(_LIBRARY_MODE)
        # So that it finds the libraries copied beside it.
        _output_of([patchelf, "--set-rpath", _LIBRARY_RPATH, str(target)])
        added.append(source)
        pending.extend(libraries_needed_by(target))
    return added


def names_of(library: Path, root: Path = Path("/")) -> list[Path]:
    """Every name under which this system holds ``library``: as given, its
    real path, and the other name of each where /usr is merged.

    A merged name is included only if this system shows it is the same
    file, so a name is never assumed.
    """
    names: list[Path] = []

    def add(path: Path) -> None:
        if path not in names:
            names.append(path)

    for path in (library, library.resolve()):
        add(path)
        try:
            parts = path.relative_to(root).parts
        except ValueError:
            continue
        if len(parts) > 2 and parts[0] == "usr" and parts[1] in _MERGED_USR_FOLDERS:
            other = root.joinpath(*parts[1:])
        elif len(parts) > 1 and parts[0] in _MERGED_USR_FOLDERS:
            other = root.joinpath("usr", *parts)
        else:
            continue
        if other.exists() and os.path.samefile(other, path):
            add(other)
    return names


def package_of(library: Path) -> str:
    """The Debian package that installed ``library``.

    Raises:
        BuildError: no package recorded the library under any of its names.
    """
    query = compile_tools("linux")["dpkg-query"]
    names = names_of(library)
    for name in names:
        try:
            owner = _output_of([query, "--search", str(name)])
        except BuildError:
            continue
        return owner.split(":", 1)[0].strip()
    raise BuildError(
        f"no package recorded {library.name} under any of its names: "
        + ", ".join(str(name) for name in names)
    )


def debian_copyright_files(libraries: Iterable[Path]) -> dict[str, Path]:
    """The copyright file of the package each library belongs to, by
    package name.

    Raises:
        BuildError: a library belongs to no package, or its package has
            no copyright file.
    """
    files: dict[str, Path] = {}
    for library in libraries:
        package = package_of(library)
        copyright_file = Path(_DEBIAN_COPYRIGHT.format(package=package))
        if not copyright_file.is_file():
            raise BuildError(f"the package {package} has no copyright file")
        files[package] = copyright_file
    return files


def add_system_licenses(folder: Path, system: str, libraries: Iterable[Path]) -> list[str]:
    """Write into the build the licence text of each part of this system
    that the build carries, and return their names.

    The interpreter's own licence file is taken as it is: on Windows it
    also covers the libraries the interpreter is shipped with.

    Raises:
        BuildError: a text cannot be found.
    """
    try:
        texts = {_INTERPRETER_NAME: collect_licenses.interpreter_license()}
    except collect_licenses.LicenseError as exc:
        raise BuildError(str(exc)) from exc
    if system == "linux":
        texts.update(debian_copyright_files(libraries))

    target = folder / SYSTEM_LICENSES_DIR.relative_to(PROJECT_ROOT)
    target.mkdir(parents=True, exist_ok=True)
    for name, source in texts.items():
        text = source.read_text(encoding=ENCODING, errors="replace")
        (target / f"{name}{SYSTEM_LICENSE_SUFFIX}").write_text(
            text, encoding=ENCODING, newline="\n",
        )
    return sorted(texts)


# ── Check the program folder ─────────────────────────────────────────────────


def missing_files(folder: Path) -> list[str]:
    """The entries of ``_REQUIRED_FILES`` that ``folder`` does not hold."""
    return [pattern for pattern in _REQUIRED_FILES if not any(folder.glob(pattern))]


def check_contents(system: str) -> None:
    """Raises BuildError if the program or a file it reads is missing."""
    if not executable(system).is_file():
        raise BuildError(
            f"{executable(system)} was not found. Run the compile stage first."
        )
    missing = missing_files(program_dir()) + [
        pattern for pattern in _ADDED_FILES if not any(program_dir().glob(pattern))
    ]
    if missing:
        raise BuildError("the build does not contain: " + ", ".join(missing))
    log("The files the program reads are in the build")


def smoke_test(command: list[str]) -> str:
    """Run the built program's own self-check.

    It imports everything the program loads late, starts the window
    system, fills the user guide, verifies a known update signature and
    reads the licence texts. A build that fails is not packaged.

    Returns:
        What the program printed.

    Raises:
        BuildError: it failed or did not finish.
    """
    log("$ " + " ".join(command))
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=_SMOKE_TEST_TIMEOUT_S, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BuildError(f"the smoke test did not finish in {_SMOKE_TEST_TIMEOUT_S} s") from exc
    except OSError as exc:
        raise BuildError(f"the built program could not be started: {exc}") from exc
    output = result.stdout + result.stderr
    for line in output.splitlines():
        log(f"  {line}")
    if result.returncode != 0:
        raise BuildError(f"the smoke test failed with exit code {result.returncode}")
    return output


def _is_elf(path: Path) -> bool:
    with path.open("rb") as file:
        return file.read(len(_ELF_MAGIC)) == _ELF_MAGIC


def required_glibc(folder: Path) -> tuple[int, int] | None:
    """The newest C library version any program or library in ``folder``
    asks for, or None if none asks for one.

    Each names the versions it needs as ``GLIBC_2.34`` in its symbol
    table. The highest one named is the oldest system the build runs on.
    """
    newest: tuple[int, int] | None = None
    for path in folder.rglob("*"):
        if not path.is_file() or path.is_symlink() or not _is_elf(path):
            continue
        for major, minor in _GLIBC_VERSION.findall(path.read_bytes()):
            version = (int(major), int(minor))
            if newest is None or version > newest:
                newest = version
    return newest


def check_glibc(folder: Path, baseline: tuple[int, int] = LINUX_GLIBC_BASELINE) -> None:
    """Raises BuildError if the build needs a newer C library than the baseline."""
    needed = required_glibc(folder)
    if needed is None:
        raise BuildError("no program in the build names a C library version")
    wanted = ".".join(str(part) for part in baseline)
    found = ".".join(str(part) for part in needed)
    if needed > baseline:
        raise BuildError(
            f"the build needs glibc {found}, newer than the baseline {wanted}. "
            "Build on the baseline system."
        )
    log(f"Runs on glibc {found} and later (baseline {wanted})")


def check_program(system: str) -> None:
    section("Check the build")
    check_contents(system)
    smoke_test([str(executable(system)), _SMOKE_TEST_ARGUMENT])
    if system == "linux":
        check_glibc(program_dir())


# ── Package: Windows ─────────────────────────────────────────────────────────


def installer_definitions(privacy_file: Path, output_dir: Path) -> dict[str, str]:
    """Everything the installer script is told, by name.

    Raises:
        BuildError: a value cannot be written into the script as it is.
    """
    definitions = {
        "AppName": APP_NAME,
        "AppVersion": APP_VERSION,
        "AppPublisher": APP_AUTHOR,
        "AppUrl": APP_WEBSITE,
        "AppDescription": APP_DESCRIPTION,
        "AppCopyright": COPYRIGHT,
        "AppExeName": _EXECUTABLE_NAMES["windows"],
        "AppMutexName": WINDOWS_INSTALLER_MUTEX,
        "SourceDir": str(program_dir()),
        "OutputDir": str(output_dir),
        "OutputName": Path(
            WINDOWS_INSTALLER_NAME.format(name=APP_NAME, version=APP_VERSION)
        ).stem,
        "IconFile": str(PROJECT_ROOT / "resources" / f"{_ICON_NAME}.ico"),
        "PrivacyFile": str(privacy_file),
        "UpdateOption": UPDATE_CHECK_OPTION,
        "ChoicesFile": INSTALLER_CHOICES_FILE,
        "ChoicesSection": INSTALLER_CHOICES_SECTION,
        "ChoicesKey": INSTALLER_UPDATE_CHECK_KEY,
    }
    for name, value in definitions.items():
        # The script places these inside quoted strings.
        if not value or any(character in value for character in "\"'\r\n"):
            raise BuildError(f"the installer cannot be given {name} as it is: {value!r}")
    return definitions


def write_privacy_statement(path: Path) -> None:
    """The privacy statement the installer shows, as a text file.

    Raises:
        BuildError: it holds a character the installer cannot show.
    """
    if not PRIVACY_STATEMENT.isascii():
        raise BuildError("the privacy statement must be plain ASCII for the installer")
    path.write_text(PRIVACY_STATEMENT, encoding="ascii", newline="\r\n")


def package_windows() -> list[Path]:
    output_dir = deployment_dir("windows")
    output_dir.mkdir(parents=True, exist_ok=True)
    privacy_file = DIST_DIR / _PRIVACY_FILE_NAME
    write_privacy_statement(privacy_file)
    compiler = packaging_tools("windows")["Inno Setup"]
    definitions = installer_definitions(privacy_file, output_dir)
    run([
        compiler,
        *(f"/D{name}={value}" for name, value in definitions.items()),
        str(INSTALLER_SCRIPT),
    ])
    installer = output_dir / WINDOWS_INSTALLER_NAME.format(name=APP_NAME, version=APP_VERSION)
    if not installer.is_file():
        raise BuildError(f"Inno Setup did not produce {installer.name}")
    return [installer]


# ── Package: Linux ───────────────────────────────────────────────────────────


def desktop_entry(command: str, icon: str) -> str:
    return (
        "[Desktop Entry]\n"
        f"Name={APP_NAME}\n"
        f"Comment={APP_DESCRIPTION}\n"
        f"Exec={command}\n"
        f"Icon={icon}\n"
        "Terminal=false\n"
        "Type=Application\n"
        "Categories=Development;Electronics;\n"
    )


def assemble_appdir(source: Path, appdir: Path) -> Path:
    """Lay out ``source``, the whole program folder, as an AppDir.

    The program and everything beside it go to ``usr/bin``. ``AppRun``
    starts the program and passes its arguments on.
    """
    name = APP_NAME.lower()
    if appdir.exists():
        shutil.rmtree(appdir)
    bin_dir = appdir / "usr" / "bin"
    applications = appdir / "usr" / "share" / "applications"
    icons = appdir / "usr" / "share" / "icons" / "hicolor" / _ICON_SIZE_DIR / "apps"
    for folder in (applications, icons):
        folder.mkdir(parents=True)
    shutil.copytree(source, bin_dir, symlinks=True)

    apprun = appdir / "AppRun"
    apprun.write_text(
        "#!/bin/sh\n"
        'HERE="$(dirname "$(readlink -f "${0}")")"\n'
        f'exec "${{HERE}}/usr/bin/{name}" "$@"\n',
        encoding="utf-8", newline="\n",
    )
    apprun.chmod(_EXECUTABLE_MODE)

    entry = desktop_entry(name, name)
    for target in (appdir / f"{name}.desktop", applications / f"{name}.desktop"):
        target.write_text(entry, encoding="utf-8", newline="\n")
    icon = PROJECT_ROOT / "resources" / f"{_ICON_NAME}.png"
    for target in (appdir / f"{name}.png", icons / f"{name}.png"):
        shutil.copy2(icon, target)
    return appdir


def build_appimage(output_dir: Path, runtime_file: Path | None) -> Path:
    appdir = assemble_appdir(program_dir(), DIST_DIR / "AppDir")
    # The AppImage alone carries the runtime, so it alone carries its licence.
    licences = appdir / "usr" / "bin" / SYSTEM_LICENSES_DIR.relative_to(PROJECT_ROOT)
    shutil.copyfile(
        APPIMAGE_RUNTIME_LICENSE, licences / f"{_APPIMAGE_RUNTIME_NAME}{SYSTEM_LICENSE_SUFFIX}",
    )
    appimage = output_dir / LINUX_APPIMAGE_NAME.format(name=APP_NAME, version=APP_VERSION)
    appimage.unlink(missing_ok=True)

    command = [packaging_tools("linux")["appimagetool"]]
    if runtime_file is not None:
        command.append(f"--runtime-file={runtime_file}")
    command.extend([str(appdir), str(appimage)])
    # The tool is itself an AppImage. Unpacking itself to run needs no
    # FUSE, which a build machine may not have.
    run(command, env={**os.environ, "ARCH": "x86_64", "APPIMAGE_EXTRACT_AND_RUN": "1"})
    if not appimage.is_file():
        raise BuildError(f"appimagetool did not produce {appimage.name}")
    appimage.chmod(_EXECUTABLE_MODE)
    return appimage


def deb_dependencies(staging: Path, install_dir: Path) -> str:
    """The packages the program needs from the system, worked out by the
    package tools from the libraries the program and the X11 plugin load.

    Plugins a system may do without (a desktop's own theme, printing,
    Wayland) are not counted: Qt leaves out a plugin it cannot load.

    Raises:
        BuildError: the tool fails or names nothing.
    """
    tool = packaging_tools("linux")["dpkg-shlibdeps"]
    programs = [
        path for path in sorted(install_dir.iterdir(), key=lambda path: path.name)
        if path.is_file() and not path.is_symlink() and _is_elf(path)
    ]
    programs.append(install_dir / _X11_PLUGIN)
    # The tool reads debian/control in the folder it runs in: the
    # description of a source package, of which it needs the names only.
    name = APP_NAME.lower()
    control = staging / "debian" / "control"
    control.parent.mkdir()
    control.write_text(
        f"Source: {name}\n\nPackage: {name}\nArchitecture: amd64\n",
        encoding="utf-8", newline="\n",
    )
    try:
        output = _output_of(
            [tool, "-O", f"-l{install_dir}", *(f"-e{path}" for path in programs)], cwd=staging,
        )
    finally:
        shutil.rmtree(control.parent)
    for line in output.splitlines():
        if line.startswith(_SHLIBS_PREFIX) and line[len(_SHLIBS_PREFIX):].strip():
            return line[len(_SHLIBS_PREFIX):].strip()
    raise BuildError("dpkg-shlibdeps named no package the program depends on")


def deb_control(installed_kib: int, depends: str) -> str:
    return (
        f"Package: {APP_NAME.lower()}\n"
        f"Version: {APP_VERSION}\n"
        "Section: electronics\n"
        "Priority: optional\n"
        "Architecture: amd64\n"
        f"Installed-Size: {installed_kib}\n"
        f"Depends: {depends}\n"
        f"Maintainer: {APP_AUTHOR} <{APP_MAINTAINER_EMAIL}>\n"
        f"Homepage: {APP_WEBSITE}\n"
        f"Description: {APP_NAME}: {APP_DESCRIPTION}\n"
        " Serial data monitor and logger for embedded work. Several devices\n"
        " at once, filtering, live plots, and export to text and CSV.\n"
    )


def build_deb(output_dir: Path) -> Path:
    name = APP_NAME.lower()
    staging = DIST_DIR / "deb"
    if staging.exists():
        shutil.rmtree(staging)
    install_dir = staging / _DEB_INSTALL_DIR.lstrip("/")
    shutil.copytree(program_dir(), install_dir, symlinks=True)

    bin_dir = staging / "usr" / "bin"
    applications = staging / "usr" / "share" / "applications"
    for folder in (staging / "DEBIAN", bin_dir, applications):
        folder.mkdir(parents=True)
    (bin_dir / name).symlink_to(f"{_DEB_INSTALL_DIR}/{name}")
    (applications / f"{name}.desktop").write_text(
        desktop_entry(
            f"{_DEB_INSTALL_DIR}/{name}",
            f"{_DEB_INSTALL_DIR}/resources/{_ICON_NAME}.png",
        ),
        encoding="utf-8", newline="\n",
    )
    installed = sum(path.stat().st_size for path in install_dir.rglob("*") if path.is_file())
    depends = deb_dependencies(staging, install_dir)
    log(f"Depends: {depends}")
    (staging / "DEBIAN" / "control").write_text(
        deb_control(-(-installed // 1024), depends), encoding="utf-8", newline="\n",
    )

    deb = output_dir / LINUX_DEB_NAME.format(version=APP_VERSION)
    deb.unlink(missing_ok=True)
    # Owned by root in the package, whoever builds it.
    run([packaging_tools("linux")["dpkg-deb"], "--root-owner-group", "--build",
         str(staging), str(deb)])
    if not deb.is_file():
        raise BuildError(f"dpkg-deb did not produce {deb.name}")
    return deb


def package_linux(runtime_file: Path | None) -> list[Path]:
    output_dir = deployment_dir("linux")
    output_dir.mkdir(parents=True, exist_ok=True)
    appimage = build_appimage(output_dir, runtime_file)
    # The AppImage is what the update check installs, so it is proved
    # to start as it will be downloaded.
    output = smoke_test([str(appimage), "--appimage-extract-and-run", _SMOKE_TEST_ARGUMENT])
    # It must also know that it is an AppImage, or it would send its
    # users to the release page instead of updating itself.
    if _APPIMAGE_RECOGNISED not in output:
        raise BuildError("the AppImage does not recognise itself as one")
    return [appimage, build_deb(output_dir)]


def package(system: str, runtime_file: Path | None) -> list[Path]:
    section("Package")
    check_contents(system)
    artifacts = package_windows() if system == "windows" else package_linux(runtime_file)
    for artifact in artifacts:
        size = artifact.stat().st_size / 1024 / 1024
        log(f"{artifact.relative_to(PROJECT_ROOT)}  ({size:.1f} MB)")
    return artifacts


# ── Entry point ──────────────────────────────────────────────────────────


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--stage", choices=STAGES, default="all",
        help="compile the program folder, package it, or both (default)",
    )
    parser.add_argument(
        "--appimage-runtime", type=Path, default=None,
        help="Linux: the AppImage runtime to embed, instead of the one "
             "appimagetool would download",
    )
    options = parser.parse_args(arguments)

    try:
        system = target_system()
        print(f"{APP_NAME} {APP_VERSION} for {system}", flush=True)
        if options.stage in ("compile", "all"):
            check_release_inputs(system)
            compile_program(system)
            check_program(system)
        if options.stage in ("package", "all"):
            package(system, options.appimage_runtime)
    except BuildError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
