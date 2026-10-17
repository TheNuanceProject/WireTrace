# SPDX-License-Identifier: GPL-3.0-or-later
"""The licence texts of the third-party work a build contains.

They come from two places. The packages WireTrace is built from are the
same in every build; their texts are collected into one file by
``tools/collect_licenses.py``. A build also carries parts of the system
it was made on: the Python interpreter, and on Linux some libraries of
the operating system. The build writes the text of each of those into a
folder beside that file. Running from source, that folder does not
exist, and nothing of the system is carried.

This module finds and reads both. It has no Qt dependency.
"""

from __future__ import annotations

from pathlib import Path

LICENSES_DIR = Path(__file__).resolve().parent.parent / "resources" / "licenses"
LICENSES_FILE = LICENSES_DIR / "THIRD_PARTY_LICENSES.txt"

#: Written by the build: one text file for each part of the build system
#: that the build carries, named after that part.
SYSTEM_LICENSES_DIR = LICENSES_DIR / "system"
SYSTEM_LICENSE_SUFFIX = ".txt"

#: The line above and below the heading of each component.
SECTION_RULE = "=" * 78
ENCODING = "utf-8"

_SYSTEM_HEADING = "{name} (from the system this build was made on)"


class LicensesNotFoundError(FileNotFoundError):
    """The licence texts are missing from the installation."""


def section_heading(name: str) -> str:
    """The heading of a component, as three lines."""
    return f"{SECTION_RULE}\n{name}\n{SECTION_RULE}"


def _system_sections() -> list[str]:
    try:
        files = sorted(
            SYSTEM_LICENSES_DIR.glob(f"*{SYSTEM_LICENSE_SUFFIX}"), key=lambda path: path.name
        )
    except OSError:
        return []
    sections = []
    for path in files:
        try:
            text = path.read_text(encoding=ENCODING)
        except OSError as exc:
            raise LicensesNotFoundError(f"a licence text could not be read: {path}") from exc
        heading = section_heading(_SYSTEM_HEADING.format(name=path.stem))
        sections.append(f"{heading}\n\n{text.strip()}\n")
    return sections


def load_third_party_licenses() -> str:
    """The licence texts, as one text.

    Raises:
        LicensesNotFoundError: the file is missing or empty.
    """
    try:
        text = LICENSES_FILE.read_text(encoding=ENCODING)
    except OSError as exc:
        raise LicensesNotFoundError(f"the licence texts were not found: {LICENSES_FILE}") from exc
    if not text.strip():
        raise LicensesNotFoundError(f"the licence texts are empty: {LICENSES_FILE}")
    return "\n".join([text, *_system_sections()])
