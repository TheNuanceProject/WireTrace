# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared rules for detecting named columns in a sample of lines.

The CSV export and the live plot both look at a sample of lines, pull
the keys out of each one (JSON object keys, or ``key: value`` /
``key=value`` pairs), and decide which keys are stable enough to be
columns. The format-specific extraction differs between them; the
decision rule is the same and lives here, so both always agree.

This module does not touch the GUI, the disk, the serial port or any other I/O.
"""

from __future__ import annotations

from collections import Counter

from app.constants import (
    STRUCTURE_KEY_FREQUENCY_THRESHOLD,
    STRUCTURE_MIN_MATCHING_FRACTION,
)


def enough_lines_match(matching: int, sample_size: int) -> bool:
    """True if enough of the sample matched a format to consider it."""
    return matching > 0 and matching >= sample_size * STRUCTURE_MIN_MATCHING_FRACTION


def select_common_keys(
    per_line_keys: list[list[str]], sample_size: int,
) -> list[str] | None:
    """Choose the column names from the keys found on each matching line.

    Args:
        per_line_keys: For every sample line that matched the format,
            its keys in the order they appeared.
        sample_size: Number of lines in the whole sample, matching or not.

    Returns:
        The keys that appear on enough of the matching lines, in the
        order the device emits them, or ``None`` if too few lines
        matched or no key is common enough.

    A strict intersection would be brittle: real streams mix boot
    banners with readings, and one stray line would remove every
    column. A frequency threshold keeps stable readings with
    occasional dropouts while excluding keys that appear only rarely.
    """
    if not enough_lines_match(len(per_line_keys), sample_size):
        return None

    frequency: Counter[str] = Counter()
    for keys in per_line_keys:
        frequency.update(set(keys))
    threshold = len(per_line_keys) * STRUCTURE_KEY_FREQUENCY_THRESHOLD
    common = {key for key, count in frequency.items() if count >= threshold}
    if not common:
        return None

    # Prefer the order of the first line that carries every common key,
    # so the columns follow the device's own emission order.
    for keys in per_line_keys:
        if all(key in keys for key in common):
            return _unique(key for key in keys if key in common)

    ordered = _unique(key for key in per_line_keys[0] if key in common)
    ordered.extend(key for key in sorted(common) if key not in ordered)
    return ordered


def _unique(keys) -> list[str]:
    """Return ``keys`` in order, each one once. A line that repeats a
    key supplies one column, not two with the same name."""
    return list(dict.fromkeys(keys))
