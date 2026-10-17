# SPDX-License-Identifier: GPL-3.0-or-later
"""Turning numbers into the short text the interface shows."""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import datetime

_BYTES_PER_UNIT = 1024
_SIZE_UNITS = ("bytes", "KB", "MB", "GB", "TB")
_SECONDS_PER_MINUTE = 60
_MICROSECONDS_PER_MILLISECOND = 1000
_MINUTES_PER_HOUR = 60


def format_size(size: int) -> str:
    """A file size in the largest unit that keeps it at 1 or above.

    Whole numbers below 10 of a unit get one decimal, so "1.5 MB" and
    "41 KB" are both shown with about two digits.
    """
    if size < 0:
        raise ValueError(f"a size cannot be negative: {size}")
    value = float(size)
    unit = _SIZE_UNITS[0]
    for unit in _SIZE_UNITS:
        if value < _BYTES_PER_UNIT or unit == _SIZE_UNITS[-1]:
            break
        value /= _BYTES_PER_UNIT
    if unit == _SIZE_UNITS[0]:
        return f"{int(value)} {unit}"
    if value < 10 and not value.is_integer():
        return f"{value:.1f} {unit}"
    return f"{round(value)} {unit}"


def format_duration(seconds: float) -> str:
    """Elapsed time as ``HH:MM:SS``. Hours keep counting past 99."""
    whole = max(0, int(seconds))
    minutes, secs = divmod(whole, _SECONDS_PER_MINUTE)
    hours, minutes = divmod(minutes, _MINUTES_PER_HOUR)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


_GROUPED_FROM = 1_000
_EXPONENT_FROM = 1e9
_SIGNIFICANT_DIGITS = 4


def format_value(value: float) -> str:
    """A measured value in about four significant digits.

    "12.26", "0.0034", "1,451", "2.5e+09". Large whole-sounding values
    are grouped; only very large ones fall back to an exponent.
    """
    size = abs(value)
    if size >= _EXPONENT_FROM:
        return f"{value:.{_SIGNIFICANT_DIGITS - 1}e}"
    if size >= _GROUPED_FROM:
        return f"{value:,.0f}"
    text = f"{value:.{_SIGNIFICANT_DIGITS}g}"
    return "0" if text in ("-0", "0") else text


def format_received(value: float) -> str:
    """A value exactly as received: "1,682", "12.97", "-0.0034".

    Whole numbers are grouped like ``format_value``'s; any other number
    is written in its shortest exact form, so no digit is added or lost.
    """
    if value.is_integer() and abs(value) < _EXPONENT_FROM:
        return f"{int(value):,}"
    return repr(value)


def format_time_of_day(moment: datetime) -> str:
    """A moment as the console shows it: "19:43:44.132"."""
    return (
        f"{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}"
        f".{moment.microsecond // _MICROSECONDS_PER_MILLISECOND:03d}"
    )


def format_change(value: float) -> str:
    """A difference with its sign: "+1.66", "-41", "0"."""
    text = format_value(value)
    return f"+{text}" if value > 0 and text != "0" else text


def format_count(count: int, singular: str, plural: str | None = None) -> str:
    """A count with its noun: "1 line", "2,480 lines"."""
    noun = singular if count == 1 else (plural or f"{singular}s")
    return f"{count:,} {noun}"


def describe_files(text_paths: Sequence[str], csv_paths: Sequence[str]) -> str:
    """The files a log or an export wrote, in a few words.

    One file is named. Several are named by the first, whose name the
    others share with a part number added, and counted by kind:
    "Export_COM15_2026-10-17_16-00-34, in 27 text files and 9 CSV files".
    """
    paths = (*text_paths, *csv_paths)
    if not paths:
        return ""
    if len(paths) == 1:
        return os.path.basename(paths[0])
    kinds = ((text_paths, "text file"), (csv_paths, "CSV file"))
    counts = " and ".join(format_count(len(group), kind) for group, kind in kinds if group)
    name = os.path.splitext(os.path.basename(paths[0]))[0]
    return f"{name}, in {counts}"
