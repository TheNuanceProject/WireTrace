# SPDX-License-Identifier: GPL-3.0-or-later
"""The samples a chart needs to draw a line: first, least, greatest and
last of each pixel column, every break, and every segment into view."""

from __future__ import annotations

from itertools import pairwise

import numpy as np
import pytest

from core.plot_thinning import SAMPLES_PER_COLUMN, points_to_draw

_COLUMNS = 100


def _noise(count: int = 20_000, seed: int = 3) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    x = np.sort(rng.uniform(0.0, 10.0, count))
    y = rng.normal(size=count)
    y[::997] += 8.0
    return x, y


def _column(x: np.ndarray, low: float, high: float, columns: int = _COLUMNS) -> np.ndarray:
    return np.minimum(((x - low) * (columns / (high - low))).astype(int), columns - 1)


def _positions(x: np.ndarray, drawn_x: np.ndarray) -> np.ndarray:
    """Where each drawn sample was among the samples given (all times differ)."""
    return np.searchsorted(x, drawn_x)


class TestNothingToSave:
    def test_few_samples_are_given_back_as_they_are(self):
        x, y = _noise(count=SAMPLES_PER_COLUMN * _COLUMNS)

        drawn_x, drawn_y = points_to_draw(x, y, 0.0, 10.0, _COLUMNS)

        assert drawn_x is x and drawn_y is y

    @pytest.mark.parametrize(("low", "high", "columns"), [(0.0, 10.0, 0), (5.0, 5.0, 100), (6.0, 5.0, 100)])
    def test_no_columns_or_no_time_gives_back_every_sample(self, low, high, columns):
        x, y = _noise()

        drawn_x, drawn_y = points_to_draw(x, y, low, high, columns)

        assert drawn_x is x and drawn_y is y


class TestColumns:
    def test_each_column_keeps_its_first_least_greatest_and_last_sample(self):
        x, y = _noise()

        drawn_x, drawn_y = points_to_draw(x, y, 0.0, 10.0, _COLUMNS)

        columns, drawn_columns = _column(x, 0.0, 10.0), _column(drawn_x, 0.0, 10.0)
        for column in np.unique(columns):
            given = columns == column
            kept = drawn_columns == column
            assert drawn_y[kept].min() == y[given].min()
            assert drawn_y[kept].max() == y[given].max()
            assert drawn_x[kept][0] == x[given][0]
            assert drawn_x[kept][-1] == x[given][-1]
            assert kept.sum() <= SAMPLES_PER_COLUMN

    def test_samples_kept_are_samples_given_in_their_order(self):
        x, y = _noise()

        drawn_x, drawn_y = points_to_draw(x, y, 0.0, 10.0, _COLUMNS)
        at = _positions(x, drawn_x)

        assert np.all(np.diff(at) > 0)
        assert np.array_equal(x[at], drawn_x)
        assert np.array_equal(y[at], drawn_y)

    def test_every_spike_is_kept(self):
        x, y = _noise()

        _, drawn_y = points_to_draw(x, y, 0.0, 10.0, _COLUMNS)

        assert np.count_nonzero(drawn_y > 5.0) == np.count_nonzero(y > 5.0)

    def test_a_column_of_equal_values_keeps_one_least_and_one_greatest(self):
        x = np.linspace(0.0, 10.0, 10_000, endpoint=False)
        y = np.ones_like(x)

        drawn_x, _ = points_to_draw(x, y, 0.0, 10.0, _COLUMNS)

        assert drawn_x.size <= 2 * _COLUMNS


