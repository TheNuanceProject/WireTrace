# SPDX-License-Identifier: MIT
"""Tests for window-geometry screen clamping (ui.window_geometry).

Restoring a saved window position that lies on a now-disconnected monitor
would place the window off-screen (taskbar entry only). These tests pin the
policy: keep the saved position when it is visible on a connected screen,
otherwise centre on the primary screen.
"""

from __future__ import annotations

from ui.window_geometry import visible_geometry

LAPTOP = (0, 0, 1920, 1040)  # single built-in screen, available geometry
MIN = (800, 600)


class TestVisibleGeometry:
    def test_saved_on_screen_is_kept(self):
        saved = (100, 100, 1200, 800)
        assert visible_geometry(saved, [LAPTOP], MIN, LAPTOP) == saved

    def test_offscreen_external_monitor_is_recentred(self):
        # The real bug: saved on an external monitor at x=2934 that is now
        # disconnected. Only the laptop screen remains.
        saved = (2934, 246, 958, 844)
        x, y, w, h = visible_geometry(saved, [LAPTOP], MIN, LAPTOP)
        assert (w, h) == (958, 844)
        # Centred on the laptop screen, fully on-screen.
        assert x == (1920 - 958) // 2
        assert y == (1040 - 844) // 2
        assert x >= 0 and x + w <= 1920
        assert y >= 0 and y + h <= 1040

    def test_kept_when_external_monitor_still_connected(self):
        external = (1920, 0, 1920, 1080)
        saved = (2934, 246, 958, 844)
        assert visible_geometry(saved, [LAPTOP, external], MIN, LAPTOP) == saved

    def test_sliver_overlap_is_not_enough(self):
        # Window pushed almost entirely off the right edge (only ~30px on).
        saved = (1890, 100, 958, 844)
        x = visible_geometry(saved, [LAPTOP], MIN, LAPTOP)[0]
        assert x == (1920 - 958) // 2  # recentred, not left as a sliver

    def test_size_clamped_up_to_minimum(self):
        saved = (100, 100, 200, 150)  # below the 800x600 floor
        _, _, w, h = visible_geometry(saved, [LAPTOP], MIN, LAPTOP)
        assert (w, h) == (800, 600)

    def test_size_clamped_down_to_screen_when_recentred(self):
        small = (0, 0, 1000, 700)
        saved = (5000, 5000, 1920, 1200)  # off-screen and bigger than screen
        x, y, w, h = visible_geometry(saved, [small], MIN, small)
        assert w <= 1000 and h <= 700
        assert x >= 0 and y >= 0

    def test_no_screens_returns_clamped_saved(self):
        saved = (10, 10, 900, 700)
        assert visible_geometry(saved, [], MIN, None) == saved
