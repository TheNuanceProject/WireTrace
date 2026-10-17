# SPDX-License-Identifier: GPL-3.0-or-later
"""The frames of WireTrace's windows, redrawn when the theme changes.

On Windows, Qt tells the system at once whether a window's title bar is
dark (QWindowsWindow::setDarkBorder), but Windows 10 draws the title bar
again only when the window is next activated or deactivated: until then
it kept the colours of the theme before. ``redraw_frames`` asks Windows
to draw each title bar now, the way activation does: drawn in the other
state, then in the state the window is in.

Of the ways tried on Windows 10 (22H2), this is the one that drew the
title bar at once and cleanly. A single message in the window's own
state left the title text on a box and lost the caption buttons;
redrawing the frame (RedrawWindow) or recalculating it (SetWindowPos
with SWP_FRAMECHANGED) changed nothing until the next activation. Other
systems draw the frame themselves and need nothing.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication

#: The message Windows sends a window whose title bar is to be drawn
#: active (wParam 1) or inactive (wParam 0): WM_NCACTIVATE.
_WM_NCACTIVATE = 0x0086
_INACTIVE, _ACTIVE = 0, 1


def redraw_frames() -> None:
    """Have the system draw the title bar of every shown window again."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    send = user32.SendMessageW
    send.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    send.restype = ctypes.c_ssize_t
    framed = (Qt.WindowType.Window, Qt.WindowType.Dialog)
    for window in QGuiApplication.topLevelWindows():
        if not window.isVisible() or window.type() not in framed:
            continue
        if window.flags() & Qt.WindowType.FramelessWindowHint:
            continue
        handle = int(window.winId())
        state = _ACTIVE if window.isActive() else _INACTIVE
        send(handle, _WM_NCACTIVATE, _ACTIVE - state, 0)
        send(handle, _WM_NCACTIVATE, state, 0)
