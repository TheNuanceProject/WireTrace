# SPDX-License-Identifier: GPL-3.0-or-later
"""Rules for the text of the source tree itself."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.add_spdx_headers import is_excluded

ROOT = Path(__file__).resolve().parent.parent

#: Directories and files that make up the program, its tests and its build.
_SOURCE_DIRS = (
    "app", "core", "ui", "updater", "tests", "tests_gui", "tools", "build", ".github",
    "resources/help",
)
_SOURCE_FILES = (
    "main.py", "version.py", "build.py", "pyproject.toml",
    "README.md", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md", "BUILD_GUIDE.md",
    "LICENSE", "NOTICE",
)
_TEXT_SUFFIXES = frozenset({
    ".py", ".template", ".toml", ".yml", ".yaml", ".cfg", ".iss", ".desktop", ".plist", ".txt",
    ".html",
})
#: The mark itself and the ways of writing it that produce it on screen.
#: Each is put together here so that this file does not contain one.
_CODE_POINT = 0x2014
_EM_DASH_FORMS = (
    chr(_CODE_POINT),
    "\\u" + f"{_CODE_POINT:04x}",
    "&" + "mdash;",
    "&#" + f"{_CODE_POINT};",
    "&#x" + f"{_CODE_POINT:04x};",
)


def _source_files() -> list[Path]:
    files = [ROOT / name for name in _SOURCE_FILES if (ROOT / name).is_file()]
    for directory in _SOURCE_DIRS:
        base = ROOT / directory
        if base.is_dir():
            files.extend(
                path for path in sorted(base.rglob("*"), key=lambda p: p.as_posix())
                if path.is_file()
                and path.suffix in _TEXT_SUFFIXES
                # What a build or a local environment writes is not source.
                and not is_excluded(path, ROOT)
            )
    return files


def test_the_source_tree_is_found() -> None:
    names = {path.relative_to(ROOT).as_posix() for path in _source_files()}

    assert {"main.py", "version.py", "ui/main_window.py", "core/log_writer.py"} <= names


@pytest.mark.parametrize(
    "path", _source_files(), ids=lambda path: path.relative_to(ROOT).as_posix(),
)
def test_no_em_dash_in_source_text(path: Path) -> None:
    """The interface, the logs and the files WireTrace writes use plain
    punctuation. Keeping the mark out of the source keeps it out of them."""
    found = [
        f"{number}: {line.strip()}"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if any(form in line.lower() for form in _EM_DASH_FORMS)
    ]
    assert not found, "\n".join(found)


#: Files of the interface that make a label of their own, and how many.
#: ``plain_label`` is where every other label is made; the dialog that
#: describes a plot format shows markup it writes and escapes itself.
_LABELS_MADE_DIRECTLY = {
    "ui/widgets/styling.py": 1,
    "ui/dialogs/plot_config_dialog.py": 3,
}
_INTERFACE_DIRS = ("ui", "updater")


def test_every_label_shows_its_text_as_written() -> None:
    """Qt reads a label's text as markup when it looks like markup. Text
    from a device, a file name or a server must never be read that way,
    so labels are made by ``plain_label``."""
    made_directly: dict[str, int] = {}
    for directory in _INTERFACE_DIRS:
        for path in sorted((ROOT / directory).rglob("*.py")):
            count = path.read_text(encoding="utf-8").count("QLabel(")
            if count:
                made_directly[path.relative_to(ROOT).as_posix()] = count

    assert made_directly == _LABELS_MADE_DIRECTLY


#: Characters that change the order text is shown in, or break a line
#: without being a line break. In source they can make code read
#: differently from what it does.
_HIDDEN_CODE_POINTS = frozenset({0x2028, 0x2029, *range(0x202A, 0x202F), *range(0x2066, 0x206A)})


@pytest.mark.parametrize(
    "path", _source_files(), ids=lambda path: path.relative_to(ROOT).as_posix(),
)
def test_no_hidden_direction_or_line_characters_in_source_text(path: Path) -> None:
    found = [
        f"{number}: U+{ord(character):04X}"
        for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1)
        for character in line
        if ord(character) in _HIDDEN_CODE_POINTS
    ]
    assert not found, "\n".join(found)


def test_the_documents_state_the_update_check_interval_of_the_code() -> None:
    from app.constants import UPDATE_CHECK_INTERVAL_HOURS

    stated = f"every {UPDATE_CHECK_INTERVAL_HOURS} hours"
    for name in ("README.md", "CHANGELOG.md"):
        assert stated in (ROOT / name).read_text(encoding="utf-8"), name
