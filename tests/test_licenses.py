# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests that the licence texts shipped with a build are complete and
match what a release build contains."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import app.licenses as licenses
from app.licenses import LicensesNotFoundError, load_third_party_licenses
from tools.collect_licenses import COMPONENTS, OUTPUT, REQUIREMENTS, normalise, required_names

ROOT = Path(__file__).resolve().parent.parent
LOCK_FILE = ROOT / "requirements-release.txt"
BUILD_REQUIREMENTS = ROOT / "requirements-build.txt"

_PINNED = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)", re.MULTILINE)
_SECTION = re.compile(r"^={78}\n(.+)\n={78}$", re.MULTILINE)


def _locked() -> dict[str, str]:
    """Every package of the release lock file, with its exact version."""
    return {
        normalise(name): version
        for name, version in _PINNED.findall(LOCK_FILE.read_text(encoding="utf-8"))
    }


def _sections() -> dict[str, str]:
    """``name -> version`` of each component the licence file lists."""
    found = {}
    for heading in _SECTION.findall(OUTPUT.read_text(encoding="utf-8")):
        name, _, version = heading.rpartition(" ")
        found[normalise(name or version)] = version if name else ""
    return found


def test_the_licence_file_is_found_and_read() -> None:
    text = load_third_party_licenses()

    assert text.startswith("Third-party licences")
    assert "GNU LESSER GENERAL PUBLIC LICENSE" in text


def test_a_missing_licence_file_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(licenses, "LICENSES_FILE", tmp_path / "missing.txt")
    with pytest.raises(LicensesNotFoundError):
        load_third_party_licenses()

    empty = tmp_path / "empty.txt"
    empty.write_text("\n", encoding="utf-8")
    monkeypatch.setattr(licenses, "LICENSES_FILE", empty)
    with pytest.raises(LicensesNotFoundError):
        load_third_party_licenses()


def test_every_required_package_is_locked_and_listed_at_the_locked_version() -> None:
    locked = _locked()
    listed = _sections()

    for name in required_names(REQUIREMENTS):
        assert name in locked, f"{name} is not in {LOCK_FILE.name}"
        assert listed.get(name) == locked[name], (
            f"{name}: licence file says {listed.get(name)}, lock file says {locked[name]}. "
            "Run tools/collect_licenses.py in an environment installed from the lock file."
        )


def test_every_locked_package_that_ships_in_a_build_is_listed() -> None:
    """The lock file holds the packages of a build and the build tools.
    Everything in it that is not a build tool must have its licence listed."""
    build_tools = set(required_names(BUILD_REQUIREMENTS))
    listed = _sections()

    for name, version in _locked().items():
        if name in build_tools:
            continue
        assert listed.get(name) == version, name


def test_the_font_and_the_compiler_runtime_are_listed() -> None:
    components = json.loads(COMPONENTS.read_text(encoding="utf-8"))
    text = OUTPUT.read_text(encoding="utf-8")

    assert f"\n{components['build_runtime']['name']} " in text
    for entry in components["files"]:
        assert f"\n{entry['name']}\n" in text
        assert (ROOT / entry["path"]).is_file()


def test_qt_is_listed_with_its_licence_and_where_its_source_is() -> None:
    text = OUTPUT.read_text(encoding="utf-8")
    section = text[text.index("\nPySide6 "):]

    assert "Licence: LGPL-3.0-only" in section[:400]
    assert "Source:  https://" in section[:400]
    assert "can be replaced" in section[:1200]


def test_the_bill_of_materials_lists_the_interpreter_and_each_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools import collect_licenses
    from version import APP_LICENSE, APP_NAME, APP_VERSION

    listed = [
        collect_licenses.Component(
            name="Example", version="1.2.3", license="MIT", url="https://example.org",
            note="", texts=(("LICENSE", "text"),), package="example",
        ),
        collect_licenses.Component(
            name="A font", version="", license="OFL-1.1", url="", note="",
            texts=(("OFL.txt", "text"),),
        ),
    ]
    monkeypatch.setattr(collect_licenses, "components", lambda: listed)
    monkeypatch.setattr(collect_licenses.platform, "python_version", lambda: "3.12.9")
    document = collect_licenses.bill_of_materials()

    assert document["bomFormat"] == "CycloneDX"
    assert document["metadata"]["component"] == {
        "type": "application", "name": APP_NAME, "version": APP_VERSION,
        "licenses": [{"license": {"id": APP_LICENSE}}],
    }
    interpreter, example, font = document["components"]
    assert (interpreter["name"], interpreter["version"]) == ("Python", "3.12.9")
    assert "purl" not in interpreter
    assert example == {
        "type": "library", "name": "Example", "version": "1.2.3",
        "purl": "pkg:pypi/example@1.2.3",
        "licenses": [{"license": {"name": "MIT"}}],
        "externalReferences": [{"type": "website", "url": "https://example.org"}],
    }
    assert "version" not in font


def test_the_licence_file_is_compared_with_what_is_installed_by_name_and_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools import collect_licenses

    rule = collect_licenses.RULE
    text = (
        f"{rule}\nExample 1.2.3\n{rule}\nLicence: MIT\n\n"
        f"{rule}\nA Font Name\n{rule}\n\n"
        f"{rule}\nGone 2.0\n{rule}\n"
    )
    assert collect_licenses.listed_versions(text) == {
        "Example": "1.2.3", "A Font Name": "", "Gone": "2.0",
    }

    monkeypatch.setattr(
        collect_licenses, "installed_versions", lambda: {"Example": "1.2.4", "New": "0.1"},
    )
    assert collect_licenses.differences(text) == [
        "Example: 1.2.3 is listed, 1.2.4 is installed",
        "Gone 2.0 is listed but not installed",
        "New 0.1 is installed but not listed",
    ]

    monkeypatch.setattr(
        collect_licenses, "installed_versions", lambda: {"Example": "1.2.3", "Gone": "2.0"},
    )
    assert collect_licenses.differences(text) == []


def test_the_compiler_runtime_is_listed_at_the_locked_version() -> None:
    from tools.collect_licenses import listed_versions

    components = json.loads(COMPONENTS.read_text(encoding="utf-8"))
    runtime = components["build_runtime"]
    listed = listed_versions(OUTPUT.read_text(encoding="utf-8"))
    assert listed[runtime["name"]] == _locked()[normalise(runtime["package"])]


def test_what_a_build_carries_of_its_system_is_shown_after_the_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = tmp_path / "system"
    system.mkdir()
    (system / "libssl3.txt").write_text("OpenSSL text\n", encoding="utf-8")
    (system / "Python.txt").write_text("\nPython text\n\n", encoding="utf-8")
    (system / "notes.md").write_text("not a licence", encoding="utf-8")
    monkeypatch.setattr(licenses, "SYSTEM_LICENSES_DIR", system)

    text = load_third_party_licenses()

    packages = OUTPUT.read_text(encoding="utf-8")
    assert text.startswith(packages)
    added = text[len(packages):]
    assert added.index("Python (from the system") < added.index("libssl3 (from the system")
    assert "Python text" in added
    assert "OpenSSL text" in added
    assert "not a licence" not in added


def test_running_from_source_there_is_nothing_of_a_build_system_to_show(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(licenses, "SYSTEM_LICENSES_DIR", tmp_path / "absent")
    assert load_third_party_licenses() == OUTPUT.read_text(encoding="utf-8")
