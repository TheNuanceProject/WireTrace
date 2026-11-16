# SPDX-License-Identifier: GPL-3.0-or-later
"""A module that takes a moment to load, standing in for the plot."""

from __future__ import annotations

import threading
import time

LOADED_ON = threading.current_thread().name
time.sleep(0.2)
