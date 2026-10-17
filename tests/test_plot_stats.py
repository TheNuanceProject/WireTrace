# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for core.plot_stats: the figures the plot shows beside its lines."""

from __future__ import annotations

import numpy as np
import pytest

from app.constants import PLOT_MAX_PANELS, PLOT_PANEL_SCALE_RATIO
from core.plot_stats import (
    SeriesStats,
    group_by_scale,
    magnitude,
    nearest_sample,
    value_held,
    visible_stats,
)

_X = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
_Y = np.array([10.0, 20.0, 40.0, 30.0, 50.0])
_EMPTY = np.empty(0)


class TestVisibleStats:
    def test_whole_range(self):
        assert visible_stats(_X, _Y, 0.0, 4.0) == SeriesStats(50.0, 10.0, 50.0, 30.0)

    def test_only_samples_inside_the_range_count(self):
        assert visible_stats(_X, _Y, 1.0, 3.0) == SeriesStats(30.0, 20.0, 40.0, 30.0)

    def test_range_between_two_samples_holds_nothing(self):
        assert visible_stats(_X, _Y, 1.2, 1.8) is None

    def test_no_samples(self):
        assert visible_stats(_EMPTY, _EMPTY, 0.0, 10.0) is None

    def test_range_wider_than_the_data(self):
        assert visible_stats(_X, _Y, -100.0, 100.0).mean == 30.0

    def test_values_that_are_not_numbers_are_left_out(self):
        y = np.array([10.0, np.nan, 40.0, np.inf, 50.0])

        assert visible_stats(_X, y, 0.0, 4.0) == SeriesStats(50.0, 10.0, 50.0, pytest.approx(100 / 3))

    def test_range_holding_only_values_that_are_not_numbers(self):
        assert visible_stats(_X, np.full(5, np.nan), 0.0, 4.0) is None


class TestMagnitude:
    def test_greatest_distance_from_zero(self):
        assert magnitude(np.array([-7.0, 3.0, 5.0])) == 7.0

    def test_empty_or_unusable_series_has_none(self):
        assert magnitude(_EMPTY) == 0.0
        assert magnitude(np.array([np.nan, np.inf])) == 0.0


class TestGroupByScale:
    def test_nothing_gives_no_panels(self):
        assert group_by_scale({}) == []

    def test_values_of_similar_size_share_a_panel(self):
        sizes = {"RPM": 1474.0, "Current": 4.11, "Voltage": 12.55, "MotorTemp": 52.2}

        assert group_by_scale(sizes) == [["RPM"], ["Current", "Voltage"], ["MotorTemp"]]

    def test_one_value_is_one_panel(self):
        assert group_by_scale({"temp": 21.5}) == [["temp"]]

    def test_all_alike_is_one_panel_in_the_given_order(self):
        assert group_by_scale({"c": 3.0, "a": 1.0, "b": 2.0}) == [["c", "a", "b"]]

    def test_the_ratio_is_measured_from_the_smallest_of_the_panel(self):
        just_in = {"a": 1.0, "b": PLOT_PANEL_SCALE_RATIO}
        just_out = {"a": 1.0, "b": PLOT_PANEL_SCALE_RATIO * 1.01}
        chain = {"a": 1.0, "b": 9.0, "c": 81.0}

        assert group_by_scale(just_in) == [["a", "b"]]
        assert group_by_scale(just_out) == [["a"], ["b"]]
        assert group_by_scale(chain) == [["a", "b"], ["c"]]

    def test_a_value_that_never_left_zero_joins_the_first_panel(self):
        assert group_by_scale({"flag": 0.0, "big": 5000.0, "small": 2.0}) == [
            ["flag", "small"], ["big"],
        ]

    def test_only_zeros_is_one_panel(self):
        assert group_by_scale({"a": 0.0, "b": 0.0}) == [["a", "b"]]

    def test_no_more_panels_than_the_limit_and_the_closest_are_joined(self):
        # One panel too many; "b" and "c" are the two closest in size.
        sizes = {"a": 1.0, "b": 100.0, "c": 1_100.0}
        sizes.update({f"far{n}": 1e6 * 1000.0 ** n for n in range(PLOT_MAX_PANELS - 2)})
        assert len(sizes) == PLOT_MAX_PANELS + 1

        panels = group_by_scale(sizes)

        assert len(panels) == PLOT_MAX_PANELS
        assert ["b", "c"] in panels
        assert ["a"] in panels

    def test_every_value_is_placed_exactly_once(self):
        sizes = {f"s{n}": float(n * n * 37 % 1000) for n in range(20)}

        placed = [name for panel in group_by_scale(sizes) for name in panel]

        assert sorted(placed) == sorted(sizes)


class TestWhatWasReceived:
    def test_the_nearest_sample_is_a_received_time(self):
        assert nearest_sample([_X], 2.4) == 2.0
        assert nearest_sample([_X], 2.6) == 3.0

    def test_the_nearest_sample_of_several_series(self):
        late = np.array([2.45, 9.0])
        assert nearest_sample([_X, late], 2.4) == 2.45

    @pytest.mark.parametrize("at", [-5.0, 99.0])
    def test_beyond_the_samples_the_nearest_is_the_end(self, at):
        assert nearest_sample([_X], at) == (0.0 if at < 0 else 4.0)

    def test_no_sample_no_time(self):
        assert nearest_sample([_EMPTY], 1.0) is None
        assert nearest_sample([], 1.0) is None

    def test_value_on_a_sample(self):
        assert value_held(_X, _Y, 2.0) == 40.0

    def test_between_samples_the_last_received_value_holds(self):
        assert value_held(_X, _Y, 2.9) == 40.0

    def test_no_value_before_the_first_sample(self):
        assert value_held(_X, _Y, -0.1) is None
        assert value_held(_EMPTY, _EMPTY, 1.0) is None

    def test_a_value_that_is_not_a_number_is_none(self):
        assert value_held(_X, np.array([1.0, np.nan, 3.0, 4.0, 5.0]), 1.0) is None
