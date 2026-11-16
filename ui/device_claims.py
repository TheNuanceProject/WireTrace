# SPDX-License-Identifier: GPL-3.0-or-later
"""Which tab holds which port, so a board belongs to one tab only.

A tab claims its port while connected and gives it up when the
connection ends or is lost. A tab waiting for its lost board never
matches, opens or offers a port another tab holds.

Across WireTrace windows the open itself settles it: a port is opened
for one program at a time (``core/transport.py``).

The register is read from the threads that look for lost boards, so it
is guarded by a lock.
"""

from __future__ import annotations

import threading


class DeviceClaims:
    """The ports held by the tabs of this WireTrace."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: dict[int, str] = {}

    def claim(self, holder: object, port: str) -> None:
        with self._lock:
            self._held[id(holder)] = port

    def release(self, holder: object) -> None:
        with self._lock:
            self._held.pop(id(holder), None)

    def held_by_others(self, holder: object) -> frozenset[str]:
        """The ports held by every tab but ``holder``."""
        with self._lock:
            return frozenset(port for key, port in self._held.items() if key != id(holder))


#: The register of this WireTrace's tabs.
CLAIMS = DeviceClaims()
