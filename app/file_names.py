# SPDX-License-Identifier: GPL-3.0-or-later
"""How the files WireTrace writes are named.

A log or export is named ``<name>_<port>_<date>_<time>``. The rule is
written once here, so the name a dialog previews is built the same way
as the name the file gets.

This module does not touch the disk.
"""

from __future__ import annotations

import os
import re
from datetime import datetime

from app.constants import (
    FILENAME_DEFAULT_NAME,
    FILENAME_MAX_LENGTH,
    FILENAME_NO_PORT,
    FILENAME_TIMESTAMP_FORMAT,
)

#: Characters no file name may hold on at least one supported system.
_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_PATH_SEPARATORS = re.compile(r"[/\\]")


def sanitize_file_name(name: str) -> str:
    """Turn what the user typed into a name every file system accepts."""
    cleaned = _FORBIDDEN.sub("", name.strip().replace(" ", "_"))[:FILENAME_MAX_LENGTH]
    # Windows does not allow a name to end in a dot or a space.
    return cleaned.rstrip(". ") or FILENAME_DEFAULT_NAME


def port_label(port_name: str) -> str:
    """A port name as it appears in a file name: ``/dev/ttyUSB0`` has
    path separators, which a file name cannot."""
    return _PATH_SEPARATORS.sub("_", port_name) or FILENAME_NO_PORT


def output_base_name(name: str, port_name: str, moment: datetime) -> str:
    """The file name, without extension, of a log or export."""
    stamp = moment.strftime(FILENAME_TIMESTAMP_FORMAT)
    return f"{sanitize_file_name(name)}_{port_label(port_name)}_{stamp}"


def output_base_path(directory: str, name: str, port_name: str, moment: datetime) -> str:
    """The path, without extension, of a log or export."""
    return os.path.join(directory, output_base_name(name, port_name, moment))
