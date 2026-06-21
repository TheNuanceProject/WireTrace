# SPDX-License-Identifier: MIT
"""Pure geometry helpers for restoring window position safely.

Restoring a saved window position blindly is unsafe: the saved coordinates
may lie on a monitor that is no longer connected (e.g. an external display
on a laptop that has since been undocked). The window would then be placed
off-screen and appear only as a taskbar entry.

This module contains the screen-clamping *policy* as a pure function over
plain ``(x, y, w, h)`` rectangles so it can be unit-tested without Qt. The
Qt-facing caller (``MainWindow._restore_window_state``) only gathers the
current screen rectangles and delegates here.
"""

from __future__ import annotations

Rect = tuple[int, int, int, int]  # (x, y, w, h)

# Minimum overlap, per axis, for a saved position to count as "visible".
# A sliver poking onto a screen is not enough to grab the title bar.
_MIN_VISIBLE_OVERLAP = 100


def _overlap(a: Rect, b: Rect) -> tuple[int, int]:
    """Return the (width, height) of the intersection of two rectangles."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    iw = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    ih = max(0, min(ay + ah, by + bh) - max(ay, by))
    return iw, ih


def visible_geometry(
    saved: Rect,
    screens: list[Rect],
    min_size: tuple[int, int],
    primary: Rect | None,
) -> Rect:
    """Return a window geometry guaranteed visible on a connected screen.

    Args:
        saved: The saved ``(x, y, w, h)`` from config.
        screens: Available geometry of every connected screen.
        min_size: ``(min_w, min_h)`` floor for the window size.
        primary: Available geometry of the primary screen, used to centre
            the window when the saved position is not visible anywhere.

    The saved size is first clamped up to ``min_size``. If the resulting
    rectangle overlaps any screen by at least ``_MIN_VISIBLE_OVERLAP`` px on
    both axes, it is returned unchanged. Otherwise the window is centred on
    ``primary`` (falling back to the first screen, then to the saved values)
    with its size clamped to fit that screen.
    """
    min_w, min_h = min_size
    sx, sy, sw, sh = saved
    sw = max(sw, min_w)
    sh = max(sh, min_h)
    target: Rect = (sx, sy, sw, sh)

    for screen in screens:
        iw, ih = _overlap(target, screen)
        if iw >= _MIN_VISIBLE_OVERLAP and ih >= _MIN_VISIBLE_OVERLAP:
            return target

    base = primary or (screens[0] if screens else target)
    bx, by, bw, bh = base
    w = min(sw, bw)
    h = min(sh, bh)
    x = bx + (bw - w) // 2
    y = by + (bh - h) // 2
    return (x, y, w, h)
