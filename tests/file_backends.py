# SPDX-License-Identifier: GPL-3.0-or-later
"""File backends that misbehave on demand, for testing disk failures."""

from __future__ import annotations

import errno
import threading

from core.log_files import OsFileBackend


class FaultyBackend(OsFileBackend):
    """A real backend whose writes and creates can be made to fail."""

    def __init__(self) -> None:
        self.fail_writes = False
        self.fail_creates = False
        self.fail_closes = False
        # Writes fail only for files whose name ends with this.
        self.fail_writes_to_suffix: str | None = None
        self.sync_calls = 0
        self._failing_descriptors: set[int] = set()
        self.max_bytes_per_write: int | None = None
        self.accept_before_failing: int | None = None
        self.write_calls = 0

    def create(self, path: str, *mode: int) -> int:
        if self.fail_creates:
            raise OSError(errno.ENOSPC, "No space left on device")
        descriptor = super().create(path, *mode)
        if self.fail_writes_to_suffix and path.endswith(self.fail_writes_to_suffix):
            self._failing_descriptors.add(descriptor)
        return descriptor

    def write(self, descriptor: int, data: memoryview) -> int:
        self.write_calls += 1
        if descriptor in self._failing_descriptors:
            raise OSError(errno.ENOSPC, "No space left on device")
        if self.accept_before_failing is not None:
            if self.accept_before_failing == 0:
                raise OSError(errno.ENOSPC, "No space left on device")
            data = data[:self.accept_before_failing]
            self.accept_before_failing -= len(data)
            return super().write(descriptor, data)
        if self.fail_writes:
            raise OSError(errno.ENOSPC, "No space left on device")
        if self.max_bytes_per_write is not None:
            data = data[:self.max_bytes_per_write]
        return super().write(descriptor, data)

    def sync(self, descriptor: int) -> None:
        self.sync_calls += 1
        super().sync(descriptor)

    def close(self, descriptor: int) -> None:
        super().close(descriptor)
        self._failing_descriptors.discard(descriptor)
        if self.fail_closes:
            raise OSError(errno.EIO, "I/O error on close")


class GatedBackend(OsFileBackend):
    """A real backend whose writes wait until the gate is opened.

    Stands in for a disk that has stopped responding without failing.
    """

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.gate.set()
        self.blocked = threading.Event()

    def write(self, descriptor: int, data: memoryview) -> int:
        if not self.gate.is_set():
            self.blocked.set()
            self.gate.wait()
        return super().write(descriptor, data)
