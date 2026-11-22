# SPDX-License-Identifier: GPL-3.0-or-later
"""Whether the system asks applications to keep motion to a minimum.

Qt does not report this setting, so it is read where each system keeps
it, as browsers read it for ``prefers-reduced-motion``:

- Windows: "Show animations in Windows" (``SPI_GETCLIENTAREAANIMATION``);
  off asks for reduced motion.
- macOS: Accessibility > Display > Reduce motion
  (``NSWorkspace.accessibilityDisplayShouldReduceMotion``).
- Linux: GNOME's ``enable-animations`` (which GTK applications follow),
  read with ``gsettings``; or, on KDE Plasma, an animation speed of 0
  (``AnimationDurationFactor`` in ``kdeglobals``).

When the setting cannot be read, motion is not reduced, as browsers do
when no preference is given; the motion WireTrace uses is short.

This module does not touch the GUI.
"""

from __future__ import annotations

import configparser
import ctypes
import ctypes.util
import logging
import os
import shutil
import subprocess
import sys

from app.constants import SYSTEM_MOTION_TIMEOUT_S

logger = logging.getLogger(__name__)

_SPI_GETCLIENTAREAANIMATION = 0x1042


def reduced_motion() -> bool:
    """Whether the system asks for reduced motion."""
    try:
        if sys.platform == "win32":
            return _windows()
        if sys.platform == "darwin":
            return _macos()
        return _linux()
    except (OSError, AttributeError, ValueError, subprocess.SubprocessError) as exc:
        logger.info("The system's motion setting could not be read: %s", exc)
        return False


def _windows() -> bool:
    animations = ctypes.c_int(1)
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    if not user32.SystemParametersInfoW(_SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(animations), 0):
        raise OSError("SystemParametersInfoW refused SPI_GETCLIENTAREAANIMATION")
    return not animations.value


def _macos() -> bool:
    path = ctypes.util.find_library("objc")
    if path is None:
        raise OSError("the Objective-C runtime was not found")
    objc = ctypes.cdll.LoadLibrary(path)
    ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AppKit.framework/AppKit")
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    send = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
    to_object = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(send)
    to_bool = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(send)
    workspace = to_object(objc.objc_getClass(b"NSWorkspace"), objc.sel_registerName(b"sharedWorkspace"))
    if not workspace:
        raise OSError("NSWorkspace is not available")
    return bool(to_bool(workspace, objc.sel_registerName(b"accessibilityDisplayShouldReduceMotion")))


def _linux() -> bool:
    return _gnome_animations_off() or _kde_animations_off()


def _gnome_animations_off() -> bool:
    gsettings = shutil.which("gsettings")
    if gsettings is None:
        return False
    result = subprocess.run(
        [gsettings, "get", "org.gnome.desktop.interface", "enable-animations"],
        capture_output=True, text=True, timeout=SYSTEM_MOTION_TIMEOUT_S, check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "false"


def _kde_animations_off() -> bool:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    settings = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        if not settings.read(os.path.join(base, "kdeglobals"), encoding="utf-8"):
            return False
    except (configparser.Error, UnicodeDecodeError):
        return False
    try:
        return float(settings.get("KDE", "AnimationDurationFactor", fallback="1")) == 0
    except ValueError:
        return False
