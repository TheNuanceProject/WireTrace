# SPDX-License-Identifier: GPL-3.0-or-later
"""The samples of one value that a chart needs to draw its line.

A chart a few hundred pixels wide cannot show more than its pixel
columns. Handing it every sample makes it draw thousands of segments
that land in the same columns. ``points_to_draw`` keeps, for each
column, the first and the last sample and the one with the least and
the greatest value, at their own times: the M4 method (Jugel and
others, "M4: A Visualization-Oriented Time Series Data Aggregation",
VLDB 2014). Each column keeps its whole height, so no spike can vanish,
and the line enters and leaves each column where it did.

Samples outside the time given are dropped, except the first and the
last of each run of them on one side, so every segment that crosses
into that time is drawn as it was. A sample whose time or value is not
a finite number breaks the line (as pyqtgraph's ``connect="finite"``
draws it). A finite sample between two such samples joins nothing, so it
draws nothing either: it counts as part of the break. Of each run of
breaks, one sample that is not finite is kept, which breaks the line as
the whole run would, with the samples on either side. The samples need
not be in time order.

This module does not touch the GUI, the disk, the serial port or any other I/O.
"""

from __future__ import annotations

import numpy as np

#: How many samples each column keeps at most: first, least, greatest, last.
SAMPLES_PER_COLUMN = 4

_LEFT = -1
_RIGHT = -2
_BROKEN = -3


def points_to_draw(
    x: np.ndarray, y: np.ndarray, low: float, high: float, columns: int,
) -> tuple[np.ndarray, np.ndarray]:
    """The samples of ``x`` and ``y`` that draw the same line in ``columns``
    equal columns of time from ``low`` to ``high``.

    Returns ``x`` and ``y`` themselves when there are no more samples
    than the columns can keep, or no columns.
    """
    count = x.size
    if columns < 1 or not high > low or count <= SAMPLES_PER_COLUMN * columns:
        return x, y
    finite = np.isfinite(x) & np.isfinite(y)
    # A finite sample with no finite neighbour on either side is drawn as
    # nothing, so it belongs to the break around it.
    joined = np.zeros(count, dtype=bool)
    joined[1:] |= finite[:-1]
    joined[:-1] |= finite[1:]
    drawn = finite & joined
    inside = drawn & (x >= low) & (x <= high)
    # Each sample's column, or the side of the time it lies on, or that it
    # breaks the line.
    key = np.where(x < low, _LEFT, _RIGHT)
    key[~drawn] = _BROKEN
    key[inside] = np.minimum(
        ((x[inside] - low) * (columns / (high - low))).astype(np.int64), columns - 1,
    )
    # Runs of neighbouring samples in one column, on one side, or breaking
    # the line.
    change = np.empty(count, dtype=bool)
    change[0] = True
    change[1:] = key[1:] != key[:-1]
    starts = np.flatnonzero(change)
    ends = np.append(starts[1:], count) - 1
    run = np.cumsum(change) - 1
    lines = key[starts] != _BROKEN
    keep = [starts[lines], ends[lines], _first_in_run(np.flatnonzero(~finite), run)]
    if inside.any():
        keep.append(_first_extreme(y, run, inside, np.minimum.reduceat(y, starts)))
        keep.append(_first_extreme(y, run, inside, np.maximum.reduceat(y, starts)))
    chosen = np.unique(np.concatenate(keep))
    return x[chosen], y[chosen]


def _first_in_run(found: np.ndarray, run: np.ndarray) -> np.ndarray:
    """Of the samples ``found``, in order, the first of each run."""
    first = np.ones(found.size, dtype=bool)
    first[1:] = run[found[1:]] != run[found[:-1]]
    return found[first]


def _first_extreme(
    y: np.ndarray, run: np.ndarray, inside: np.ndarray, extremes: np.ndarray,
) -> np.ndarray:
    """In each run in a column, the first sample whose value is the run's
    extreme (``extremes`` holds one per run)."""
    return _first_in_run(np.flatnonzero(inside & (y == extremes[run])), run)
