# SPDX-License-Identifier: GPL-3.0-or-later
"""Numbers the plot shows beside its lines.

Three things, each a plain function of the samples:
  - the last, least, greatest and mean value within the visible time
  - which values are near enough in size to share one panel
  - what was received at a moment: the sample nearest a time, and the
    value each series last received at or before it, for the readout
    under the pointer and the measuring cursors

This module does not touch the GUI, the disk, the serial port or any other I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.constants import PLOT_MAX_PANELS, PLOT_PANEL_SCALE_RATIO


@dataclass(frozen=True, slots=True)
class SeriesStats:
    """One value over the visible time range."""

    last: float
    minimum: float
    maximum: float
    mean: float


def visible_stats(
    x: np.ndarray, y: np.ndarray, x_min: float, x_max: float,
) -> SeriesStats | None:
    """Statistics of the samples with ``x_min <= x <= x_max``.

    ``x`` is in ascending order, as the plot engine keeps it. Returns
    None when no sample lies in the range.
    """
    if x.size == 0:
        return None
    first = int(np.searchsorted(x, x_min, side="left"))
    end = int(np.searchsorted(x, x_max, side="right"))
    window = y[first:end]
    window = window[np.isfinite(window)]
    if window.size == 0:
        return None
    return SeriesStats(
        last=float(window[-1]),
        minimum=float(window.min()),
        maximum=float(window.max()),
        mean=float(window.mean()),
    )


def magnitude(y: np.ndarray) -> float:
    """How large a value gets: the greatest distance of a sample from zero."""
    finite = y[np.isfinite(y)]
    return float(np.abs(finite).max()) if finite.size else 0.0


def group_by_scale(magnitudes: dict[str, float]) -> list[list[str]]:
    """Put values of similar size in the same panel.

    Values are ordered by size. A value joins the panel of the smaller
    values before it while it is no more than ``PLOT_PANEL_SCALE_RATIO``
    times the smallest of them; otherwise it starts a new panel. A
    value that never left zero has no size of its own and joins the
    first panel. If more than ``PLOT_MAX_PANELS`` panels would result,
    the two neighbouring panels closest in size are joined until they fit.

    Within a panel, values keep the order they were given in. Panels are
    ordered by the position of their first value, so the layout does not
    depend on the numbers alone.
    """
    names = list(magnitudes)
    if not names:
        return []
    sized = sorted((name for name in names if magnitudes[name] > 0), key=magnitudes.get)
    flat = [name for name in names if magnitudes[name] <= 0]

    groups: list[list[str]] = []
    for name in sized:
        if groups and magnitudes[name] <= magnitudes[groups[-1][0]] * PLOT_PANEL_SCALE_RATIO:
            groups[-1].append(name)
        else:
            groups.append([name])
    if not groups:
        groups.append([])
    groups[0].extend(flat)

    while len(groups) > PLOT_MAX_PANELS:
        # The gap between two neighbours is how many times larger the
        # smallest value of one is than the smallest value of the other.
        gaps = [
            magnitudes[groups[index + 1][0]] / magnitudes[groups[index][0]]
            for index in range(len(groups) - 1)
        ]
        closest = gaps.index(min(gaps))
        groups[closest:closest + 2] = [groups[closest] + groups[closest + 1]]

    position = {name: index for index, name in enumerate(names)}
    ordered = [sorted(group, key=position.get) for group in groups]
    return sorted(ordered, key=lambda group: position[group[0]])


def nearest_sample(times: list[np.ndarray], at: float) -> float | None:
    """The time of the sample nearest to ``at``, in any of ``times``.

    Each array holds one series' sample times, oldest first. None when
    there is no sample at all.
    """
    best: float | None = None
    for x in times:
        if x.size == 0:
            continue
        index = int(np.searchsorted(x, at))
        for candidate in (index - 1, index):
            if 0 <= candidate < x.size:
                time = float(x[candidate])
                if best is None or abs(time - at) < abs(best - at):
                    best = time
    return best


def value_held(x: np.ndarray, y: np.ndarray, at: float) -> float | None:
    """The value of the last sample at or before ``at``: what was received.

    None before the first sample, and for a value that is not a number:
    the plot shows only what was received, never a value between two.
    """
    index = int(np.searchsorted(x, at, side="right")) - 1
    if index < 0:
        return None
    value = float(y[index])
    return value if np.isfinite(value) else None
