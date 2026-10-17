# SPDX-License-Identifier: GPL-3.0-or-later
"""A lock on a file, held by one open WireTrace at a time.

Several WireTrace windows can be open at once, and they share the
preferences and the usage notes. Each writes one of those files only
while it holds the lock beside it, after reading what the others wrote,
so no window's change is ever lost.

The lock is the operating system's own: ``fcntl.flock`` on Linux and
macOS, ``msvcrt.locking`` on Windows. The system releases it when the
process that holds it ends, however it ends (Windows does so shortly
after the process has ended, not at once, which ``acquire`` waits out). That is also how a run that
did not close normally is recognised: its lock can be taken by another.

The lock file itself holds nothing. A lock file that is no longer needed
is removed only by a process that holds its lock (``FileLock.remove``),
and a lock is taken only once the file locked is the one at the path, so
a lock is never held on a file that another process has just removed.
This module depends on nothing else in the project.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Iterator
from pathlib import Path

if os.name == "nt":
    import msvcrt

    def _try_lock(descriptor: int) -> bool:
        # The first byte is the lock; the file may be empty.
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(descriptor: int) -> bool:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _unlock(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


#: How often a wait for a lock looks again.
_POLL_S = 0.02
#: How many times a lock file removed while it was being locked is
#: opened again before the lock is taken to be held by another.
_REOPEN_ATTEMPTS = 3


def _is_file_at(descriptor: int, path: Path) -> bool:
    """Whether ``descriptor`` is the file now at ``path``."""
    try:
        at_path = os.stat(path)
    except FileNotFoundError:
        return False
    opened = os.fstat(descriptor)
    return (opened.st_dev, opened.st_ino) == (at_path.st_dev, at_path.st_ino)


class LockTimeout(OSError):
    """The lock was held by another for longer than the wait allowed."""


class FileLock:
    """An exclusive lock on ``path``, which is created if it is missing.

    Use it as a context manager, or with ``acquire`` and ``release``. A
    lock is held at most once at a time, and is released when the
    process ends even if ``release`` is never called.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._descriptor: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def held(self) -> bool:
        return self._descriptor is not None

    def try_acquire(self) -> bool:
        """Take the lock if no one holds it; whether it was taken.

        Raises:
            OSError: the lock file could not be opened or created.
        """
        if self._descriptor is not None:
            return True
        self._path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(_REOPEN_ATTEMPTS):
            # Not inherited by child processes (PEP 446), so a program
            # started from WireTrace never holds its lock.
            descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
            if not _try_lock(descriptor):
                os.close(descriptor)
                return False
            if _is_file_at(descriptor, self._path):
                self._descriptor = descriptor
                return True
            # Removed by its last holder between opening and locking: the
            # lock on that file is no lock at all. Open the path again.
            _unlock(descriptor)
            os.close(descriptor)
        return False

    def acquire(self, timeout_s: float) -> None:
        """Take the lock, waiting up to ``timeout_s`` seconds for it.

        Raises:
            LockTimeout: another held it for longer than that.
            OSError: the lock file could not be opened or created.
        """
        deadline = time.monotonic() + timeout_s
        while not self.try_acquire():
            if time.monotonic() >= deadline:
                raise LockTimeout(f"the lock {self._path} is held by another")
            time.sleep(_POLL_S)

    def release(self) -> None:
        """Let the lock go. Releasing a lock not held does nothing."""
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is None:
            return
        try:
            _unlock(descriptor)
        finally:
            os.close(descriptor)

    def remove(self) -> None:
        """Remove the lock file and let the lock go. Removing a lock not
        held does nothing.

        On Linux and macOS the file is removed while the lock is still
        held, so another process can never lock it in between. On Windows
        a file that another process has open cannot be removed; it is then
        that process's, and is left as it is.

        Raises:
            OSError: the file could not be removed.
        """
        if self._descriptor is None:
            return
        if os.name == "nt":
            self.release()
            with contextlib.suppress(FileNotFoundError, PermissionError):
                self._path.unlink()
            return
        try:
            self._path.unlink(missing_ok=True)
        finally:
            self.release()


@contextlib.contextmanager
def held_lock(path: Path | str, timeout_s: float) -> Iterator[FileLock]:
    """Hold the lock on ``path`` for a ``with`` block, waiting up to
    ``timeout_s`` seconds for it.

    Raises:
        LockTimeout: another held it for longer than that.
        OSError: the lock file could not be opened or created.
    """
    lock = FileLock(path)
    lock.acquire(timeout_s)
    try:
        yield lock
    finally:
        lock.release()
