# SPDX-License-Identifier: GPL-3.0-or-later
"""The least and greatest samples of one value over a span of time.

The plot keeps the newest samples of each value in memory. For older
time it keeps an overview: time cut into equal slices, and for each
slice the sample with the least value and the sample with the greatest,
each at the time it was received, with the count and the sum of the
samples in the slice.

When the samples outgrow the slices, each pair of neighbours is joined
and every slice covers twice the time. Memory stays fixed however long
the session runs, and no spike can vanish when zoomed out: the extreme
samples of every slice are kept exactly as received.

Samples whose time or value is not a finite number are not counted.
``figures`` gives the count, least, greatest and sum of the slices that
touch a time range: exactly those of the range when it starts and ends
on slice boundaries.

This module does not touch the GUI, the disk, the serial port or any other I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.constants import PLOT_OVERVIEW_FIRST_SLICE_SECONDS, PLOT_OVERVIEW_SLICES


@dataclass(frozen=True, slots=True)
class OverviewFigures:
    """The figures of the samples in the slices that touch a time range."""

    count: int
    minimum: float
    maximum: float
    total: float


class Overview:
    """Least and greatest samples per time slice, in fixed memory.

    Times are seconds from the overview's start; earlier samples are not
    counted.
    """

    def __init__(
        self,
        slices: int = PLOT_OVERVIEW_SLICES,
        first_slice_seconds: float = PLOT_OVERVIEW_FIRST_SLICE_SECONDS,
    ) -> None:
        if slices < 2 or slices % 2:
            raise ValueError("an overview needs an even number of slices, at least two")
        if not first_slice_seconds > 0:
            raise ValueError("a slice must cover some time")
        self._slices = slices
        self._first_width = first_slice_seconds
        self._width = first_slice_seconds
        self._min_x = np.zeros(slices)
        self._min_y = np.full(slices, np.inf)
        self._max_x = np.zeros(slices)
        self._max_y = np.full(slices, -np.inf)
        self._count = np.zeros(slices, dtype=np.int64)
        self._total = np.zeros(slices)
        self._used = 0

    @property
    def slice_seconds(self) -> float:
        """The time each slice covers now."""
        return self._width

    @property
    def is_empty(self) -> bool:
        return self._used == 0

    def clear(self) -> None:
        self._width = self._first_width
        self._min_y.fill(np.inf)
        self._max_y.fill(-np.inf)
        self._count.fill(0)
        self._total.fill(0.0)
        self._used = 0

    def add(self, x: np.ndarray, y: np.ndarray) -> None:
        """Count samples received at times ``x`` with values ``y``.

        The samples may come in any order; of samples with equal values
        in one slice, the one received first is kept.
        """
        finite = np.isfinite(x) & np.isfinite(y) & (x >= 0)
        x, y = x[finite], y[finite]
        if x.size == 0:
            return
        while int(x.max() // self._width) >= self._slices:
            self._halve()
        slices = (x // self._width).astype(np.int64)
        # Sorted by slice, then by value, then by time: the first of each
        # slice is its least (or greatest) value, received first.
        by_least = np.lexsort((x, y, slices))
        by_greatest = np.lexsort((x, -y, slices))
        sorted_slices = slices[by_least]
        starts = np.flatnonzero(np.r_[True, sorted_slices[1:] != sorted_slices[:-1]])
        touched = sorted_slices[starts]

        lowest = by_least[starts]
        highest = by_greatest[starts]
        lower = y[lowest] < self._min_y[touched]
        self._min_x[touched[lower]] = x[lowest[lower]]
        self._min_y[touched[lower]] = y[lowest[lower]]
        higher = y[highest] > self._max_y[touched]
        self._max_x[touched[higher]] = x[highest[higher]]
        self._max_y[touched[higher]] = y[highest[higher]]
        self._count[touched] += np.diff(np.r_[starts, x.size])
        self._total[touched] += np.add.reduceat(y[by_least], starts)
        self._used = max(self._used, int(touched[-1]) + 1)

    def samples(self, low: float, high: float) -> tuple[np.ndarray, np.ndarray]:
        """The least and greatest sample of each slice touching ``[low, high]``, in time order."""
        first, end = self._touching(low, high)
        held = np.flatnonzero(self._count[first:end]) + first
        if held.size == 0:
            return np.empty(0), np.empty(0)
        least_first = self._min_x[held] <= self._max_x[held]
        first_x = np.where(least_first, self._min_x[held], self._max_x[held])
        first_y = np.where(least_first, self._min_y[held], self._max_y[held])
        second_x = np.where(least_first, self._max_x[held], self._min_x[held])
        second_y = np.where(least_first, self._max_y[held], self._min_y[held])
        x = np.column_stack((first_x, second_x)).ravel()
        y = np.column_stack((first_y, second_y)).ravel()
        # A slice whose least and greatest are one sample gives it once.
        keep = np.ones(x.size, dtype=bool)
        keep[1::2] = (second_x != first_x) | (second_y != first_y)
        return x[keep], y[keep]

    def figures(self, low: float, high: float) -> OverviewFigures | None:
        """Count, least, greatest and sum of the slices touching ``[low, high]``."""
        first, end = self._touching(low, high)
        held = self._count[first:end] > 0
        if not held.any():
            return None
        return OverviewFigures(
            count=int(self._count[first:end].sum()),
            minimum=float(self._min_y[first:end][held].min()),
            maximum=float(self._max_y[first:end][held].max()),
            total=float(self._total[first:end].sum()),
        )

    # ── Internal ─────────────────────────────────────────────────────────

    def _touching(self, low: float, high: float) -> tuple[int, int]:
        """The slices from the one holding ``low`` to the one holding ``high``."""
        held = self._used * self._width
        first = int(min(max(low, 0.0), held) // self._width)
        end = self._used if high >= held else int(max(high, 0.0) // self._width) + 1
        return first, max(first, end)

    def _halve(self) -> None:
        """Join each pair of neighbouring slices; each then covers twice the time."""
        half = self._slices // 2
        earlier, later = slice(0, None, 2), slice(1, None, 2)
        # The earlier slice of each pair wins a tie: its sample came first.
        lower = self._min_y[later] < self._min_y[earlier]
        higher = self._max_y[later] > self._max_y[earlier]
        min_x = np.where(lower, self._min_x[later], self._min_x[earlier])
        min_y = np.where(lower, self._min_y[later], self._min_y[earlier])
        max_x = np.where(higher, self._max_x[later], self._max_x[earlier])
        max_y = np.where(higher, self._max_y[later], self._max_y[earlier])
        count = self._count[earlier] + self._count[later]
        total = self._total[earlier] + self._total[later]

        self._min_x[:half], self._min_y[:half] = min_x, min_y
        self._max_x[:half], self._max_y[:half] = max_x, max_y
        self._count[:half], self._total[:half] = count, total
        self._min_y[half:] = np.inf
        self._max_y[half:] = -np.inf
        self._count[half:] = 0
        self._total[half:] = 0.0
        self._width *= 2
        self._used = (self._used + 1) // 2
