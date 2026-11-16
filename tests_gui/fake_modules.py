# SPDX-License-Identifier: GPL-3.0-or-later
"""Modules for the tests of the module host."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ui.module_host import TabModule


class RecordingModule(TabModule):
    """Keeps what it is told."""

    reads_lines = True

    def start(self) -> None:
        self.events = ["start"]
        self.texts: list[str] = []
        self.skipped = 0

    def stop(self) -> None:
        self.events.append("stop")

    def take_records(self, records, skipped: int) -> None:
        self.texts.extend(record.text for record in records)
        self.skipped += skipped

    def session_started(self) -> None:
        self.events.append("session_started")

    def session_ended(self, unexpected: bool) -> None:
        self.events.append(f"session_ended:{unexpected}")


class FailingModule(TabModule):
    """Raises on the first records it is given."""

    reads_lines = True

    def take_records(self, records, skipped: int) -> None:
        raise RuntimeError("a fault inside the module")


class _Bell(QObject):
    rung = Signal(int)


class SlotFailingModule(TabModule):
    """Raises in a slot it connected, on the second ring of its bell."""

    def start(self) -> None:
        self.rings: list[int] = []
        self.bell = _Bell()
        self.bell.rung.connect(self.guarded(self._ring))

    def _ring(self, number: int) -> None:
        self.rings.append(number)
        if number == 2:
            raise RuntimeError("a fault inside a slot")
