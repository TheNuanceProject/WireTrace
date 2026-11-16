# SPDX-License-Identifier: GPL-3.0-or-later
"""Loads the plot's charting library before the plot is first opened.

pyqtgraph and numpy take a noticeable moment to load. While the plot is
switched on, they are loaded on a thread of their own shortly after a
tab is built, so the first Plot opens at once. Loading only defines
classes: no widget is made off the main thread. Opening the plot while
loading is still under way waits for it to finish, as any import waits
for the same import in progress.
"""

from __future__ import annotations

import importlib
import logging
import sys
import threading

logger = logging.getLogger(__name__)

#: The module that brings in pyqtgraph and numpy.
PLOT_MODULE = "ui.widgets.plot_view"

_THREAD_NAME = "wiretrace-plot-preload"
_lock = threading.Lock()
_thread: threading.Thread | None = None


def preload_plot() -> None:
    """Start loading the plot on a thread, once per run."""
    global _thread
    with _lock:
        if _thread is not None or PLOT_MODULE in sys.modules:
            return
        _thread = threading.Thread(target=_load, name=_THREAD_NAME, daemon=True)
        _thread.start()


def plot_loaded() -> bool:
    """Whether the plot is loaded, so opening it does not wait."""
    with _lock:
        loading = _thread is not None and _thread.is_alive()
    return not loading and PLOT_MODULE in sys.modules


def _load() -> None:
    try:
        importlib.import_module(PLOT_MODULE)
    except Exception:
        # The plot is loaded again when it is opened, and says why it
        # cannot be if it still fails.
        logger.exception("The plot could not be loaded ahead of its first use")
