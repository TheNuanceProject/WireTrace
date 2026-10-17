# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for core.log_format: what a name or a note may do to a CSV header."""

from __future__ import annotations

import csv
import io
from datetime import datetime

import pytest

from core.log_format import FILE_ENCODING, FileHeaderInfo, csv_header

_STARTED = datetime(2026, 1, 2, 3, 4, 5)
_COLUMNS = "Timestamp,Data"


def _header_rows(**fields: str) -> list[list[str]]:
    info = FileHeaderInfo(
        title="Log Session", kind="Session", name=fields.get("name", "Bench"),
        time_label="Started", time=_STARTED,
        port_name=fields.get("port_name", "COM7"), baud_rate=115200,
        note=fields.get("note", ""),
    )
    text = csv_header(info, 1, _COLUMNS).decode(FILE_ENCODING)
    return list(csv.reader(io.StringIO(text)))


@pytest.mark.parametrize("field", ["name", "port_name", "note"])
@pytest.mark.parametrize(
    "value",
    [
        'Bench,=HYPERLINK("http://example.invalid")',
        "Bench\n=1+1",
        "Bench\r\n@SUM(A1)",
        'Bench","+cmd',
    ],
)
def test_a_comment_line_stays_one_cell_behind_its_mark(field: str, value: str) -> None:
    rows = _header_rows(**{field: value})

    comments, columns = rows[:-1], rows[-1]
    assert columns == _COLUMNS.split(",")
    for row in comments:
        assert len(row) == 1
        assert row[0].startswith("# ")


def test_the_text_of_a_name_is_still_readable() -> None:
    rows = _header_rows(name="Motor, left")

    assert any("Motor; left" in row[0] for row in rows[:-1])
