# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for core.plot_overview: least and greatest samples in fixed memory."""

from __future__ import annotations

import numpy as np
import pytest

from core.plot_overview import Overview


def _add(overview: Overview, pairs: list[tuple[float, float]]) -> None:
    x, y = zip(*pairs, strict=True)
    overview.add(np.array(x, dtype=float), np.array(y, dtype=float))


def test_each_slice_keeps_its_least_and_greatest_sample_in_time_order() -> None:
    overview = Overview(slices=4, first_slice_seconds=1.0)

    _add(overview, [(0.1, 5), (0.5, 9), (0.9, 1), (1.2, 3), (1.7, 3)])

    x, y = overview.samples(0, 2)
    assert list(zip(x, y, strict=True)) == [(0.5, 9), (0.9, 1), (1.2, 3)]


def test_a_spike_survives_any_amount_of_zooming_out() -> None:
    overview = Overview(slices=8, first_slice_seconds=0.001)
    times = np.arange(100_000) * 0.01
    values = np.zeros(times.size)
    values[61_234] = 250.0

    overview.add(times, values)

    assert overview.slice_seconds >= times[-1] / 8
    x, y = overview.samples(0, times[-1])
    assert (times[61_234], 250.0) in set(zip(x, y, strict=True))


def test_joining_slices_keeps_counts_and_sums() -> None:
    overview = Overview(slices=4, first_slice_seconds=1.0)
    _add(overview, [(0.5, 1), (1.5, 2), (2.5, 3), (3.5, 4)])

    _add(overview, [(9.5, 10)])

    assert overview.slice_seconds == 4.0
    figures = overview.figures(0, 16)
    assert (figures.count, figures.minimum, figures.maximum, figures.total) == (5, 1, 10, 20)
    assert overview.figures(0, 3.9).count == 4


def test_samples_may_arrive_out_of_order() -> None:
    overview = Overview(slices=4, first_slice_seconds=1.0)

    _add(overview, [(0.9, 2), (0.1, 7), (0.5, -3)])

    x, y = overview.samples(0, 1)
    assert list(zip(x, y, strict=True)) == [(0.1, 7), (0.5, -3)]


def test_of_equal_values_the_first_received_is_kept() -> None:
    overview = Overview(slices=4, first_slice_seconds=1.0)

    _add(overview, [(0.2, 4), (0.4, 4)])
    _add(overview, [(0.6, 4)])

    x, y = overview.samples(0, 1)
    assert list(zip(x, y, strict=True)) == [(0.2, 4)]


def test_values_and_times_that_are_not_numbers_are_not_counted() -> None:
    overview = Overview(slices=4, first_slice_seconds=1.0)

    _add(overview, [(0.2, np.nan), (0.4, np.inf), (np.nan, 3), (-1, 3), (0.5, 2)])

    assert overview.figures(0, 4).count == 1


def test_empty_ranges_give_nothing() -> None:
    overview = Overview(slices=4, first_slice_seconds=1.0)
    assert overview.figures(0, 4) is None
    _add(overview, [(0.5, 1)])

    assert overview.samples(2, 3)[0].size == 0
    assert overview.figures(2, 3) is None


def test_clear_starts_again_from_the_first_slice_length() -> None:
    overview = Overview(slices=2, first_slice_seconds=1.0)
    _add(overview, [(5.0, 1)])

    overview.clear()

    assert overview.is_empty
    assert overview.slice_seconds == 1.0


@pytest.mark.parametrize(("slices", "seconds"), [(3, 1.0), (0, 1.0), (4, 0.0)])
def test_shapes_that_cannot_work_are_refused(slices: int, seconds: float) -> None:
    with pytest.raises(ValueError):
        Overview(slices=slices, first_slice_seconds=seconds)