class TestOutsideTheTime:
    def test_only_the_samples_next_to_the_time_are_kept_outside_it(self):
        x, y = _noise()

        drawn_x, _ = points_to_draw(x, y, 3.0, 7.0, _COLUMNS)

        before, after = x[x < 3.0], x[x > 7.0]
        assert drawn_x[drawn_x < 3.0].tolist() == [before[0], before[-1]]
        assert drawn_x[drawn_x > 7.0].tolist() == [after[0], after[-1]]

    def test_a_line_that_crosses_the_time_without_a_sample_in_it_is_kept(self):
        x = np.concatenate((np.linspace(0.0, 2.0, 5_000), np.linspace(8.0, 10.0, 5_000)))
        y = np.arange(x.size, dtype=float)

        drawn_x, _ = points_to_draw(x, y, 4.0, 6.0, _COLUMNS)

        assert 2.0 in drawn_x and 8.0 in drawn_x

    def test_samples_out_of_time_order_keep_every_segment_into_the_time(self):
        # A clock set back: the times run 0 to 10, then 0 to 10 again.
        x = np.concatenate((np.linspace(0.0, 10.0, 6_000), np.linspace(0.0, 10.0, 6_000)))
        y = np.sin(np.arange(x.size) / 7.0)

        drawn_x, drawn_y = points_to_draw(x, y, 4.0, 6.0, _COLUMNS)

        # Each pass enters and leaves the time at the samples next to it.
        for edge, side in ((4.0, x < 4.0), (6.0, x > 6.0)):
            runs = np.flatnonzero(np.diff(np.concatenate(([0], side.astype(int), [0]))))
            for start, end in zip(runs[::2], runs[1::2] - 1, strict=True):
                assert x[start] in drawn_x and x[end] in drawn_x, edge
        inside = (x >= 4.0) & (x <= 6.0)
        assert drawn_y[(drawn_x >= 4.0) & (drawn_x <= 6.0)].max() == y[inside].max()


class TestBreaks:
    @pytest.mark.parametrize("gap", [np.nan, np.inf, -np.inf])
    def test_a_break_is_kept_with_the_samples_either_side(self, gap):
        x, y = _noise()
        y = y.copy()
        y[10_000] = gap

        drawn_x, drawn_y = points_to_draw(x, y, 0.0, 10.0, _COLUMNS)

        at = _positions(x, drawn_x)
        assert 10_000 in at
        assert 9_999 in at and 10_001 in at
        assert np.count_nonzero(~np.isfinite(drawn_y)) == 1

    def test_a_break_in_the_time_of_a_sample_is_kept(self):
        x, y = _noise()
        x = x.copy()
        x[5_000] = np.nan

        drawn_x, _ = points_to_draw(x, y, 0.0, 10.0, _COLUMNS)

        assert np.count_nonzero(np.isnan(drawn_x)) == 1

    def test_a_run_of_breaks_keeps_one_with_the_samples_either_side(self):
        x, y = _noise()
        y = y.copy()
        y[10_000:10_500] = np.nan

        drawn_x, drawn_y = points_to_draw(x, y, 0.0, 10.0, _COLUMNS)

        at = _positions(x, drawn_x)
        assert 10_000 in at
        assert 9_999 in at and 10_500 in at
        assert np.count_nonzero(np.isnan(drawn_y)) == 1

    def test_a_sensor_that_sends_nan_every_other_sample_is_still_thinned(self):
        # A failed sensor often prints "nan": every other value here.
        x, y = _noise()
        y = y.copy()
        y[::2] = np.nan

        drawn_x, drawn_y = points_to_draw(x, y, 0.0, 10.0, _COLUMNS)

        # Each column at most its four samples and the breaks between its
        # runs; nowhere near every sample.
        assert drawn_x.size < x.size // 10
        assert np.isnan(drawn_y).any()

    @pytest.mark.parametrize("share", [0.05, 0.3, 0.7])
    def test_no_line_is_drawn_across_a_break(self, share):
        x, y = _noise()
        y = y.copy()
        y[np.random.default_rng(5).random(y.size) < share] = np.nan

        drawn_x, _ = points_to_draw(x, y, 0.0, 10.0, _COLUMNS)

        at = _positions(x, drawn_x)
        finite = np.isfinite(y)
        for left, right in pairwise(at):
            if finite[left] and finite[right]:
                assert finite[left:right + 1].all(), (left, right)
