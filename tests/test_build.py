# SPDX-License-Identifier: GPL-3.0-or-later
"""The build script: what it checks, and what it tells the packaging tools.

The compiler and the packaging tools are not run here. What is tested is
everything the script decides itself.
"""

from __future__ import annotations

import importlib.util
import os
import re
import stat
from pathlib import Path

import pytest

from updater.install import UPDATE_CHECK_OPTION
from updater.privacy import PRIVACY_STATEMENT
from version import (
    APP_MAINTAINER_EMAIL,
    APP_NAME,
    APP_PRIVACY_URL,
    APP_VERSION,
    WINDOWS_INSTALLER_NAME,
)

ROOT = Path(__file__).resolve().parent.parent

# build/build.py is a script, not part of a package: loaded by its path.
_spec = importlib.util.spec_from_file_location("wiretrace_build", ROOT / "build" / "build.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)

INSTALLER_SCRIPT = (ROOT / "build" / "windows" / "installer.iss").read_text(encoding="utf-8")
A_KEY = "PUAXw+hDiVqStwqnTRt+vJyYLM8uxJaMwM1V8Sr0Zgw="


def _program_folder(tmp_path: Path) -> Path:
    """A stand-in for a compiled program folder: the program and the
    files beside it."""
    folder = tmp_path / APP_NAME
    (folder / "resources").mkdir(parents=True)
    (folder / "PySide6").mkdir()
    (folder / "wiretrace").write_bytes(b"\x7fELF program")
    (folder / "libshiboken6.abi3.so").write_bytes(b"\x7fELF shiboken")
    (folder / "PySide6" / "QtCore.so").write_bytes(b"\x7fELF qtcore")
    (folder / "resources" / "app_icon.svg").write_text("<svg/>", encoding="utf-8")
    return folder


class TestTheSystemBuiltFor:
    @pytest.mark.parametrize("system", ["windows", "linux"])
    def test_windows_and_linux_are_built(self, monkeypatch, system):
        monkeypatch.setattr(build, "system_name", lambda: system)
        assert build.target_system() == system

    def test_no_build_is_made_for_macos(self, monkeypatch, capsys):
        monkeypatch.setattr(build, "system_name", lambda: "macos")
        with pytest.raises(build.BuildError):
            build.target_system()
        assert build.main([]) == 1
        assert "macos" in capsys.readouterr().err


class TestWhatAReleaseNeeds:
    def test_a_build_that_trusts_no_key_is_refused(self, monkeypatch):
        monkeypatch.setattr(build, "UPDATE_PUBLIC_KEYS", ())
        with pytest.raises(build.BuildError):
            build.check_update_keys()

    def test_a_key_that_cannot_be_read_is_refused(self, monkeypatch):
        monkeypatch.setattr(build, "UPDATE_PUBLIC_KEYS", ("not a key",))
        with pytest.raises(build.BuildError):
            build.check_update_keys()

    def test_a_good_key_is_accepted(self, monkeypatch):
        monkeypatch.setattr(build, "UPDATE_PUBLIC_KEYS", (A_KEY,))
        build.check_update_keys()

    def test_a_licence_file_that_lists_other_versions_is_refused(self, monkeypatch):
        monkeypatch.setattr(
            build.collect_licenses, "differences", lambda text: ["Example: 1 is listed"],
        )
        with pytest.raises(build.BuildError, match="Example"):
            build.check_licenses()

    def test_a_licence_file_that_lists_what_is_installed_passes(self, monkeypatch):
        monkeypatch.setattr(build.collect_licenses, "differences", lambda text: [])
        build.check_licenses()

    def test_a_missing_packaging_tool_is_an_error(self, monkeypatch):
        monkeypatch.setattr(build.shutil, "which", lambda name: None)
        for variable in build._PROGRAM_FILES_VARIABLES:
            monkeypatch.delenv(variable, raising=False)
        for system in build.SYSTEMS:
            with pytest.raises(build.BuildError):
                build.packaging_tools(system)

    def test_a_missing_compile_tool_is_named(self, monkeypatch):
        monkeypatch.setattr(
            build.shutil, "which", lambda name: None if name == "patchelf" else f"/usr/bin/{name}",
        )
        with pytest.raises(build.BuildError, match="patchelf"):
            build.compile_tools("linux")
        assert build.compile_tools("windows") == {}

    def test_inno_setup_is_found_where_its_installer_puts_it(self, monkeypatch, tmp_path):
        compiler = tmp_path / build._INNO_SETUP_FOLDER / build._INNO_SETUP_COMPILER
        compiler.parent.mkdir()
        compiler.write_bytes(b"")
        monkeypatch.setattr(build.shutil, "which", lambda name: None)
        monkeypatch.setenv(build._PROGRAM_FILES_VARIABLES[0], str(tmp_path))
        assert build.find_inno_setup() == str(compiler)


class TestCompiling:
    @pytest.mark.parametrize("system", ["windows", "linux"])
    def test_the_build_is_a_folder_with_the_files_the_program_reads(self, system):
        command = build.nuitka_command(system)
        assert "--standalone" in command
        assert "--onefile" not in command
        assert any(item.endswith("=resources") for item in command)
        assert any(item.endswith("=ui/themes") for item in command)
        assert f"--product-version={APP_VERSION}" in command
        assert command[-1].endswith("main.py")

    def test_packages_reached_only_at_run_time_are_named(self):
        command = build.nuitka_command("linux")
        for package in ("pyqtgraph", "numpy"):
            assert f"--include-package={package}" in command

    def test_test_suites_and_examples_of_packages_are_left_out(self):
        command = build.nuitka_command("linux")
        assert "--nofollow-import-to=*.tests" in command
        assert "--nofollow-import-to=pyqtgraph.examples" in command

    def test_the_windows_build_opens_no_console(self):
        assert "--windows-console-mode=disable" in build.nuitka_command("windows")

    def test_only_the_windows_build_has_windows_options(self, monkeypatch):
        """Read by option, not by text: the interpreter's own path can hold
        the word, as GitHub's Windows machines keep Python under
        ``C:\\hostedtoolcache\\windows``."""
        monkeypatch.setattr(
            build.sys, "executable", r"C:\hostedtoolcache\windows\Python\3.12.10\x64\python.exe",
        )
        options = [item for item in build.nuitka_command("linux") if item.startswith("--")]
        assert options
        assert not any(option.startswith("--windows-") for option in options)


class TestCheckingTheBuild:
    def test_every_required_file_is_in_the_source_tree(self):
        """The same list, applied to the source: a file that was renamed
        or removed cannot stay on the list unnoticed."""
        assert build.missing_files(ROOT) == []

    def test_a_missing_file_is_named(self, tmp_path):
        assert set(build.missing_files(tmp_path)) == set(build._REQUIRED_FILES)

    def test_files_kept_out_of_a_build_exist_in_the_source(self):
        for name in build._NOT_INSTALLED:
            assert (ROOT / name).exists()


REPORT = """<?xml version='1.0' encoding='utf8'?>
<nuitka-compilation-report>
  <included_dll name="libQt6Core.so.6" source_path="${sys.prefix}/lib/PySide6/Qt/lib/libQt6Core.so.6" />
  <included_dll name="libssl.so.3" source_path="/lib/x86_64-linux-gnu/libssl.so.3" />
  <included_dll name="libffi.so.8" source_path="/lib/x86_64-linux-gnu/libffi.so.8" />
  <included_extension name="_ssl.so" source_path="${sys.real_prefix}/lib/_ssl.so" />
  <option value="--copyright=\u00a9 2026" />
</nuitka-compilation-report>
"""


class TestWhatABuildCarriesOfTheSystem:
    def test_libraries_from_the_system_are_read_from_the_report(self, tmp_path):
        report = tmp_path / "report.xml"
        report.write_text(REPORT, encoding="utf-8")
        assert [path.name for path in build.libraries_from_the_system(report)] == [
            "libssl.so.3", "libffi.so.8",
        ]

    def test_a_report_that_cannot_be_read_stops_the_build(self, tmp_path):
        with pytest.raises(build.BuildError):
            build.libraries_from_the_system(tmp_path / "absent.xml")
        broken = tmp_path / "report.xml"
        broken.write_text("<nuitka-compilation-report>", encoding="utf-8")
        with pytest.raises(build.BuildError):
            build.libraries_from_the_system(broken)

    def test_the_exclude_list_names_libraries_and_no_comments(self):
        excluded = build.excluded_libraries()
        assert {"libc.so.6", "libxcb.so.1", "libX11.so.6", "libGL.so.1"} <= excluded
        assert "libxcb-cursor.so.0" not in excluded
        assert all(" " not in name and not name.startswith("#") for name in excluded)

    @pytest.fixture
    def x11(self, tmp_path, monkeypatch):
        """A build folder with an X11 plugin, on a system that has some
        libraries. Returns the folder and the commands that were run."""
        folder = tmp_path / "build"
        plugin = folder / build._X11_PLUGIN
        plugin.parent.mkdir(parents=True)
        plugin.write_bytes(b"plugin")
        (folder / "libQt6Core.so.6").write_bytes(b"qt")
        system = tmp_path / "system"
        system.mkdir()
        for name in ("libxcb-cursor.so.0", "libxcb-image.so.0", "libxcb-util.so.1"):
            (system / name).write_bytes(name.encode())

        needed = {
            plugin.name: ["libQt6Core.so.6", "libxcb.so.1", "libxcb-cursor.so.0", "libc.so.6"],
            "libxcb-cursor.so.0": ["libxcb-image.so.0", "libxcb.so.1"],
            "libxcb-image.so.0": ["libxcb-util.so.1", "libxcb-cursor.so.0"],
            "libxcb-util.so.1": ["libxcb.so.1"],
        }
        commands: list[list[str]] = []
        monkeypatch.setattr(
            build, "compile_tools", lambda system: {"patchelf": "patchelf", "ldd": "ldd"},
        )
        monkeypatch.setattr(build, "libraries_needed_by", lambda path: list(needed[path.name]))
        monkeypatch.setattr(
            build, "libraries_loaded_by",
            lambda path: {name: system / name for name in ("libxcb-cursor.so.0",
                                                            "libxcb-image.so.0",
                                                            "libxcb-util.so.1")},
        )
        monkeypatch.setattr(
            build, "_output_of", lambda command, **options: commands.append(command) or "",
        )
        return folder, commands

    def test_libraries_the_x11_plugin_needs_are_copied_with_those_they_need(self, x11):
        folder, commands = x11
        added = build.add_x11_libraries(folder)
        assert sorted(path.name for path in added) == [
            "libxcb-cursor.so.0", "libxcb-image.so.0", "libxcb-util.so.1",
        ]
        for path in added:
            assert (folder / path.name).read_bytes() == path.name.encode()
        # Each is told to look beside itself for the others.
        assert sorted(command[-1] for command in commands) == sorted(
            str(folder / path.name) for path in added
        )
        assert all(command[1:3] == ["--set-rpath", "$ORIGIN"] for command in commands)

    def test_excluded_libraries_and_those_already_in_the_build_are_left(self, x11):
        folder, _ = x11
        before = (folder / "libQt6Core.so.6").read_bytes()
        build.add_x11_libraries(folder)
        assert (folder / "libQt6Core.so.6").read_bytes() == before
        assert not (folder / "libxcb.so.1").exists()
        assert not (folder / "libc.so.6").exists()

    def test_a_library_this_system_lacks_stops_the_build(self, x11, monkeypatch):
        folder, _ = x11
        monkeypatch.setattr(build, "libraries_loaded_by", lambda path: {"libxcb-cursor.so.0": None})
        with pytest.raises(build.BuildError, match=r"libxcb-cursor\.so\.0"):
            build.add_x11_libraries(folder)

    def test_a_build_without_the_x11_plugin_is_refused(self, tmp_path):
        with pytest.raises(build.BuildError):
            build.add_x11_libraries(tmp_path)

    def test_what_ldd_prints_is_read(self, monkeypatch):
        printed = (
            "\tlinux-vdso.so.1 (0x00007ffd)\n"
            "\tlibxcb.so.1 => /lib/x86_64-linux-gnu/libxcb.so.1 (0x00007f00)\n"
            "\tlibxcb-cursor.so.0 => not found\n"
            "\t/lib64/ld-linux-x86-64.so.2 (0x00007f01)\n"
        )
        monkeypatch.setattr(build, "compile_tools", lambda system: {"ldd": "ldd"})
        monkeypatch.setattr(build, "_output_of", lambda command, **options: printed)
        assert build.libraries_loaded_by(Path("plugin.so")) == {
            "libxcb.so.1": Path("/lib/x86_64-linux-gnu/libxcb.so.1"),
            "libxcb-cursor.so.0": None,
        }

    def test_the_licence_of_each_system_part_is_written_into_the_build(
        self, tmp_path, monkeypatch,
    ):
        interpreter = tmp_path / "LICENSE.txt"
        interpreter.write_text("the interpreter's licence", encoding="utf-8")
        copyright_file = tmp_path / "copyright"
        copyright_file.write_text("the library's licence", encoding="utf-8")
        monkeypatch.setattr(build.collect_licenses, "interpreter_license", lambda: interpreter)
        monkeypatch.setattr(
            build, "debian_copyright_files", lambda libraries: {"libssl3": copyright_file},
        )
        folder = tmp_path / "build"

        assert build.add_system_licenses(folder, "linux", [Path("/lib/libssl.so.3")]) == [
            "Python", "libssl3",
        ]
        written = folder / build.SYSTEM_LICENSES_DIR.relative_to(build.PROJECT_ROOT)
        assert (written / "Python.txt").read_text(encoding="utf-8") == "the interpreter's licence"
        assert (written / "libssl3.txt").read_text(encoding="utf-8") == "the library's licence"
        # They are where the check of the finished build looks for them.
        assert any(folder.glob(build._ADDED_FILES[0]))

    def test_on_windows_only_the_interpreters_licence_is_added(self, tmp_path, monkeypatch):
        interpreter = tmp_path / "LICENSE.txt"
        interpreter.write_text("licence", encoding="utf-8")
        monkeypatch.setattr(build.collect_licenses, "interpreter_license", lambda: interpreter)
        assert build.add_system_licenses(tmp_path / "build", "windows", []) == ["Python"]

    @pytest.mark.skipif(os.name != "posix", reason="a step of the Linux build")
    def test_a_library_is_named_as_given_and_by_its_real_path(self, tmp_path):
        real = tmp_path / "libssl.so.3.0.2"
        real.write_bytes(b"")
        link = tmp_path / "libssl.so.3"
        link.symlink_to(real)

        assert build.names_of(link, root=tmp_path) == [link, real.resolve()]

    @pytest.mark.skipif(os.name != "posix", reason="a step of the Linux build")
    def test_where_usr_is_merged_a_library_has_both_names(self, tmp_path):
        """As on Ubuntu 22.04: /lib is /usr/lib, and a package may have
        recorded its library under either name."""
        root = tmp_path.resolve()
        folder = root / "usr" / "lib" / "x86_64-linux-gnu"
        folder.mkdir(parents=True)
        library = folder / "libbz2.so.1.0.4"
        library.write_bytes(b"")
        (root / "lib").symlink_to(root / "usr" / "lib")

        assert build.names_of(library, root=root) == [
            library, root / "lib" / "x86_64-linux-gnu" / "libbz2.so.1.0.4",
        ]

    @pytest.mark.skipif(os.name != "posix", reason="a step of the Linux build")
    def test_a_name_that_is_another_file_is_not_used(self, tmp_path):
        root = tmp_path.resolve()
        for top in (root / "usr" / "lib", root / "lib"):
            top.mkdir(parents=True)
            (top / "libx.so.1").write_bytes(b"")

        assert build.names_of(root / "usr" / "lib" / "libx.so.1", root=root) == [
            root / "usr" / "lib" / "libx.so.1",
        ]

    def test_the_package_is_found_under_the_name_it_recorded(self, tmp_path, monkeypatch):
        real = Path("/usr/lib/x86_64-linux-gnu/libbz2.so.1.0.4")
        recorded = Path("/lib/x86_64-linux-gnu/libbz2.so.1.0.4")
        asked: list[str] = []

        def output(command, **options):
            asked.append(command[-1])
            if command[-1] != str(recorded):
                raise build.BuildError(f"dpkg-query: no path found matching pattern {command[-1]}")
            return f"libbz2-1.0:amd64: {recorded}\n"

        monkeypatch.setattr(build, "compile_tools", lambda system: {"dpkg-query": "dpkg-query"})
        monkeypatch.setattr(build, "names_of", lambda library: [real, recorded])
        monkeypatch.setattr(build, "_output_of", output)

        assert build.package_of(real) == "libbz2-1.0"
        assert asked == [str(real), str(recorded)]

    def test_a_library_no_package_recorded_stops_the_build(self, monkeypatch):
        library = Path("/opt/libunknown.so.1")

        def output(command, **options):
            raise build.BuildError("dpkg-query: no path found")

        monkeypatch.setattr(build, "compile_tools", lambda system: {"dpkg-query": "dpkg-query"})
        monkeypatch.setattr(build, "_output_of", output)

        with pytest.raises(build.BuildError, match=re.escape("libunknown.so.1")):
            build.package_of(library)

    def test_each_library_gives_its_packages_copyright_file(self, tmp_path, monkeypatch):
        copyright_file = tmp_path / "copyright"
        copyright_file.write_text("text", encoding="utf-8")
        monkeypatch.setattr(build, "package_of", lambda library: "libssl3")
        monkeypatch.setattr(build, "_DEBIAN_COPYRIGHT", str(copyright_file))

        assert build.debian_copyright_files([tmp_path / "libssl.so.3"]) == {"libssl3": copyright_file}

    def test_a_package_without_a_copyright_file_stops_the_build(self, tmp_path, monkeypatch):
        monkeypatch.setattr(build, "compile_tools", lambda system: {"dpkg-query": "dpkg-query"})
        monkeypatch.setattr(build, "_output_of", lambda command, **options: "pkg: /x\n")
        monkeypatch.setattr(build, "_DEBIAN_COPYRIGHT", str(tmp_path / "{package}" / "absent"))
        with pytest.raises(build.BuildError, match="pkg"):
            build.debian_copyright_files([tmp_path / "libx.so"])


class TestTheCLibraryABuildNeeds:
    def _folder(self, tmp_path: Path, *versions: str) -> Path:
        folder = tmp_path / "build"
        folder.mkdir()
        names = b"\0".join(f"GLIBC_{version}".encode() for version in versions)
        (folder / "program").write_bytes(b"\x7fELF\0\0" + names + b"\0")
        return folder

    def test_the_newest_version_named_is_the_one_needed(self, tmp_path):
        folder = self._folder(tmp_path, "2.2.5", "2.34", "2.17", "2.4")
        assert build.required_glibc(folder) == (2, 34)

    def test_versions_compare_as_numbers(self, tmp_path):
        assert build.required_glibc(self._folder(tmp_path, "2.9", "2.10")) == (2, 10)

    def test_a_file_that_is_not_a_program_is_not_read(self, tmp_path):
        folder = self._folder(tmp_path, "2.17")
        (folder / "notes.txt").write_bytes(b"needs GLIBC_2.99")
        assert build.required_glibc(folder) == (2, 17)

    def test_libraries_in_subfolders_are_read(self, tmp_path):
        folder = self._folder(tmp_path, "2.17")
        (folder / "lib").mkdir()
        (folder / "lib" / "library.so").write_bytes(b"\x7fELF\0GLIBC_2.31\0")
        assert build.required_glibc(folder) == (2, 31)

    def test_a_build_at_the_baseline_passes(self, tmp_path):
        baseline = ".".join(str(part) for part in build.LINUX_GLIBC_BASELINE)
        build.check_glibc(self._folder(tmp_path, "2.17", baseline))

    def test_a_build_that_needs_a_newer_library_fails(self, tmp_path):
        major, minor = build.LINUX_GLIBC_BASELINE
        with pytest.raises(build.BuildError):
            build.check_glibc(self._folder(tmp_path, f"{major}.{minor + 1}"))

    def test_a_folder_without_programs_fails(self, tmp_path):
        with pytest.raises(build.BuildError):
            build.check_glibc(tmp_path)


class TestTheWindowsInstaller:
    def _definitions(self, tmp_path: Path) -> dict[str, str]:
        return build.installer_definitions(tmp_path / "privacy.txt", tmp_path / "out")

    def test_the_script_is_given_every_name_it_uses(self, tmp_path):
        used = set(re.findall(r"\{#(\w+)\}", INSTALLER_SCRIPT))
        defined_in_script = set(re.findall(r"^#define (\w+)", INSTALLER_SCRIPT, re.MULTILINE))
        assert used - defined_in_script == set(self._definitions(tmp_path))

    def test_the_script_refuses_to_compile_without_any_of_them(self, tmp_path):
        required = set(re.findall(r"^#ifndef (\w+)", INSTALLER_SCRIPT, re.MULTILINE))
        assert required == set(self._definitions(tmp_path))

    def test_the_script_states_no_version_of_its_own(self):
        assert not re.search(r"\d+\.\d+\.\d+", INSTALLER_SCRIPT.replace("10.0.17763", ""))

    def test_the_installer_is_named_as_the_update_check_expects(self, tmp_path):
        expected = WINDOWS_INSTALLER_NAME.format(name=APP_NAME, version=APP_VERSION)
        assert self._definitions(tmp_path)["OutputName"] + ".exe" == expected

    def test_a_value_that_would_break_the_script_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(build, "UPDATE_CHECK_OPTION", "It's an option")
        with pytest.raises(build.BuildError):
            self._definitions(tmp_path)

    def test_the_installer_words_the_option_as_preferences_does(self, tmp_path):
        assert self._definitions(tmp_path)["UpdateOption"] == UPDATE_CHECK_OPTION
        assert 'Description: "{#UpdateOption}"' in INSTALLER_SCRIPT

    def test_the_update_check_is_ticked_unless_it_is_unticked(self):
        task = next(
            line for line in INSTALLER_SCRIPT.splitlines() if line.startswith('Name: "updatecheck"')
        )
        assert "unchecked" not in task

    def test_the_installer_shows_the_privacy_statement_before_installing(self, tmp_path):
        assert "InfoBeforeFile={#PrivacyFile}" in INSTALLER_SCRIPT
        path = tmp_path / "privacy.txt"
        build.write_privacy_statement(path)
        content = path.read_bytes()
        assert content.decode("ascii").replace("\r\n", "\n") == PRIVACY_STATEMENT
        assert b"\n" not in content.replace(b"\r\n", b"")

    def test_the_privacy_statement_names_everything_a_check_sends(self):
        statement = " ".join(PRIVACY_STATEMENT.split())
        for stated in ("the version of WireTrace", "the operating system", "the processor type",
                       "how WireTrace was installed", "the date of the previous check",
                       "It carries no identifier", "network address",
                       "the country and an approximate area", "never stores it",
                       "Usage statistics are off", "does not switch them on",
                       "Preferences", "GitHub receives each update check",
                       "Usage reports and deletion requests never reach GitHub",
                       APP_PRIVACY_URL, f"Questions and requests: {APP_MAINTAINER_EMAIL}"):
            assert stated in statement

    def test_the_installer_waits_for_the_mutex_the_application_holds(self):
        assert "AppMutex={#AppMutexName}" in INSTALLER_SCRIPT

    def test_the_answer_is_recorded_where_the_application_reads_it(self):
        recorded = [
            line for line in INSTALLER_SCRIPT.splitlines()
            if line.startswith('Filename: "{app}\\{#ChoicesFile}"; Section: "{#ChoicesSection}"; '
                               'Key: "{#ChoicesKey}"')
        ]
        assert len(recorded) == 2
        assert 'String: "true";  Tasks: updatecheck;' in recorded[0]
        assert 'String: "false"; Tasks: not updatecheck;' in recorded[1]


class TestTheAppImage:
    def test_the_whole_program_folder_goes_in(self, tmp_path):
        appdir = build.assemble_appdir(_program_folder(tmp_path), tmp_path / "AppDir")
        bin_dir = appdir / "usr" / "bin"
        assert (bin_dir / "wiretrace").is_file()
        assert (bin_dir / "libshiboken6.abi3.so").is_file()
        assert (bin_dir / "PySide6" / "QtCore.so").is_file()
        assert (bin_dir / "resources" / "app_icon.svg").is_file()

    def test_apprun_starts_the_program_and_passes_its_arguments(self, tmp_path):
        appdir = build.assemble_appdir(_program_folder(tmp_path), tmp_path / "AppDir")
        apprun = appdir / "AppRun"
        text = apprun.read_text(encoding="utf-8")
        assert text.startswith("#!/bin/sh\n")
        assert 'usr/bin/wiretrace" "$@"' in text
        if os.name == "posix":
            assert apprun.stat().st_mode & stat.S_IXUSR

    def test_apprun_does_not_change_where_other_programs_find_libraries(self, tmp_path):
        """A library path set here would be inherited by the browser the
        guide opens in, which would then load the libraries of this build."""
        appdir = build.assemble_appdir(_program_folder(tmp_path), tmp_path / "AppDir")
        assert "LD_LIBRARY_PATH" not in (appdir / "AppRun").read_text(encoding="utf-8")

    def test_the_desktop_entry_and_icon_are_where_the_tool_looks(self, tmp_path):
        appdir = build.assemble_appdir(_program_folder(tmp_path), tmp_path / "AppDir")
        entry = (appdir / "wiretrace.desktop").read_text(encoding="utf-8")
        assert f"Name={APP_NAME}\n" in entry
        assert "Exec=wiretrace\n" in entry
        assert "Icon=wiretrace\n" in entry
        assert (appdir / "wiretrace.png").is_file()

    def test_an_earlier_appdir_is_replaced(self, tmp_path):
        appdir = tmp_path / "AppDir"
        (appdir / "usr" / "bin").mkdir(parents=True)
        (appdir / "usr" / "bin" / "left_over").write_bytes(b"")
        build.assemble_appdir(_program_folder(tmp_path), appdir)
        assert not (appdir / "usr" / "bin" / "left_over").exists()


class TestTheDebPackage:
    def test_the_control_file_states_the_version_and_the_maintainer(self):
        control = build.deb_control(1234, "libc6 (>= 2.35), libgl1")
        assert f"Version: {APP_VERSION}\n" in control
        assert "Installed-Size: 1234\n" in control
        assert "Depends: libc6 (>= 2.35), libgl1\n" in control
        assert f"<{APP_MAINTAINER_EMAIL}>" in control
        assert control.endswith("\n")

    def test_what_the_package_depends_on_is_what_the_package_tool_works_out(
        self, tmp_path, monkeypatch,
    ):
        staging = tmp_path / "deb"
        install_dir = staging / "opt" / "wiretrace"
        (install_dir / "PySide6" / "qt-plugins" / "platforms").mkdir(parents=True)
        (install_dir / build._X11_PLUGIN).write_bytes(b"\x7fELF plugin")
        (install_dir / "wiretrace").write_bytes(b"\x7fELF program")
        (install_dir / "LICENSE").write_text("text", encoding="utf-8")
        asked: list[list[str]] = []

        def output(command, **options):
            asked.append(command)
            assert (options["cwd"] / "debian" / "control").is_file()
            return "shlibs:Depends=libc6 (>= 2.35), libgl1\n"

        monkeypatch.setattr(build, "packaging_tools", lambda system: {"dpkg-shlibdeps": "tool"})
        monkeypatch.setattr(build, "_output_of", output)

        assert build.deb_dependencies(staging, install_dir) == "libc6 (>= 2.35), libgl1"
        examined = [item[2:] for item in asked[0] if item.startswith("-e")]
        assert examined == [str(install_dir / "wiretrace"), str(install_dir / build._X11_PLUGIN)]
        # Nothing of the tool's working files is left in the package.
        assert not (staging / "debian").exists()

    def test_a_package_tool_that_names_nothing_stops_the_build(self, tmp_path, monkeypatch):
        staging = tmp_path / "deb"
        install_dir = staging / "opt" / "wiretrace"
        install_dir.mkdir(parents=True)
        monkeypatch.setattr(build, "packaging_tools", lambda system: {"dpkg-shlibdeps": "tool"})
        monkeypatch.setattr(build, "_output_of", lambda command, **options: "shlibs:Depends=\n")
        with pytest.raises(build.BuildError):
            build.deb_dependencies(staging, install_dir)

    def test_every_line_of_the_description_is_indented(self):
        description = build.deb_control(1, "libc6").split("Description:", 1)[1].splitlines()[1:]
        assert description
        assert all(line.startswith(" ") for line in description)
