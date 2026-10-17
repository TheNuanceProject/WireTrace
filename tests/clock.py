# SPDX-License-Identifier: GPL-3.0-or-later
"""A deterministic source of arrival timestamps for tests."""

from __future__ import annotations

from datetime import datetime, timedelta

_EPOCH = datetime(2026, 1, 1, 12, 0, 0)
_STEP = timedelta(milliseconds=10)


class TickClock:
    """Returns timestamps that advance by a fixed step on every call."""

    def __init__(self) -> None:
        self._now = _EPOCH

    def __call__(self) -> datetime:
        self._now += _STEP
        return self._now
