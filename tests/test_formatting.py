# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for ui.formatting: the short texts made from numbers."""

from __future__ import annotations

import pytest

from ui.formatting import (
    describe_files,
    format_change,
    format_count,
    format_duration,
    format_size,
    format_value,
)


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "0 bytes"),
        (1, "1 bytes"),
        (1023, "1023 bytes"),
        (1024, "1 KB"),
        (1536, "1.5 KB"),
        (41 * 1024, "41 KB"),
        (10 * 1024 * 1024, "10 MB"),
        (int(2.25 * 1024**3), "2.2 GB"),
        (5 * 1024**4, "5 TB"),
        (5000 * 1024**4, "5000 TB"),
    ],
)
def test_size_uses_the_largest_unit_that_fits(size: int, expected: str) -> None:
    assert format_size(size) == expected


def test_negative_size_is_rejected() -> None:
    with pytest.raises(ValueError):
        format_size(-1)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "00:00:00"),
        (59.9, "00:00:59"),
        (60, "00:01:00"),
        (3661, "01:01:01"),
        (100 * 3600 + 5, "100:00:05"),
        (-3, "00:00:00"),
    ],
)
def test_duration_is_hours_minutes_seconds(seconds: float, expected: str) -> None:
    assert format_duration(seconds) == expected


@pytest.mark.parametrize(
    ("count", "expected"),
    [(0, "0 lines"), (1, "1 line"), (2, "2 lines"), (84_210, "84,210 lines")],
)
def test_count_agrees_with_its_noun(count: int, expected: str) -> None:
    assert format_count(count, "line") == expected


def test_count_takes_an_irregular_plural() -> None:
    assert format_count(1, "device is", "devices are") == "1 device is"
    assert format_count(3, "device is", "devices are") == "3 devices are"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, "0"),
        (-0.0, "0"),
        (0.00001, "1e-05"),
        (0.0034, "0.0034"),
        (4.02, "4.02"),
        (12.26, "12.26"),
        (43.5, "43.5"),
        (999.96, "1000"),
        (1451.0, "1,451"),
        (-1446.4, "-1,446"),
        (123456.7, "123,457"),
        (2.5e9, "2.500e+09"),
    ],
)
def test_value_is_shown_in_about_four_significant_digits(value: float, expected: str) -> None:
    assert format_value(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1.66, "+1.66"), (-41.0, "-41"), (0.0, "0"), (0.000001, "+1e-06"), (-8.6, "-8.6")],
)
def test_change_carries_its_sign(value: float, expected: str) -> None:
    assert format_change(value) == expected


_FOLDER = "/logs"


def _parts(name: str, extension: str, count: int) -> list[str]:
    """The paths of a file split into ``count`` parts."""
    first = f"{_FOLDER}/{name}.{extension}"
    return [first, *(f"{_FOLDER}/{name}_{part:03d}.{extension}" for part in range(2, count + 1))]


@pytest.mark.parametrize(
    ("text", "csv", "expected"),
    [
        (_parts("Export", "txt", 1), [], "Export.txt"),
        ([], _parts("Export", "csv", 1), "Export.csv"),
        (_parts("Export", "txt", 1), _parts("Export", "csv", 1), "Export, in 1 text file and 1 CSV file"),
        (_parts("Export", "txt", 27), _parts("Export", "csv", 9), "Export, in 27 text files and 9 CSV files"),
        (_parts("Export", "txt", 3), [], "Export, in 3 text files"),
        ([], [], ""),
    ],
)
def test_files_written_are_described_in_a_few_words(
    text: list[str], csv: list[str], expected: str,
) -> None:
    assert describe_files(text, csv) == expected
