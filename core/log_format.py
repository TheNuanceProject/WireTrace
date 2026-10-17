# SPDX-License-Identifier: GPL-3.0-or-later
"""The on-disk format of WireTrace log and export files.

One place defines how a record becomes a line of a ``.txt`` file and
what the header of each file says, so a live log and an export of the
same session are written identically.

This module does not touch the GUI, the disk, the serial port or any other I/O.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass
from datetime import datetime

from app.constants import DEFAULT_LOG_LINE_ENDING, LOG_TIMESTAMP_FORMAT
from core.records import LineRecord, format_timestamp
from version import APP_NAME, APP_VERSION

FILE_ENCODING = "utf-8"
DEFAULT_FILE_LINE_ENDING = DEFAULT_LOG_LINE_ENDING.value

_HEADER_RULE = "=" * 80
_NOT_AVAILABLE = "N/A"
_HEADER_LABEL_WIDTH = 14
_CSV_COMMENT_REPLACEMENTS = str.maketrans({",": ";", '"': "'", "\r": " ", "\n": " "})


@dataclass(frozen=True, slots=True)
class FileHeaderInfo:
    """What the header of a log or export file reports.

    Attributes:
        title: What the file is ("Log Session" or "Exported Data").
        kind: One word naming the name field ("Session" or "Export").
        name: The name the user gave.
        time_label: Label of the time field ("Started", "Exported").
        time: When the log started or the export was made.
        port_name: Serial port of the session, empty if none.
        baud_rate: Baud rate of the session, 0 if none.
        description: Free text from the user, empty if none.
        note: An extra remark printed in the header, empty if none.
    """

    title: str
    kind: str
    name: str
    time_label: str
    time: datetime
    port_name: str = ""
    baud_rate: int = 0
    description: str = ""
    note: str = ""


def encode_text_line(record: LineRecord, line_ending: str = DEFAULT_FILE_LINE_ENDING) -> bytes:
    """Encode one record as a line of a ``.txt`` log: ``[timestamp] text``."""
    line = f"[{format_timestamp(record.timestamp)}] {record.text}{line_ending}"
    return line.encode(FILE_ENCODING, errors="backslashreplace")


def encode_csv_line(row: str, line_ending: str = DEFAULT_FILE_LINE_ENDING) -> bytes:
    """Encode one CSV row or header line."""
    return f"{row}{line_ending}".encode(FILE_ENCODING, errors="backslashreplace")


def _encode_block(lines: list[str], line_ending: str) -> bytes:
    """Encode several lines, each closed by the line ending."""
    text = line_ending.join(lines) + line_ending
    return text.encode(FILE_ENCODING, errors="backslashreplace")


def text_header(
    info: FileHeaderInfo, part_number: int, line_ending: str = DEFAULT_FILE_LINE_ENDING,
) -> bytes:
    """Build the header block of a ``.txt`` file part."""
    lines = [
        _HEADER_RULE,
        f"{APP_NAME} v{APP_VERSION} - {info.title}",
        _HEADER_RULE,
        f"{info.kind + ' Name':<{_HEADER_LABEL_WIDTH}}: {info.name}",
        f"{'Port':<{_HEADER_LABEL_WIDTH}}: {info.port_name or _NOT_AVAILABLE}",
        f"{'Baud Rate':<{_HEADER_LABEL_WIDTH}}: {info.baud_rate or _NOT_AVAILABLE}",
        f"{info.time_label:<{_HEADER_LABEL_WIDTH}}: {info.time.strftime(LOG_TIMESTAMP_FORMAT)}",
        f"{'Platform':<{_HEADER_LABEL_WIDTH}}: {platform.system()} {platform.release()}",
    ]
    if info.description:
        lines.append(f"{'Description':<{_HEADER_LABEL_WIDTH}}: {info.description}")
    if part_number > 1:
        lines.append(f"{'File':<{_HEADER_LABEL_WIDTH}}: {part_number}")
    if info.note:
        lines.append(f"{'Note':<{_HEADER_LABEL_WIDTH}}: {info.note}")
    lines.extend([_HEADER_RULE, ""])
    return _encode_block(lines, line_ending)


def _one_csv_cell(text: str) -> str:
    """``text`` for a comment line of a ``.csv`` file.

    A spreadsheet reads a comment line like any other, so a comma in a
    session or port name would start a new cell, and a cell may be read
    as a formula. Commas, quotes and line breaks are replaced, which
    keeps the whole comment in its one cell behind the ``#``.
    """
    return text.translate(_CSV_COMMENT_REPLACEMENTS)


def csv_header(
    info: FileHeaderInfo,
    part_number: int,
    column_header: str,
    line_ending: str = DEFAULT_FILE_LINE_ENDING,
) -> bytes:
    """Build the comment lines and column header of a ``.csv`` file part."""
    part = f" | File: {part_number}" if part_number > 1 else ""
    lines = [
        f"# {APP_NAME} v{APP_VERSION} - {info.title}",
        f"# {info.kind}: {_one_csv_cell(info.name)}"
        f" | Port: {_one_csv_cell(info.port_name) or _NOT_AVAILABLE}"
        f" | Baud: {info.baud_rate or _NOT_AVAILABLE}{part}",
        f"# {info.time_label}: {info.time.strftime(LOG_TIMESTAMP_FORMAT)}",
    ]
    if info.note:
        lines.append(f"# Note: {_one_csv_cell(info.note)}")
    lines.append(column_header)
    return _encode_block(lines, line_ending)
